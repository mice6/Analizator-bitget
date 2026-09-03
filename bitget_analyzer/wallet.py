"""Przepływy zewnętrzne (wpłaty/wypłaty) i transfery wewnętrzne."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .client import BitgetClient, dedupe
from .limits import SHORT_HISTORY_DAYS, effective_start, window_guard
from .config import Config
from .model import Dataset, ExternalFlow, Transfer, to_float
from .prices import PriceBook

log = logging.getLogger("bitget.wallet")

DEPOSIT_PATH = "/api/v2/spot/wallet/deposit-records"
WITHDRAW_PATH = "/api/v2/spot/wallet/withdrawal-records"
TRANSFER_PATH = "/api/v2/spot/account/transferRecords"

# Limity okien czasowych narzucone przez API.
WALLET_WINDOW_DAYS = 89
TRANSFER_WINDOW_DAYS = 89

SUCCESS_STATUSES = {"success", "successful", "finish", "finished", "completed"}

# Po tylu pustych okresach z rzędu (~5 lat) przestajemy szukać wstecz.
# Chroni przed przemiataniem dekad, gdy ktoś poda zakres "od 2000 roku".
EMPTY_WINDOWS_LIMIT = 20


def _ts(row: dict) -> int:
    for key in ("cTime", "createTime", "ts", "uTime", "updateTime"):
        value = row.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _is_success(row: dict) -> bool:
    status = str(row.get("status", "")).lower()
    return status in SUCCESS_STATUSES or status == ""


def fetch_deposits(client: BitgetClient, cfg: Config, prices: PriceBook, data: Dataset) -> None:
    """Historia wpłat (środki przychodzące z zewnątrz)."""
    coverage = data.coverage_for("wpłaty")
    rows = dedupe(
        client.paginate_windows(
            DEPOSIT_PATH,
            {},
            cfg.start_ms,
            cfg.end_ms,
            WALLET_WINDOW_DAYS,
            stop_after_empty=EMPTY_WINDOWS_LIMIT,
            on_window_error=window_guard(data, coverage, "Wpłaty"),
        ),
        "orderId",
        "tradeId",
    )

    for row in rows:
        ts = _ts(row)
        if not ts:
            continue
        coin = str(row.get("coin", "")).upper()
        amount = to_float(row.get("size") or row.get("amount"))
        if amount <= 0:
            continue
        if not _is_success(row):
            data.warn(
                f"Pominięto niezakończoną wpłatę {amount} {coin} "
                f"(status: {row.get('status')})."
            )
            continue
        price, source = prices.at(coin, ts)
        flow = ExternalFlow(
            ts=ts,
            direction="deposit",
            coin=coin,
            amount=amount,
            usd_value=amount * price,
            price=price,
            price_source=source,
            tx_id=str(row.get("tradeId") or row.get("orderId") or ""),
            status=str(row.get("status", "")),
        )
        data.deposits.append(flow)
        coverage.observe(ts)
    log.info("Wpłaty: %d rekordów.", len(data.deposits))


def fetch_withdrawals(client: BitgetClient, cfg: Config, prices: PriceBook, data: Dataset) -> None:
    """Historia wypłat (środki wychodzące poza giełdę)."""
    coverage = data.coverage_for("wypłaty")
    rows = dedupe(
        client.paginate_windows(
            WITHDRAW_PATH,
            {},
            cfg.start_ms,
            cfg.end_ms,
            WALLET_WINDOW_DAYS,
            stop_after_empty=EMPTY_WINDOWS_LIMIT,
            on_window_error=window_guard(data, coverage, "Wypłaty"),
        ),
        "orderId",
    )

    for row in rows:
        ts = _ts(row)
        if not ts:
            continue
        coin = str(row.get("coin", "")).upper()
        gross = to_float(row.get("size") or row.get("amount"))
        fee = abs(to_float(row.get("fee")))
        if gross <= 0:
            continue
        if not _is_success(row):
            data.warn(
                f"Pominięto niezakończoną wypłatę {gross} {coin} "
                f"(status: {row.get('status')})."
            )
            continue
        # Na konto zewnętrzne trafia kwota po odjęciu prowizji; prowizja jest
        # realnym kosztem, więc nie liczymy jej jako "odzyskanego kapitału".
        net = max(gross - fee, 0.0)
        price, source = prices.at(coin, ts)
        flow = ExternalFlow(
            ts=ts,
            direction="withdraw",
            coin=coin,
            amount=net,
            fee=fee,
            usd_value=net * price,
            price=price,
            price_source=source,
            tx_id=str(row.get("tradeId") or row.get("orderId") or ""),
            status=str(row.get("status", "")),
        )
        data.withdrawals.append(flow)
        coverage.observe(ts)
    log.info("Wypłaty: %d rekordów.", len(data.withdrawals))


def fetch_transfers(
    client: BitgetClient, cfg: Config, data: Dataset, coins: Optional[List[str]] = None
) -> None:
    """Transfery wewnętrzne (Funding <-> Spot <-> Futures <-> Earn).

    Endpoint wymaga podania monety, więc odpytujemy go dla każdej monety,
    która pojawiła się w danych (albo z listy podanej przez użytkownika).
    """
    coverage = data.coverage_for("transfery wewnętrzne")
    candidates = cfg.transfer_coins or coins or ["USDT"]
    seen_ids = set()
    start_ms = effective_start(cfg, SHORT_HISTORY_DAYS)
    for coin in sorted({c.upper() for c in candidates if c}):
        rows = dedupe(
            client.paginate_windows(
                TRANSFER_PATH,
                {"coin": coin},
                start_ms,
                cfg.end_ms,
                TRANSFER_WINDOW_DAYS,
                limit=500,
                on_window_error=window_guard(data, coverage, f"Transfery {coin}"),
            ),
            "transferId",
        )

        for row in rows:
            ts = _ts(row)
            if not ts:
                continue
            transfer_id = str(row.get("transferId", ""))
            if transfer_id and transfer_id in seen_ids:
                continue
            seen_ids.add(transfer_id)
            transfer = Transfer(
                ts=ts,
                coin=str(row.get("coin", coin)).upper(),
                amount=to_float(row.get("size") or row.get("amount")),
                from_type=str(row.get("fromType", "")),
                to_type=str(row.get("toType", "")),
                transfer_id=str(row.get("transferId", "")),
                status=str(row.get("status", "")),
            )
            data.transfers.append(transfer)
            coverage.observe(ts)
    log.info("Transfery wewnętrzne: %d rekordów.", len(data.transfers))


def load_extra_flows(path: Path, prices: PriceBook, data: Dataset) -> None:
    """Ręczne uzupełnienie przepływów, których API już nie zwraca.

    Format CSV (nagłówek wymagany):
        data,typ,moneta,ilosc,wartosc_usd
        2023-02-11,deposit,USDT,1000,
        2023-08-04,withdraw,BTC,0.05,1420.50

    Kolumna `wartosc_usd` jest opcjonalna - jeśli pusta, skrypt wyceni
    operację kursem z podanego dnia.
    """
    if not path.is_file():
        data.warn(f"Nie znaleziono pliku z dodatkowymi przepływami: {path}")
        return

    added = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        handle.seek(0)
        # Akceptujemy zarówno CSV z przecinkiem, jak i eksport z polskiego Excela.
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        for row in csv.DictReader(handle, delimiter=delimiter):
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            raw_date = normalized.get("data") or normalized.get("date")
            direction = (normalized.get("typ") or normalized.get("type") or "").lower()
            coin = (normalized.get("moneta") or normalized.get("coin") or "").upper()
            amount = to_float(normalized.get("ilosc") or normalized.get("amount"))
            if not raw_date or direction not in ("deposit", "withdraw") or amount <= 0:
                data.warn(f"Pominięto wiersz w {path.name}: {row}")
                continue
            try:
                ts = int(
                    datetime.strptime(raw_date[:10], "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                    * 1000
                )
            except ValueError:
                data.warn(f"Zła data w {path.name}: {raw_date}")
                continue

            explicit = to_float(normalized.get("wartosc_usd") or normalized.get("usd_value"))
            if explicit > 0:
                price, source = (explicit / amount if amount else 0.0), "ręczna"
                usd_value = explicit
            else:
                price, source = prices.at(coin, ts)
                usd_value = amount * price

            flow = ExternalFlow(
                ts=ts,
                direction=direction,
                coin=coin,
                amount=amount,
                usd_value=usd_value,
                price=price,
                price_source=source,
                source="manual",
            )
            (data.deposits if direction == "deposit" else data.withdrawals).append(flow)
            added += 1
    if added:
        data.warn(
            f"Doliczono {added} ręcznie podanych przepływów z pliku {path.name}."
        )
    log.info("Dodatkowe przepływy z pliku: %d.", added)
