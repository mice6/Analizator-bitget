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


def build_config(args, env_file: Path = Path(".env")) -> Config:
    """Składa Config z pliku .env, zmiennych środowiskowych i argumentów CLI."""
    load_dotenv(env_file)

    key = os.environ.get("BITGET_API_KEY", "").strip()
    secret = os.environ.get("BITGET_API_SECRET", "").strip()
    passphrase = os.environ.get("BITGET_API_PASSPHRASE", "").strip()

    missing = [
        name
        for name, value in (
            ("BITGET_API_KEY", key),
            ("BITGET_API_SECRET", secret),
            ("BITGET_API_PASSPHRASE", passphrase),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Brak zmiennych środowiskowych: "
            + ", ".join(missing)
            + ".\nSkopiuj .env.example do .env i uzupełnij klucz API (read-only)."
        )

    now = datetime.now(timezone.utc)
    end = _parse_date(args.to, end_of_day=True) if args.to else now
    if args.since:
        start = _parse_date(args.since)
    elif os.environ.get("BITGET_START"):
        start = _parse_date(os.environ["BITGET_START"])
    else:
        start = end - timedelta(days=365)
    if start >= end:
        raise ConfigError("Data --od musi być wcześniejsza niż --do.")

    fx_rate = float(args.fx_rate or os.environ.get("BITGET_FX_RATE") or 1.0)
    fx_label = args.fx_label or os.environ.get("BITGET_FX_LABEL") or "USDT"
    if fx_rate <= 0:
        raise ConfigError("--fx-rate musi być liczbą dodatnią.")

    product_types = [p.strip().upper() for p in args.product_types.split(",") if p.strip()]
    unknown = [p for p in product_types if p not in ALL_PRODUCT_TYPES]
    if unknown:
        raise ConfigError(
            f"Nieznany productType: {', '.join(unknown)}. Dozwolone: {', '.join(ALL_PRODUCT_TYPES)}"
        )

    transfer_coins = None
    if args.transfer_coins:
        transfer_coins = [c.strip().upper() for c in args.transfer_coins.split(",") if c.strip()]

    return Config(
        api_key=key,
        api_secret=secret,
        api_passphrase=passphrase,
        start=start,
        end=end,
        base_url=os.environ.get("BITGET_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        out_dir=Path(args.out),
        fx_rate=fx_rate,
        fx_label=fx_label,
        product_types=product_types,
        transfer_coins=transfer_coins,
        extra_flows=Path(args.extra_flows) if args.extra_flows else None,
        skip=[s.strip().lower() for s in (args.skip or "").split(",") if s.strip()],
        csv_sep=args.csv_sep,
        csv_decimal=args.csv_decimal,
        requests_per_second=args.rps,
        verbose=args.verbose,
    )
