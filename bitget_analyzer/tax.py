"""Rejestry transakcyjne Bitget (tax records) - historia do ~2 lat wstecz.

Endpointy `/api/v2/spot/account/bills` i `/api/v2/spot/trade/fills` oddają
tylko ostatnie ~90 dni. Rejestry podatkowe sięgają znacznie dalej i to one są
głównym źródłem historii; księgi rachunków zostają jako awaryjne uzupełnienie.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from .client import BitgetClient, dedupe
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

# Monety, które w parze pełnią rolę waluty kwotowanej.
QUOTE_COINS = set(STABLECOINS) | {"BTC", "ETH", "BGB", "EUR", "BRL", "TRY"}

# Słowa kluczowe w polu spotTaxType -> kategoria. Bitget bywa niekonsekwentny
# w nazewnictwie, więc dopasowujemy po fragmencie, a nie po pełnej nazwie.
SPOT_TYPE_KEYWORDS = (
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


def fetch_spot_records(client: BitgetClient, cfg: Config, data: Dataset, start_ms: int) -> None:
    """Rejestr transakcji spot - każdy ruch monety z typem operacji."""
    coverage = data.coverage_for("rejestr spot (2 lata)")
    rows = dedupe(
        client.paginate_windows(
            SPOT_TAX_PATH,
            {},
            start_ms,
            cfg.end_ms,
            TAX_WINDOW_DAYS,
            limit=TAX_PAGE_LIMIT,
            on_window_error=window_guard(data, coverage, "Rejestr spot"),
        ),
        "id",
    )

    unknown: Dict[str, int] = defaultdict(int)
    for row in rows:
        ts = int(to_float(row.get("ts") or row.get("cTime")))
        if not ts:
            continue
        tax_type = str(row.get("spotTaxType") or row.get("taxType") or "")
        category = classify_spot_tax(tax_type)
        if category == CAT_OTHER and tax_type:
            unknown[tax_type] += 1
        data.spot_ledger.append(
            LedgerEntry(
                ts=ts,
                account="spot",
                coin=str(row.get("coin", "")).upper(),
                amount=to_float(row.get("amount")),
                fee=to_float(row.get("fee")),
                category=category,
                business_type=tax_type,
                entry_id=str(row.get("id", "")),
                symbol=str(row.get("bizOrderId", "")),
            )
        )
        coverage.observe(ts)

    if unknown:
        top = ", ".join(f"{name} ({count})" for name, count in sorted(unknown.items())[:6])
        data.warn(
            f"Nierozpoznane typy operacji spot: {top}. Trafiły do kategorii "
            "'pozostałe' - jeśli to istotne kwoty, zgłoś te nazwy."
        )
    log.info("Rejestr spot: %d wpisów.", len(rows))


def fetch_futures_records(client: BitgetClient, cfg: Config, data: Dataset, start_ms: int) -> None:
    """Rejestr transakcji futures - P&L pozycji, funding i prowizje."""
    coverage = data.coverage_for("rejestr futures (2 lata)")
    rows = dedupe(
        client.paginate_windows(
            FUTURES_TAX_PATH,
            {},
            start_ms,
            cfg.end_ms,
            TAX_WINDOW_DAYS,
            limit=TAX_PAGE_LIMIT,
            on_window_error=window_guard(data, coverage, "Rejestr futures"),
        ),
        "id",
    )

    for row in rows:
        ts = int(to_float(row.get("ts") or row.get("cTime")))
        if not ts:
            continue
        tax_type = str(row.get("futureTaxType") or row.get("taxType") or "")
        data.futures_ledger.append(
            LedgerEntry(
                ts=ts,
                account="futures",
                coin=str(row.get("marginCoin") or row.get("coin") or "").upper(),
                amount=to_float(row.get("amount")),
                fee=to_float(row.get("fee")),
                category=classify_futures(tax_type),
                business_type=tax_type,
                symbol=str(row.get("symbol", "")),
                entry_id=str(row.get("id", "")),
            )
        )
        coverage.observe(ts)
    log.info("Rejestr futures: %d wpisów.", len(rows))


# --------------------------------------------------------- odtwarzanie transakcji


def _split_pair(records: List[LedgerEntry]) -> Optional[Tuple[LedgerEntry, LedgerEntry]]:
    """Z jednego zlecenia robi parę (otrzymane, wydane)."""
    received = [entry for entry in records if entry.amount > 0]
    spent = [entry for entry in records if entry.amount < 0]
    if len(received) != 1 or len(spent) != 1:
        return None
    return received[0], spent[0]


def synthesize_fills(data: Dataset, prices: PriceBook, before_ts: Optional[int] = None) -> int:
    """Odtwarza transakcje spot z rejestru podatkowego.

    Każde zlecenie zostawia w rejestrze dwa wpisy o wspólnym `bizOrderId`:
    monetę otrzymaną i wydaną. To wystarczy, by odtworzyć parę, kierunek,
    wolumen i cenę - a więc policzyć zrealizowany wynik również dla okresu
    starszego niż 90 dni, których nie oddaje endpoint transakcji.

    `before_ts` chroni przed podwójnym liczeniem: pomija okres pokryty już
    dokładniejszymi danymi z `/api/v2/spot/trade/fills`.
    """
    groups: Dict[str, List[LedgerEntry]] = defaultdict(list)
    for entry in data.spot_ledger:
        if entry.category != CAT_TRADE or not entry.symbol:
            continue
        if before_ts is not None and entry.ts >= before_ts:
            continue
        groups[entry.symbol].append(entry)

    added = 0
    skipped = 0
    for order_id, records in groups.items():
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

        # Prowizja bywa pobierana w monecie kupowanej albo w kwotowanej.
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
    """Najstarsza transakcja z dokładnego endpointu - granica dla odtwarzania."""
    stamps = [fill.ts for fill in fills if not fill.trade_id.startswith("tax:")]
    return min(stamps) if stamps else None
