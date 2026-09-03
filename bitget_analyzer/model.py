"""Struktury danych używane w całej analizie."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Kategorie wpisów w ujednoliconej księdze.
CAT_TRADE = "trade"
CAT_FEE = "fee"
CAT_FUNDING = "funding"
CAT_TRANSFER = "transfer"
CAT_DEPOSIT = "deposit"
CAT_WITHDRAW = "withdraw"
CAT_REWARD = "reward"
CAT_EARN = "earn"
CAT_LIQUIDATION = "liquidation"
CAT_OTHER = "other"


def to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def month_key(ms: int) -> str:
    return to_dt(ms).strftime("%Y-%m")


def to_float(value, default: float = 0.0) -> float:
    """Bitget zwraca liczby jako stringi, czasem puste.

    Dodatkowo akceptuje zapis z polskiego Excela ("1 234,56") - przydaje się
    przy ręcznie uzupełnianym pliku z dodatkowymi przepływami.
    """
    if value is None or value == "":
        return default
    if isinstance(value, str):
        text = value.strip().replace("\u00a0", "").replace(" ", "")
        if "," in text:
            text = text.replace(",", "") if "." in text else text.replace(",", ".")
        value = text
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ExternalFlow:
    """Wpłata lub wypłata poza giełdę - realny kapitał wniesiony/wyjęty."""

    ts: int
    direction: str  # "deposit" | "withdraw"
    coin: str
    amount: float           # ilość monety, która faktycznie zmieniła właściciela
    fee: float = 0.0        # opłata sieciowa/giełdowa (tylko wypłaty)
    usd_value: float = 0.0  # wycena w USDT wg kursu z DNIA operacji
    price: float = 0.0
    price_source: str = ""
    tx_id: str = ""
    status: str = ""
    source: str = "api"     # "api" | "manual" (plik z dodatkowymi przepływami)

    @property
    def month(self) -> str:
        return month_key(self.ts)

    @property
    def signed_usd(self) -> float:
        """Dodatnie = kapitał wpłacony na giełdę, ujemne = wyjęty."""
        return self.usd_value if self.direction == "deposit" else -self.usd_value


@dataclass
class Transfer:
    """Transfer wewnętrzny między kontami - NIE jest zyskiem ani stratą."""

    ts: int
    coin: str
    amount: float
    from_type: str
    to_type: str
    transfer_id: str = ""
    status: str = ""

    @property
    def month(self) -> str:
        return month_key(self.ts)


@dataclass
class LedgerEntry:
    """Pojedynczy ruch na rachunku (spot / futures / earn)."""

    ts: int
    account: str          # spot | futures | earn
    coin: str
    amount: float         # zmiana salda bez opłaty
    fee: float = 0.0      # opłata (zwykle ujemna)
    category: str = CAT_OTHER
    business_type: str = ""
    symbol: str = ""
    product_type: str = ""
    entry_id: str = ""

    @property
    def month(self) -> str:
        return month_key(self.ts)

    @property
    def delta(self) -> float:
        return self.amount + self.fee


@dataclass
class Fill:
    """Wykonana transakcja spot (do liczenia zrealizowanego P&L)."""

    ts: int
    symbol: str
    base: str
    quote: str
    side: str          # buy | sell
    price: float       # cena w walucie kwotowanej
    size: float        # ilość base
    quote_amount: float
    fee: float         # ujemna
    fee_coin: str
    trade_id: str = ""
    order_id: str = ""

    @property
    def month(self) -> str:
        return month_key(self.ts)


@dataclass
class RealizedTrade:
    """Zamknięcie (część) pozycji spot metodą średniej ważonej ceny nabycia."""

    ts: int
    symbol: str
    base: str
    size: float
    proceeds_usd: float
    cost_usd: float
    fee_usd: float
    uncovered_size: float = 0.0  # sprzedaż bez znanej historii zakupu

    @property
    def pnl(self) -> float:
        return self.proceeds_usd - self.cost_usd + self.fee_usd

    @property
    def month(self) -> str:
        return month_key(self.ts)


@dataclass
class EquitySnapshot:
    """Aktualna wycena wszystkich aktywów (w USDT)."""

    ts: int
    by_account: Dict[str, float] = field(default_factory=dict)
    positions: List[dict] = field(default_factory=list)
    source: str = "all-account-balance"

    @property
    def total(self) -> float:
        return sum(self.by_account.values())


@dataclass
class SourceCoverage:
    """Ile danych faktycznie udało się pobrać z danego źródła."""

    name: str
    records: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    error: str = ""

    def observe(self, ts: int) -> None:
        self.records += 1
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts


@dataclass
class Dataset:
    """Komplet surowych danych pobranych z API."""

    deposits: List[ExternalFlow] = field(default_factory=list)
    withdrawals: List[ExternalFlow] = field(default_factory=list)
    transfers: List[Transfer] = field(default_factory=list)
    spot_ledger: List[LedgerEntry] = field(default_factory=list)
    futures_ledger: List[LedgerEntry] = field(default_factory=list)
    earn_ledger: List[LedgerEntry] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)
    closed_positions: List[dict] = field(default_factory=list)
    equity: Optional[EquitySnapshot] = None
    # Zlecenia spot pogrupowane po bizOrderId - do odtwarzania transakcji.
    spot_orders: Dict[str, List[LedgerEntry]] = field(default_factory=dict)
    coverage: Dict[str, SourceCoverage] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def coverage_for(self, name: str) -> SourceCoverage:
        return self.coverage.setdefault(name, SourceCoverage(name))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def external_flows(self) -> List[ExternalFlow]:
        return sorted(self.deposits + self.withdrawals, key=lambda f: f.ts)
