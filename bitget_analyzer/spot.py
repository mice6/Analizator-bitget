"""Pobieranie danych ze Spota: księga rachunku (bills) i transakcje (fills)."""

from __future__ import annotations

import json
import logging
from typing import List

from .client import BitgetClient, dedupe
from .limits import SHORT_HISTORY_DAYS, effective_start, window_guard
from .config import Config
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
from .prices import PriceBook

log = logging.getLogger("bitget.spot")

BILLS_PATH = "/api/v2/spot/account/bills"
FILLS_PATH = "/api/v2/spot/trade/fills"

SPOT_WINDOW_DAYS = 89

# groupType ze Spota -> kategoria w naszej księdze.
GROUP_CATEGORY = {
    "deposit": CAT_DEPOSIT,
    "withdraw": CAT_WITHDRAW,
    "transaction": CAT_TRADE,
    "transfer": CAT_TRANSFER,
    "financial": CAT_EARN,
    "strategy": CAT_TRADE,   # boty (grid/martingale) rozliczane na spocie
    "convert": CAT_TRADE,
    "loan": CAT_OTHER,
    "fiat": CAT_OTHER,
    "c2c": CAT_OTHER,
    "pre_c2c": CAT_OTHER,
    "on_chain": CAT_OTHER,
    "other": CAT_OTHER,
}

REWARD_BUSINESS_TYPES = {
    "REBATE_REWARDS",
    "AIRDROP_REWARDS",
    "USDT_CONTRACT_REWARDS",
    "MIX_CONTRACT_REWARDS",
}


def fetch_spot_bills(client: BitgetClient, cfg: Config, data: Dataset) -> None:
    """Księga rachunku spot - używana awaryjnie, gdy rejestr podatkowy zawiedzie."""
    coverage = data.coverage_for("księga spot (90 dni)")
    rows = dedupe(
        client.paginate_windows(
            BILLS_PATH,
            {},
            effective_start(cfg, SHORT_HISTORY_DAYS),
            cfg.end_ms,
            SPOT_WINDOW_DAYS,
            label="Księga spot",
            on_window_error=window_guard(data, coverage, "Księga spot"),
        ),
        "billId",
    )

    for row in rows:
        ts = int(to_float(row.get("cTime")))
        if not ts:
            continue
        group = str(row.get("groupType", "")).lower()
        business = str(row.get("businessType", ""))
        category = GROUP_CATEGORY.get(group, CAT_OTHER)
        if business.upper() in REWARD_BUSINESS_TYPES:
            category = CAT_REWARD

        entry = LedgerEntry(
            ts=ts,
            account="spot",
            coin=str(row.get("coin", "")).upper(),
            amount=to_float(row.get("size")),
            fee=to_float(row.get("fees")),
            category=category,
            business_type=business,
            entry_id=str(row.get("billId", "")),
        )
        data.spot_ledger.append(entry)
        coverage.observe(ts)
    log.info("Księga spot: %d wpisów.", len(data.spot_ledger))


def _parse_fee_detail(raw) -> tuple:
    """feeDetail bywa dictem albo stringiem z JSON-em."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return 0.0, ""
    if not isinstance(raw, dict):
        return 0.0, ""
    fee = to_float(raw.get("totalFee"))
    coin = str(raw.get("feeCoin", "")).upper()
    return fee, coin


def fetch_spot_fills(
    client: BitgetClient, cfg: Config, prices: PriceBook, data: Dataset
) -> None:
    """Historia wykonanych transakcji spot - podstawa do zrealizowanego P&L."""
    coverage = data.coverage_for("transakcje spot (90 dni)")
    rows = dedupe(
        client.paginate_windows(
            FILLS_PATH,
            {},
            effective_start(cfg, SHORT_HISTORY_DAYS),
            cfg.end_ms,
            SPOT_WINDOW_DAYS,
            label="Transakcje spot",
            on_window_error=window_guard(data, coverage, "Transakcje spot"),
        ),
        "tradeId",
    )

    for row in rows:
        ts = int(to_float(row.get("cTime")))
        if not ts:
            continue
        symbol = str(row.get("symbol", "")).upper()
        base, quote = prices.split_symbol(symbol)
        price = to_float(row.get("priceAvg") or row.get("price"))
        size = to_float(row.get("size"))
        quote_amount = to_float(row.get("amount")) or price * size
        fee, fee_coin = _parse_fee_detail(row.get("feeDetail"))

        data.fills.append(
            Fill(
                ts=ts,
                symbol=symbol,
                base=base,
                quote=quote,
                side=str(row.get("side", "")).lower(),
                price=price,
                size=size,
                quote_amount=quote_amount,
                fee=fee,
                fee_coin=fee_coin or quote,
                trade_id=str(row.get("tradeId", "")),
                order_id=str(row.get("orderId", "")),
            )
        )
        coverage.observe(ts)
    log.info("Transakcje spot: %d rekordów.", len(data.fills))


MAX_TRANSFER_COINS = 25


def coins_seen(data: Dataset) -> List[str]:
    """Monety do sprawdzenia w historii transferów.

    Endpoint transferów wymaga podania monety, więc pytamy przede wszystkim
    o te, które faktycznie pojawiły się we wpisach typu 'transfer'. Dopiero
    gdy takich nie ma, sięgamy po wszystkie widziane monety.
    """
    from_transfers = {
        entry.coin
        for entry in data.spot_ledger + data.futures_ledger
        if entry.category == CAT_TRANSFER and entry.coin
    }
    coins = from_transfers or {
        entry.coin for entry in data.spot_ledger + data.futures_ledger if entry.coin
    } | {flow.coin for flow in data.external_flows if flow.coin}
    coins.add("USDT")

    if len(coins) > MAX_TRANSFER_COINS:
        data.warn(
            f"Sprawdzam transfery tylko dla {MAX_TRANSFER_COINS} z {len(coins)} monet. "
            "Pełną listę wskaż opcją --transfer-coins."
        )
        ordered = ["USDT"] + [c for c in sorted(coins) if c != "USDT"]
        return ordered[:MAX_TRANSFER_COINS]
    return sorted(coins)
