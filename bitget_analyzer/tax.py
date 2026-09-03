"""Rejestry transakcyjne Bitget (tax records) - historia do ~2 lat wstecz.

Konta z grid botami potrafią mieć ponad 100 000 wpisów w jednym miesiącu,
dlatego rekordy są **agregowane w locie**: do pamięci trafiają miesięczne sumy
per moneta i kategoria, a nie pojedyncze wpisy. Podsumowanie każdego okresu
ląduje w pamięci podręcznej, więc kolejne uruchomienia nie pobierają go ponownie.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from .cache import WindowCache, snap
from .client import BitgetClient, BitgetError, time_windows
from .config import Config
from .futures import _classify as classify_futures
from .limits import window_guard
from .model import (
    CAT_DEPOSIT,
    CAT_EARN,
    CAT_OTHER,
    CAT_REWARD,
    CAT_TRADE,
    CAT_TRANSFER,
    CAT_WITHDRAW,
    Dataset,
    Fill,
    LedgerEntry,
    to_float,
)
from .prices import STABLECOINS, PriceBook

log = logging.getLogger("bitget.tax")

SPOT_TAX_PATH = "/api/v2/tax/spot-record"
FUTURES_TAX_PATH = "/api/v2/tax/future-record"

TAX_WINDOW_DAYS = 30
TAX_PAGE_LIMIT = 500

# Powyżej tylu wpisów handlowych rezygnujemy z odtwarzania pojedynczych
# transakcji - przy grid botach byłyby ich miliony, a wynik per para i tak
# policzymy z dokładnych danych z ostatnich 90 dni.
TRADE_RECONSTRUCTION_LIMIT = 20_000

QUOTE_COINS = set(STABLECOINS) | {"BTC", "ETH", "BGB", "EUR", "BRL", "TRY"}

SPOT_TYPE_KEYWORDS = (
    # Kolejność ma znaczenie: "subscribe"/"redeem" muszą wygrać z "earn",
    # bo to przesunięcie kapitału do produktu, a nie zysk.
    ("subscribe", CAT_TRANSFER),
    ("subscription", CAT_TRANSFER),
    ("redeem", CAT_TRANSFER),
    ("redemption", CAT_TRANSFER),
    ("principal", CAT_TRANSFER),
    ("stake", CAT_TRANSFER),
    ("deposit", CAT_DEPOSIT),
    ("withdraw", CAT_WITHDRAW),
    ("transfer", CAT_TRANSFER),
    ("buy", CAT_TRADE),
    ("sell", CAT_TRADE),
    ("trade", CAT_TRADE),
    ("convert", CAT_TRADE),
    ("exchange", CAT_TRADE),
    ("interest", CAT_EARN),
    ("saving", CAT_EARN),
    ("earn", CAT_EARN),
    ("financial", CAT_EARN),
    ("staking", CAT_EARN),
    ("launchpool", CAT_EARN),
    ("rebate", CAT_REWARD),
    ("reward", CAT_REWARD),
    ("airdrop", CAT_REWARD),
    ("bonus", CAT_REWARD),
    ("promotion", CAT_REWARD),
)


def classify_spot_tax(tax_type: str) -> str:
    lowered = (tax_type or "").lower()
    for keyword, category in SPOT_TYPE_KEYWORDS:
        if keyword in lowered:
            return category
    return CAT_OTHER


def _month(ts: int) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts / 1000))


class LedgerAggregator:
    """Zwija strumień rekordów do miesięcznych sum per moneta i kategoria."""

    def __init__(self, account: str):
        self.account = account
        # (miesiąc, moneta, kategoria) -> [kwota, prowizja, liczba, pierwszy ts]
        self.buckets: Dict[Tuple[str, str, str], List[float]] = defaultdict(
            lambda: [0.0, 0.0, 0, 0]
        )
        self.records = 0
        self.unknown_types: Dict[str, int] = defaultdict(int)
        self.first_ts: Optional[int] = None
        self.last_ts: Optional[int] = None

    def add(self, ts: int, coin: str, category: str, amount: float, fee: float) -> None:
        bucket = self.buckets[(_month(ts), coin, category)]
        bucket[0] += amount
        bucket[1] += fee
        bucket[2] += 1
        bucket[3] = ts if not bucket[3] else min(bucket[3], ts)
        self.records += 1
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def to_summary(self) -> dict:
        """Postać nadająca się do zapisania w pamięci podręcznej."""
        return {
            "b": [
                [month, coin, category, values[0], values[1], values[2], values[3]]
                for (month, coin, category), values in self.buckets.items()
            ],
            "u": dict(self.unknown_types),
        }

    def merge_summary(self, summary: dict) -> None:
        for month, coin, category, amount, fee, count, first in summary.get("b", []):
            bucket = self.buckets[(month, coin, category)]
            bucket[0] += amount
            bucket[1] += fee
            bucket[2] += count
            bucket[3] = first if not bucket[3] else min(bucket[3], first)
            self.records += count
            first = int(first)
            self.first_ts = first if self.first_ts is None else min(self.first_ts, first)
            self.last_ts = first if self.last_ts is None else max(self.last_ts, first)
        for name, count in (summary.get("u") or {}).items():
            self.unknown_types[name] += count

    def entries(self) -> Iterable[LedgerEntry]:
        for (month, coin, category), values in sorted(self.buckets.items()):
            yield LedgerEntry(
                ts=int(values[3]),
                account=self.account,
                coin=coin,
                amount=values[0],
                fee=values[1],
                category=category,
                business_type=f"{category} ({int(values[2])} operacji)",
                entry_id=f"{month}:{coin}:{category}",
            )


def _fetch_windows(
    client: BitgetClient,
    path: str,
    label: str,
    start_ms: int,
    end_ms: int,
    data: Dataset,
    coverage,
    cache: Optional[WindowCache],
    on_rows,
    on_cached,
):
    """Przechodzi okresy od najnowszego do najstarszego, agregując w locie."""
    windows = list(time_windows(start_ms, end_ms, TAX_WINDOW_DAYS))
    windows.reverse()
    total = len(windows)
    guard = window_guard(data, coverage, label)
    started = time.monotonic()
    fetched_windows = 0

    for index, (window_start, window_end) in enumerate(windows):
        period = (
            f"{time.strftime('%Y-%m-%d', time.gmtime(window_start / 1000))} → "
            f"{time.strftime('%Y-%m-%d', time.gmtime(window_end / 1000))}"
        )
        key = cache.key(path, window_start, window_end) if cache is not None else None
        if key is not None:
            cached = cache.get(key)
            if cached is not None:
                on_cached(cached[0] if cached else {})
                log.info("%s: okres %d/%d (%s) - z pamięci podręcznej", label, index + 1, total, period)
                continue

        log.info("%s: pobieram okres %d/%d (%s)...", label, index + 1, total, period)
        try:
            rows = client.fetch_window(
                path,
                {},
                window_start,
                window_end,
                allow_shrink=(index == 0),
                limit=TAX_PAGE_LIMIT,
            )
        except BitgetError as exc:
            guard(exc, window_start, window_end)
            return
        except RuntimeError as exc:
            data.warn(f"{label}: {exc}")
            coverage.error = str(exc)
            return

        summary = on_rows(rows)
        fetched_windows += 1
        if key is not None:
            cache.put(key, [summary])
            cache.save()

        log.info("%s: okres %d/%d (%s) - %d rekordów", label, index + 1, total, period, len(rows))
        _log_estimate(label, started, fetched_windows, total - index - 1)


def _log_estimate(label: str, started: float, done: int, remaining: int) -> None:
    """Po pierwszym okresie mówimy wprost, ile to jeszcze potrwa."""
    if done != 1 or remaining <= 0:
        return
    per_window = time.monotonic() - started
    minutes = per_window * remaining / 60.0
    if minutes >= 2:
        log.warning(
            "%s: pierwszy okres zajął %.0fs. Pozostałe %d okresów to około "
            "%.0f minut. Postęp jest zapisywany po każdym okresie - przerwanie "
            "i wznowienie później nic nie traci.",
            label,
            per_window,
            remaining,
            minutes,
        )


def fetch_spot_records(
    client: BitgetClient,
    cfg: Config,
    data: Dataset,
    start_ms: int,
    cache: Optional[WindowCache] = None,
) -> None:
    """Rejestr transakcji spot, zwijany do miesięcznych sum."""
    coverage = data.coverage_for("rejestr spot (2 lata)")
    aggregator = LedgerAggregator("spot")
    orders: Dict[str, List[LedgerEntry]] = defaultdict(list)
    trade_records = [0]

    def consume(rows: List[dict]) -> dict:
        window = LedgerAggregator("spot")
        for row in rows:
            ts = int(to_float(row.get("ts") or row.get("cTime")))
            if not ts:
                continue
            tax_type = str(row.get("spotTaxType") or row.get("taxType") or "")
            category = classify_spot_tax(tax_type)
            coin = str(row.get("coin", "")).upper()
            amount = to_float(row.get("amount"))
            fee = to_float(row.get("fee"))
            window.add(ts, coin, category, amount, fee)
            if category == CAT_OTHER and tax_type:
                window.unknown_types[tax_type] += 1

            if category == CAT_TRADE:
                trade_records[0] += 1
                order_id = str(row.get("bizOrderId", ""))
                if order_id and trade_records[0] <= TRADE_RECONSTRUCTION_LIMIT:
                    orders[order_id].append(
                        LedgerEntry(
                            ts=ts, account="spot", coin=coin, amount=amount, fee=fee,
                            category=CAT_TRADE, symbol=order_id,
                        )
                    )
        summary = window.to_summary()
        aggregator.merge_summary(summary)
        return summary

    _fetch_windows(
        client, SPOT_TAX_PATH, "Rejestr spot", start_ms, snap(cfg.end_ms),
        data, coverage, cache, consume, aggregator.merge_summary,
    )

    data.spot_ledger.extend(aggregator.entries())
    if aggregator.first_ts:
        coverage.observe(aggregator.first_ts)
        coverage.observe(aggregator.last_ts or aggregator.first_ts)
        coverage.records = aggregator.records

    data.spot_orders = orders
    if trade_records[0] > TRADE_RECONSTRUCTION_LIMIT:
        data.warn(
            f"Konto ma bardzo dużo transakcji spot ({trade_records[0]:,} wpisów w "
            "rejestrze) - typowe przy grid botach. Wynik w rozbiciu na pary liczę "
            "wtedy tylko z ostatnich 90 dni; sumy miesięczne obejmują cały zakres."
            .replace(",", " ")
        )

    if aggregator.unknown_types:
        top = ", ".join(
            f"{name} ({count})" for name, count in sorted(aggregator.unknown_types.items())[:6]
        )
        data.warn(
            f"Nierozpoznane typy operacji spot: {top}. Trafiły do kategorii "
            "'pozostałe' - jeśli to istotne kwoty, zgłoś te nazwy."
        )
    log.info("Rejestr spot: %d wpisów zwiniętych do %d sum miesięcznych.",
             aggregator.records, len(aggregator.buckets))


def fetch_futures_records(
    client: BitgetClient,
    cfg: Config,
    data: Dataset,
    start_ms: int,
    cache: Optional[WindowCache] = None,
) -> None:
    """Rejestr transakcji futures, zwijany do miesięcznych sum."""
    coverage = data.coverage_for("rejestr futures (2 lata)")
    aggregator = LedgerAggregator("futures")

    def consume(rows: List[dict]) -> dict:
        window = LedgerAggregator("futures")
        for row in rows:
            ts = int(to_float(row.get("ts") or row.get("cTime")))
            if not ts:
                continue
            tax_type = str(row.get("futureTaxType") or row.get("taxType") or "")
            window.add(
                ts,
                str(row.get("marginCoin") or row.get("coin") or "").upper(),
                classify_futures(tax_type),
                to_float(row.get("amount")),
                to_float(row.get("fee")),
            )
        summary = window.to_summary()
        aggregator.merge_summary(summary)
        return summary

    _fetch_windows(
        client, FUTURES_TAX_PATH, "Rejestr futures", start_ms, snap(cfg.end_ms),
        data, coverage, cache, consume, aggregator.merge_summary,
    )

    data.futures_ledger.extend(aggregator.entries())
    if aggregator.first_ts:
        coverage.observe(aggregator.first_ts)
        coverage.observe(aggregator.last_ts or aggregator.first_ts)
        coverage.records = aggregator.records
    log.info("Rejestr futures: %d wpisów zwiniętych do %d sum miesięcznych.",
             aggregator.records, len(aggregator.buckets))


# --------------------------------------------------------- odtwarzanie transakcji


def _split_pair(records: List[LedgerEntry]) -> Optional[Tuple[LedgerEntry, LedgerEntry]]:
    received = [entry for entry in records if entry.amount > 0]
    spent = [entry for entry in records if entry.amount < 0]
    if len(received) != 1 or len(spent) != 1:
        return None
    return received[0], spent[0]


def synthesize_fills(data: Dataset, prices: PriceBook, before_ts: Optional[int] = None) -> int:
    """Odtwarza transakcje spot ze sparowanych wpisów rejestru (bizOrderId)."""
    groups = getattr(data, "spot_orders", None) or {}
    added = 0
    skipped = 0

    for order_id, records in groups.items():
        if before_ts is not None and any(entry.ts >= before_ts for entry in records):
            continue
        pair = _split_pair(records)
        if pair is None:
            skipped += 1
            continue
        received, spent = pair

        if spent.coin in QUOTE_COINS and received.coin not in QUOTE_COINS:
            side, base, quote = "buy", received, spent
        elif received.coin in QUOTE_COINS and spent.coin not in QUOTE_COINS:
            side, base, quote = "sell", spent, received
        elif spent.coin in STABLECOINS:
            side, base, quote = "buy", received, spent
        elif received.coin in STABLECOINS:
            side, base, quote = "sell", spent, received
        else:
            skipped += 1
            continue

        size = abs(base.amount)
        quote_amount = abs(quote.amount)
        if size <= 0 or quote_amount <= 0:
            skipped += 1
            continue

        fee_entry = received if received.fee else spent
        data.fills.append(
            Fill(
                ts=max(received.ts, spent.ts),
                symbol=f"{base.coin}{quote.coin}",
                base=base.coin,
                quote=quote.coin,
                side=side,
                price=quote_amount / size,
                size=size,
                quote_amount=quote_amount,
                fee=fee_entry.fee,
                fee_coin=fee_entry.coin,
                trade_id=f"tax:{order_id}",
                order_id=order_id,
            )
        )
        added += 1

    if added:
        coverage = data.coverage_for("transakcje odtworzone z rejestru")
        for fill in data.fills:
            if fill.trade_id.startswith("tax:"):
                coverage.observe(fill.ts)
    if skipped:
        data.warn(
            f"Nie udało się odtworzyć {skipped} zleceń spot z rejestru podatkowego "
            "(nietypowy układ wpisów) - ich wynik nie wchodzi do rozbicia wg pary."
        )
    log.info("Odtworzono %d transakcji spot z rejestru (pominięto %d).", added, skipped)
    return added


def oldest_fill_ts(fills: Iterable[Fill]) -> Optional[int]:
    stamps = [fill.ts for fill in fills if not fill.trade_id.startswith("tax:")]
    return min(stamps) if stamps else None
