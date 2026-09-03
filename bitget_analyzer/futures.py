"""Pobieranie danych z Futures (USDT-M / COIN-M / USDC-M)."""

from __future__ import annotations

import logging

from .client import BitgetClient, dedupe
from .limits import (
    API_HISTORY_DAYS,
    SHORT_HISTORY_DAYS,
    effective_start,
    window_guard,
)
from .config import Config
from .model import (
    CAT_FUNDING,
    CAT_LIQUIDATION,
    CAT_OTHER,
    CAT_REWARD,
    CAT_TRADE,
    CAT_TRANSFER,
    Dataset,
    LedgerEntry,
    to_float,
)

log = logging.getLogger("bitget.futures")

BILL_PATH = "/api/v2/mix/account/bill"
POSITION_HISTORY_PATH = "/api/v2/mix/position/history-position"

# API pozwala odpytywać rachunek futures maksymalnie w oknach 30-dniowych.
BILL_WINDOW_DAYS = 30
POSITION_WINDOW_DAYS = 30


def _classify(business_type: str) -> str:
    bt = (business_type or "").lower()
    if "trans_" in bt or bt in ("transfer_in", "transfer_out"):
        return CAT_TRANSFER
    if "settle_fee" in bt or "funding" in bt:
        return CAT_FUNDING
    if "burst" in bt or "liquidat" in bt or "adl" in bt:
        return CAT_LIQUIDATION
    if "bonus" in bt or "rebate" in bt or "reward" in bt or "airdrop" in bt:
        return CAT_REWARD
    if any(token in bt for token in ("open", "close", "delivery", "settle", "pos")):
        return CAT_TRADE
    return CAT_OTHER


def fetch_futures_bills(client: BitgetClient, cfg: Config, data: Dataset) -> None:
    """Księga rachunku futures - awaryjnie, gdy rejestr podatkowy zawiedzie."""
    coverage = data.coverage_for("księga futures")
    seen_ids = set()
    start_ms = effective_start(cfg, API_HISTORY_DAYS)
    for product_type in cfg.product_types:
        rows = dedupe(
            client.paginate_windows(
                BILL_PATH,
                {"productType": product_type},
                start_ms,
                cfg.end_ms,
                BILL_WINDOW_DAYS,
                on_window_error=window_guard(data, coverage, f"Księga futures {product_type}"),
            ),
            "billId",
        )

        for row in rows:
            ts = int(to_float(row.get("cTime") or row.get("ctime")))
            if not ts:
                continue
            bill_id = str(row.get("billId", ""))
            if bill_id and bill_id in seen_ids:
                continue
            seen_ids.add(bill_id)
            business = str(row.get("businessType", ""))
            entry = LedgerEntry(
                ts=ts,
                account="futures",
                coin=str(row.get("coin") or row.get("marginCoin") or "").upper(),
                amount=to_float(row.get("amount")),
                fee=to_float(row.get("fee")) + to_float(row.get("feeByCoupon")),
                category=_classify(business),
                business_type=business,
                symbol=str(row.get("symbol", "")),
                product_type=product_type,
                entry_id=str(row.get("billId", "")),
            )
            data.futures_ledger.append(entry)
            coverage.observe(ts)
    log.info("Księga futures: %d wpisów.", len(data.futures_ledger))


def fetch_closed_positions(client: BitgetClient, cfg: Config, data: Dataset) -> None:
    """Historia zamkniętych pozycji - kontrola krzyżowa dla wyniku futures.

    Bitget udostępnia ten endpoint tylko dla ostatnich ~3 miesięcy, więc
    traktujemy go jako weryfikację, a nie główne źródło.
    """
    coverage = data.coverage_for("zamknięte pozycje futures")
    seen_ids = set()
    start_ms = effective_start(cfg, SHORT_HISTORY_DAYS)
    for product_type in cfg.product_types:
        rows = dedupe(
            client.paginate_windows(
                POSITION_HISTORY_PATH,
                {"productType": product_type},
                start_ms,
                cfg.end_ms,
                POSITION_WINDOW_DAYS,
                on_window_error=window_guard(
                    data, coverage, f"Historia pozycji {product_type}"
                ),
            ),
            "positionId",
        )

        for row in rows:
            ts = int(to_float(row.get("utime") or row.get("uTime") or row.get("ctime")))
            if not ts:
                continue
            position_id = str(row.get("positionId", ""))
            if position_id and position_id in seen_ids:
                continue
            seen_ids.add(position_id)
            row = dict(row)
            row["_ts"] = ts
            row["_productType"] = product_type
            data.closed_positions.append(row)
            coverage.observe(ts)
    log.info("Zamknięte pozycje futures: %d.", len(data.closed_positions))
