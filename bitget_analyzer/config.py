"""Konfiguracja: wczytywanie sekretów z .env / zmiennych środowiskowych."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_BASE_URL = "https://api.bitget.com"

# Typy kontraktów, dla których pobieramy historię rachunku futures.
ALL_PRODUCT_TYPES = ["USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES"]


class ConfigError(RuntimeError):
    """Brakująca lub błędna konfiguracja."""


def load_dotenv(path: Path) -> Dict[str, str]:
    """Minimalny parser .env (bez zewnętrznych zależności).

    Nie nadpisuje zmiennych już obecnych w środowisku - zmienna z shella
    ma pierwszeństwo nad plikiem.
    """
    loaded: Dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


@dataclass
class Config:
    """Wszystkie ustawienia jednego uruchomienia analizy."""

    api_key: str
    api_secret: str
    api_passphrase: str

    start: datetime
    end: datetime

    base_url: str = DEFAULT_BASE_URL
    out_dir: Path = Path("raport")

    # Waluta rozliczeniowa analizy. Bitget wycenia wszystko w USDT.
    quote: str = "USDT"
    fx_rate: float = 1.0
    fx_label: str = "USDT"

    product_types: List[str] = field(default_factory=lambda: list(ALL_PRODUCT_TYPES))
    transfer_coins: Optional[List[str]] = None
    extra_flows: Optional[Path] = None

    skip: List[str] = field(default_factory=list)

    # Format CSV - domyślnie zgodny z polskim Excelem.
    csv_sep: str = ";"
    csv_decimal: str = ","

    # Sieć / limity
    timeout: float = 30.0
    max_retries: int = 5
    requests_per_second: float = 8.0
    page_limit: int = 100

    verbose: bool = False

    @property
    def start_ms(self) -> int:
        return int(self.start.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end.timestamp() * 1000)

    def enabled(self, module: str) -> bool:
        return module not in self.skip

    def fmt_money(self, value: float) -> str:
        """Formatuje kwotę w walucie wyświetlania (domyślnie USDT)."""
        amount = f"{value * self.fx_rate:,.2f}".replace(",", "\u00a0")
        return f"{amount} {self.fx_label}"


def _parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if end_of_day and fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=timezone.utc)
    raise ConfigError(
        f"Nie rozumiem daty '{value}'. Użyj formatu YYYY-MM-DD (np. 2024-01-01)."
    )


def create_config(
    credentials,
    *,
    since: Optional[str] = None,
    to: Optional[str] = None,
    out: str = "raport",
    fx_rate: Optional[float] = None,
    fx_label: Optional[str] = None,
    product_types: str = ",".join(ALL_PRODUCT_TYPES),
    transfer_coins: Optional[str] = None,
    extra_flows: Optional[str] = None,
    skip: str = "",
    rps: float = 8.0,
    csv_sep: str = ";",
    csv_decimal: str = ",",
    verbose: bool = False,
) -> Config:
    """Buduje Config z gotowych danych - używane przez CLI i przez panel web."""
    if credentials is None or not credentials.is_complete():
        raise ConfigError(
            "Brak kluczy API. Wprowadź je w panelu (python3 panel.py) albo "
            "ustaw BITGET_API_KEY / BITGET_API_SECRET / BITGET_API_PASSPHRASE."
        )

    now = datetime.now(timezone.utc)
    end = _parse_date(to, end_of_day=True) if to else now
    if since:
        start = _parse_date(since)
    elif os.environ.get("BITGET_START"):
        start = _parse_date(os.environ["BITGET_START"])
    else:
        start = end - timedelta(days=365)
    if start >= end:
        raise ConfigError("Data początkowa musi być wcześniejsza niż końcowa.")

    rate = float(fx_rate or os.environ.get("BITGET_FX_RATE") or 1.0)
    label = fx_label or os.environ.get("BITGET_FX_LABEL") or "USDT"
    if rate <= 0:
        raise ConfigError("Kurs przeliczeniowy musi być liczbą dodatnią.")

    types = [p.strip().upper() for p in product_types.split(",") if p.strip()]
    unknown = [p for p in types if p not in ALL_PRODUCT_TYPES]
    if unknown:
        raise ConfigError(
            f"Nieznany productType: {', '.join(unknown)}. "
            f"Dozwolone: {', '.join(ALL_PRODUCT_TYPES)}"
        )

    coins = None
    if transfer_coins:
        coins = [c.strip().upper() for c in transfer_coins.split(",") if c.strip()]

    return Config(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        api_passphrase=credentials.api_passphrase,
        start=start,
        end=end,
        base_url=os.environ.get("BITGET_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        out_dir=Path(out),
        fx_rate=rate,
        fx_label=label,
        product_types=types,
        transfer_coins=coins,
        extra_flows=Path(extra_flows) if extra_flows else None,
        skip=[s.strip().lower() for s in (skip or "").split(",") if s.strip()],
        csv_sep=csv_sep,
        csv_decimal=csv_decimal,
        requests_per_second=rps,
        verbose=verbose,
    )


def build_config(args, env_file: Path = Path(".env")) -> Config:
    """Config dla CLI: .env / zmienne środowiskowe, a w razie ich braku
    klucze zapisane w panelu."""
    load_dotenv(env_file)

    from .secrets_store import SecretsError, resolve_credentials

    try:
        credentials = resolve_credentials()
    except SecretsError as exc:
        raise ConfigError(str(exc)) from exc

    return create_config(
        credentials,
        since=args.since,
        to=args.to,
        out=args.out,
        fx_rate=args.fx_rate,
        fx_label=args.fx_label,
        product_types=args.product_types,
        transfer_coins=args.transfer_coins,
        extra_flows=args.extra_flows,
        skip=args.skip,
        rps=args.rps,
        csv_sep=args.csv_sep,
        csv_decimal=args.csv_decimal,
        verbose=args.verbose,
    )
