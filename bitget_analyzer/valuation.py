"""Aktualna wycena wszystkich aktywów (Spot + Futures + Earn + Funding + boty)."""

from __future__ import annotations

import logging
import time
from typing import Dict, List

from .client import BitgetClient, BitgetError, extract_list
from .config import Config
from .model import Dataset, EquitySnapshot, to_float
from .prices import PriceBook

log = logging.getLogger("bitget.valuation")

ALL_BALANCE_PATH = "/api/v2/account/all-account-balance"
SPOT_ASSETS_PATH = "/api/v2/spot/account/assets"
FUNDING_ASSETS_PATH = "/api/v2/account/funding-assets"
MIX_ACCOUNTS_PATH = "/api/v2/mix/account/accounts"
EARN_ASSETS_PATH = "/api/v2/earn/account/assets"


def _positions_from_spot(rows: List[dict], prices: PriceBook, account: str) -> List[dict]:
    out = []
    for row in rows:
        coin = str(row.get("coin", "")).upper()
        amount = (
            to_float(row.get("available"))
            + to_float(row.get("frozen"))
            + to_float(row.get("locked"))
        )
        if amount <= 0:
            continue
        out.append(
            {
                "konto": account,
                "moneta": coin,
                "ilosc": amount,
                "kurs_usdt": prices.current(coin) or 0.0,
                "wartosc_usdt": prices.value_now(coin, amount),
            }
        )
    return out


def fetch_equity(
    client: BitgetClient, cfg: Config, prices: PriceBook, data: Dataset
) -> EquitySnapshot:
    """Buduje migawkę wartości portfela na teraz."""
    snapshot = EquitySnapshot(ts=int(time.time() * 1000))

    # Główne źródło: zbiorcze saldo wszystkich kont w USDT.
    try:
        rows = extract_list(client.request("GET", ALL_BALANCE_PATH))
        for row in rows:
            account_type = str(row.get("accountType", "")).lower()
            snapshot.by_account[account_type] = to_float(row.get("usdtBalance"))
    except BitgetError as exc:
        data.warn(f"Nie pobrano zbiorczego salda kont: {exc.msg}")
        snapshot.source = "składane z poszczególnych kont"

    # Rozbicie na monety - do CSV i do kontroli.
    try:
        spot_rows = extract_list(client.request("GET", SPOT_ASSETS_PATH))
        snapshot.positions += _positions_from_spot(spot_rows, prices, "spot")
    except BitgetError as exc:
        data.warn(f"Nie pobrano aktywów spot: {exc.msg}")

    try:
        funding_rows = extract_list(client.request("GET", FUNDING_ASSETS_PATH))
        snapshot.positions += _positions_from_spot(funding_rows, prices, "funding")
    except BitgetError as exc:
        log.info("Aktywa funding niedostępne: %s", exc.msg)

    for product_type in cfg.product_types:
        try:
            rows = extract_list(
                client.request("GET", MIX_ACCOUNTS_PATH, {"productType": product_type})
            )
        except BitgetError as exc:
            log.info("Konto futures %s niedostępne: %s", product_type, exc.msg)
            continue
        for row in rows:
            coin = str(row.get("marginCoin", "")).upper()
            equity = to_float(row.get("accountEquity"))
            usdt_equity = to_float(row.get("usdtEquity")) or prices.value_now(coin, equity)
            if equity == 0 and usdt_equity == 0:
                continue
            snapshot.positions.append(
                {
                    "konto": f"futures:{product_type}",
                    "moneta": coin,
                    "ilosc": equity,
                    "kurs_usdt": prices.current(coin) or 0.0,
                    "wartosc_usdt": usdt_equity,
                }
            )

    try:
        earn_rows = extract_list(client.request("GET", EARN_ASSETS_PATH))
        for row in earn_rows:
            coin = str(row.get("coin", "")).upper()
            amount = to_float(row.get("amount"))
            if amount <= 0:
                continue
            snapshot.positions.append(
                {
                    "konto": "earn",
                    "moneta": coin,
                    "ilosc": amount,
                    "kurs_usdt": prices.current(coin) or 0.0,
                    "wartosc_usdt": prices.value_now(coin, amount),
                }
            )
    except BitgetError as exc:
        log.info("Aktywa Earn niedostępne: %s", exc.msg)

    # Fallback: jeśli zbiorcze saldo nie zadziałało, sumujemy pozycje.
    if not snapshot.by_account and snapshot.positions:
        aggregated: Dict[str, float] = {}
        for position in snapshot.positions:
            key = str(position["konto"]).split(":")[0]
            aggregated[key] = aggregated.get(key, 0.0) + float(position["wartosc_usdt"])
        snapshot.by_account = aggregated

    if not snapshot.by_account:
        data.warn(
            "Nie udało się ustalić aktualnej wartości portfela - wynik końcowy "
            "będzie niepełny. Sprawdź uprawnienia klucza API."
        )

    data.equity = snapshot
    log.info("Wycena portfela: %.2f USDT", snapshot.total)
    return snapshot
