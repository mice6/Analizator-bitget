"""Przeliczanie surowych danych na wynik: zrealizowany P&L i bilans miesięczny."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import (
    CAT_EARN,
    CAT_FUNDING,
    CAT_LIQUIDATION,
    CAT_REWARD,
    CAT_TRADE,
    CAT_TRANSFER,
    Dataset,
    LedgerEntry,
    RealizedTrade,
    month_key,
    to_float,
)
from .prices import STABLECOINS, PriceBook

log = logging.getLogger("bitget.analysis")


@dataclass
class Position:
    """Stan posiadania monety wraz z średnią ceną nabycia (w USDT)."""

    qty: float = 0.0
    cost: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty > 1e-12 else 0.0


@dataclass
class SymbolResult:
    symbol: str
    base: str = ""
    realized_pnl: float = 0.0
    fees: float = 0.0
    trades: int = 0
    bought_usd: float = 0.0
    sold_usd: float = 0.0
    uncovered_size: float = 0.0


@dataclass
class MonthRow:
    month: str
    deposits: float = 0.0
    withdrawals: float = 0.0
    spot_realized: float = 0.0
    spot_fees: float = 0.0
    futures_pnl: float = 0.0
    futures_funding: float = 0.0
    futures_fees: float = 0.0
    earn_income: float = 0.0
    rewards: float = 0.0
    other: float = 0.0

    @property
    def net_external(self) -> float:
        """Kapitał netto wniesiony w tym miesiącu (wpłaty - wypłaty)."""
        return self.deposits - self.withdrawals

    @property
    def attributed(self) -> float:
        """Suma wyjaśnionego wyniku w miesiącu (bez zmian wyceny posiadanych monet)."""
        return (
            self.spot_realized
            + self.futures_pnl
            + self.futures_funding
            + self.futures_fees
            + self.earn_income
            + self.rewards
            + self.other
        )


@dataclass
class Analysis:
    equity_now: float = 0.0
    equity_by_account: Dict[str, float] = field(default_factory=dict)
    deposits_total: float = 0.0
    withdrawals_total: float = 0.0
    real_pnl: float = 0.0
    roi: Optional[float] = None

    months: List[MonthRow] = field(default_factory=list)
    symbols: List[SymbolResult] = field(default_factory=list)
    realized_trades: List[RealizedTrade] = field(default_factory=list)
    inventory: Dict[str, Position] = field(default_factory=dict)

    spot_realized_total: float = 0.0
    spot_fees_total: float = 0.0
    spot_unrealized: float = 0.0
    futures_pnl_total: float = 0.0
    futures_funding_total: float = 0.0
    futures_fees_total: float = 0.0
    earn_income_total: float = 0.0
    rewards_total: float = 0.0
    other_total: float = 0.0

    transfers_count: int = 0
    transfers_volume: float = 0.0
    uncovered_symbols: List[str] = field(default_factory=list)

    @property
    def attributed_total(self) -> float:
        return sum(month.attributed for month in self.months)

    @property
    def unexplained(self) -> float:
        """Różnica między twardym wynikiem a sumą wyjaśnionych składników."""
        return self.real_pnl - (self.attributed_total + self.spot_unrealized)


class Analyzer:
    """Liczy zrealizowany P&L spot metodą średniej ceny nabycia i składa bilans."""

    def __init__(self, data: Dataset, prices: PriceBook):
        self.data = data
        self.prices = prices
        self._usd_cache: Dict[str, float] = {}
        self._realized: List[RealizedTrade] = []
        self._symbols: List[SymbolResult] = []
        self._uncovered: List[str] = []

    # ------------------------------------------------------------ przeliczenia

    def to_usd(self, coin: str, amount: float, ts: int) -> float:
        """Wartość kwoty w USDT wg kursu z dnia operacji."""
        if amount == 0:
            return 0.0
        coin = (coin or "").upper()
        if coin in STABLECOINS:
            return amount
        key = f"{coin}:{ts // 86_400_000}"
        if key not in self._usd_cache:
            price, _ = self.prices.at(coin, ts)
            self._usd_cache[key] = price
        return amount * self._usd_cache[key]

    # ------------------------------------------------- zrealizowany P&L spot

    def compute_spot_realized(self) -> Dict[str, Position]:
        """Średnia ważona cena nabycia na monetę; sprzedaż realizuje wynik.

        Zakupy podnoszą podstawę kosztową, sprzedaże realizują różnicę między
        ceną sprzedaży a średnim kosztem. Prowizje są wliczone w wynik.
        """
        positions: Dict[str, Position] = defaultdict(Position)
        per_symbol: Dict[str, SymbolResult] = {}
        uncovered: set = set()

        for fill in sorted(self.data.fills, key=lambda f: f.ts):
            if fill.size <= 0 or not fill.base:
                continue
            result = per_symbol.setdefault(
                fill.symbol, SymbolResult(symbol=fill.symbol, base=fill.base)
            )
            result.trades += 1

            quote_usd = self.to_usd(fill.quote, fill.quote_amount, fill.ts)
            fee_usd = self.to_usd(fill.fee_coin, fill.fee, fill.ts) if fill.fee else 0.0
            result.fees += abs(fee_usd)
            position = positions[fill.base]

            if fill.side == "buy":
                result.bought_usd += quote_usd
                position.qty += fill.size
                position.cost += quote_usd
                if fill.fee and fill.fee_coin == fill.base:
                    # Prowizja pobrana w kupowanej monecie - dostajemy mniej sztuk.
                    position.qty += fill.fee
                else:
                    position.cost -= fee_usd  # fee_usd jest ujemne
                continue

            if fill.side != "sell":
                continue

            result.sold_usd += quote_usd
            covered = min(fill.size, max(position.qty, 0.0))
            uncovered_size = fill.size - covered
            if uncovered_size > 1e-10:
                uncovered.add(fill.symbol)
                result.uncovered_size += uncovered_size

            price_per_unit = quote_usd / fill.size if fill.size else 0.0
            cost_usd = position.avg_cost * covered
            proceeds_usd = price_per_unit * covered
            trade = RealizedTrade(
                ts=fill.ts,
                symbol=fill.symbol,
                base=fill.base,
                size=fill.size,
                proceeds_usd=proceeds_usd,
                cost_usd=cost_usd,
                fee_usd=fee_usd,
                uncovered_size=uncovered_size,
            )
            position.qty -= covered
            position.cost = max(position.cost - cost_usd, 0.0)
            if position.qty <= 1e-12:
                position.qty = 0.0
                position.cost = 0.0

            result.realized_pnl += trade.pnl
            self._realized.append(trade)

        self._symbols = sorted(
            per_symbol.values(), key=lambda item: item.realized_pnl
        )
        self._uncovered = sorted(uncovered)
        return dict(positions)

    def _futures_from_positions(self, row_for) -> None:
        """Wynik futures odtworzony z historii zamkniętych pozycji.

        `netProfit` zawiera już prowizje i funding, więc rozbijamy go na
        składniki, żeby nic nie policzyć dwa razy.
        """
        count = 0
        for position in self.data.closed_positions:
            ts = int(position.get("_ts") or 0)
            if not ts:
                continue
            coin = str(position.get("marginCoin") or "USDT").upper()
            net = to_float(position.get("netProfit"))
            fees = to_float(position.get("openFee")) + to_float(position.get("closeFee"))
            funding = to_float(position.get("totalFunding"))
            gross = net - fees - funding

            row = row_for(month_key(ts))
            row.futures_pnl += self.to_usd(coin, gross, ts)
            row.futures_fees += self.to_usd(coin, fees, ts)
            row.futures_funding += self.to_usd(coin, funding, ts)
            count += 1

        if count:
            self.data.warn(
                f"Wynik futures policzony z {count} zamkniętych pozycji "
                "(księga rachunku nic nie zwróciła). Historia pozycji obejmuje "
                "tylko ostatnie ~90 dni, więc starsze miesiące mogą być puste."
            )
            log.info("Futures: wynik odtworzony z %d zamkniętych pozycji.", count)

    # ------------------------------------------------------- bilans miesięczny

    def _ledger_rows(self) -> List[LedgerEntry]:
        return self.data.spot_ledger + self.data.futures_ledger + self.data.earn_ledger

    def build(self) -> Analysis:
        self._realized = []
        self._symbols = []
        self._uncovered = []

        analysis = Analysis()
        months: Dict[str, MonthRow] = {}

        def row_for(month: str) -> MonthRow:
            return months.setdefault(month, MonthRow(month=month))

        # 1. Przepływy zewnętrzne - wycena z dnia operacji.
        for flow in self.data.external_flows:
            row = row_for(flow.month)
            if flow.direction == "deposit":
                row.deposits += flow.usd_value
                analysis.deposits_total += flow.usd_value
            else:
                row.withdrawals += flow.usd_value
                analysis.withdrawals_total += flow.usd_value

        # 2. Zrealizowany wynik spot z historii transakcji.
        inventory = self.compute_spot_realized()
        for trade in self._realized:
            row = row_for(trade.month)
            row.spot_realized += trade.pnl
            row.spot_fees += abs(trade.fee_usd)
        if not self.data.fills and any(
            entry.category == CAT_TRADE for entry in self.data.spot_ledger
        ):
            self.data.warn(
                "Brak historii transakcji spot (fills), a w księdze spot są operacje "
                "handlowe - zrealizowany wynik spot nie został policzony. "
                "Uruchom bez '--skip fills'."
            )
        analysis.realized_trades = self._realized
        analysis.symbols = self._symbols
        analysis.uncovered_symbols = self._uncovered
        analysis.inventory = {
            coin: position for coin, position in inventory.items() if position.qty > 1e-10
        }

        # Prowizje od zakupów siedzą w podstawie kosztowej - dodajemy je do
        # licznika kosztów, żeby raport pokazywał pełną kwotę opłat.
        buy_fees = sum(
            abs(self.to_usd(fill.fee_coin, fill.fee, fill.ts))
            for fill in self.data.fills
            if fill.side == "buy" and fill.fee
        )
        analysis.spot_fees_total = sum(month.spot_fees for month in months.values()) + buy_fees

        # Niezrealizowany wynik na monetach, które nadal trzymamy.
        for coin, position in analysis.inventory.items():
            current_value = self.prices.value_now(coin, position.qty)
            if current_value:
                analysis.spot_unrealized += current_value - position.cost

        # 3. Futures: P&L pozycji, funding i prowizje (transfery pomijamy).
        for entry in self.data.futures_ledger:
            if entry.category == CAT_TRANSFER:
                continue
            row = row_for(entry.month)
            amount_usd = self.to_usd(entry.coin, entry.amount, entry.ts)
            fee_usd = self.to_usd(entry.coin, entry.fee, entry.ts)
            row.futures_fees += fee_usd
            if entry.category == CAT_FUNDING:
                row.futures_funding += amount_usd
            elif entry.category in (CAT_TRADE, CAT_LIQUIDATION):
                row.futures_pnl += amount_usd
            elif entry.category == CAT_REWARD:
                row.rewards += amount_usd
            else:
                row.other += amount_usd

        # 3b. Gdy księga futures nic nie dała, sięgamy po zamknięte pozycje.
        # Historia pozycji obejmuje ~90 dni, ale to lepsze niż zero.
        if not any(
            entry.category in (CAT_TRADE, CAT_FUNDING, CAT_LIQUIDATION)
            for entry in self.data.futures_ledger
        ) and self.data.closed_positions:
            self._futures_from_positions(row_for)

        # 4. Spot - pozycje spoza handlu (odsetki, nagrody, korekty).
        for entry in self.data.spot_ledger:
            if entry.category in (CAT_TRANSFER, CAT_TRADE):
                continue
            if entry.category in ("deposit", "withdraw"):
                continue  # ujęte w przepływach zewnętrznych
            row = row_for(entry.month)
            amount_usd = self.to_usd(entry.coin, entry.amount, entry.ts)
            if entry.category == CAT_EARN:
                row.earn_income += amount_usd
            elif entry.category == CAT_REWARD:
                row.rewards += amount_usd
            else:
                row.other += amount_usd

        # 5. Earn - odsetki z dedykowanych endpointów.
        for entry in self.data.earn_ledger:
            if entry.category != CAT_EARN:
                continue
            row = row_for(entry.month)
            row.earn_income += self.to_usd(entry.coin, entry.amount, entry.ts)

        # 6. Transfery wewnętrzne - tylko do kontroli, nie wpływają na wynik.
        analysis.transfers_count = len(self.data.transfers)
        analysis.transfers_volume = sum(
            abs(self.to_usd(transfer.coin, transfer.amount, transfer.ts))
            for transfer in self.data.transfers
        )

        analysis.months = [months[key] for key in sorted(months)]
        analysis.spot_realized_total = sum(m.spot_realized for m in analysis.months)
        analysis.futures_pnl_total = sum(m.futures_pnl for m in analysis.months)
        analysis.futures_funding_total = sum(m.futures_funding for m in analysis.months)
        analysis.futures_fees_total = sum(m.futures_fees for m in analysis.months)
        analysis.earn_income_total = sum(m.earn_income for m in analysis.months)
        analysis.rewards_total = sum(m.rewards for m in analysis.months)
        analysis.other_total = sum(m.other for m in analysis.months)

        # 7. Twardy wynik: ile realnie zostało w portfelu vs. ile włożono.
        if self.data.equity:
            analysis.equity_now = self.data.equity.total
            analysis.equity_by_account = dict(self.data.equity.by_account)
        analysis.real_pnl = (
            analysis.equity_now - analysis.deposits_total + analysis.withdrawals_total
        )
        if analysis.deposits_total > 0:
            analysis.roi = analysis.real_pnl / analysis.deposits_total

        return analysis
