"""Earn: aktualne salda produktów oszczędnościowych i historia odsetek."""

from __future__ import annotations

import logging

from .client import BitgetClient, BitgetError, dedupe
from .config import Config
from .model import CAT_EARN, CAT_TRANSFER, Dataset, LedgerEntry, to_float

log = logging.getLogger("bitget.earn")

SAVINGS_ASSETS_PATH = "/api/v2/earn/savings/assets"
SAVINGS_RECORDS_PATH = "/api/v2/earn/savings/records"

EARN_WINDOW_DAYS = 90
PERIOD_TYPES = ("flexible", "fixed")

# Nazwy pól, pod którymi Bitget zwraca naliczone odsetki (zależnie od produktu).
INTEREST_FIELDS = ("interest", "profit", "totalProfit", "totalInterest", "earnings")


def _pick(row: dict, *names: str) -> float:
    for name in names:
        if row.get(name) not in (None, ""):
            return to_float(row.get(name))
    return 0.0


def fetch_savings_history(client: BitgetClient, cfg: Config, data: Dataset) -> None:
    """Historia produktów Earn: subskrypcje, wykupy i naliczone odsetki."""
    coverage = data.coverage_for("earn")
    seen_ids = set()

    for period_type in PERIOD_TYPES:
        # Stan pozycji (zawiera narosłe odsetki dla części produktów).
        try:
            positions = dedupe(
                client.paginate_windows(
                    SAVINGS_ASSETS_PATH,
                    {"periodType": period_type},
                    cfg.start_ms,
                    cfg.end_ms,
                    EARN_WINDOW_DAYS,
                ),
                "orderId",
                "productId",
            )
        except BitgetError as exc:
            if exc.is_permission_error:
                coverage.error = exc.msg
                data.warn(f"Brak uprawnień do danych Earn: {exc.msg}")
                return
            positions = []
            log.info("Earn (%s) - pozycje niedostępne: %s", period_type, exc.msg)

        for row in positions:
            interest = _pick(row, *INTEREST_FIELDS)
            ts = int(to_float(row.get("cTime") or row.get("ctime")))
            if interest <= 0 or not ts:
                continue
            marker = ("asset", str(row.get("orderId") or row.get("productId") or ""), ts)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            data.earn_ledger.append(
                LedgerEntry(
                    ts=ts,
                    account="earn",
                    coin=str(row.get("productCoin") or row.get("coin") or "").upper(),
                    amount=interest,
                    category=CAT_EARN,
                    business_type=f"savings_{period_type}_interest",
                    entry_id=str(row.get("orderId") or row.get("productId") or ""),
                )
            )
            coverage.observe(ts)

        # Historia operacji (subskrypcja / wykup / wypłata odsetek).
        try:
            records = dedupe(
                client.paginate_windows(
                    SAVINGS_RECORDS_PATH,
                    {"periodType": period_type},
                    cfg.start_ms,
                    cfg.end_ms,
                    EARN_WINDOW_DAYS,
                ),
                "orderId",
            )
        except BitgetError as exc:
            log.info("Earn (%s) - historia niedostępna: %s", period_type, exc.msg)
            continue

        for row in records:
            ts = int(to_float(row.get("cTime") or row.get("ctime")))
            if not ts:
                continue
            marker = ("record", str(row.get("orderId", "")), ts)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            order_type = str(
                row.get("orderType") or row.get("type") or row.get("status") or ""
            ).lower()
            interest = _pick(row, *INTEREST_FIELDS)
            if interest > 0 or "interest" in order_type or "profit" in order_type:
                amount = interest or to_float(row.get("amount"))
                category = CAT_EARN
            else:
                # Subskrypcja/wykup to przesunięcie środków, nie zysk.
                amount = to_float(row.get("amount"))
                category = CAT_TRANSFER
            data.earn_ledger.append(
                LedgerEntry(
                    ts=ts,
                    account="earn",
                    coin=str(row.get("productCoin") or row.get("coin") or "").upper(),
                    amount=amount,
                    category=category,
                    business_type=f"savings_{period_type}_{order_type or 'record'}",
                    entry_id=str(row.get("orderId", "")),
                )
            )
            coverage.observe(ts)

    log.info("Earn: %d wpisów.", len(data.earn_ledger))
    if not data.earn_ledger:
        data.warn(
            "Brak historii Earn z API. Odsetki dopisane bezpośrednio na Spot są "
            "i tak ujęte w księdze spot (grupa 'financial')."
        )
