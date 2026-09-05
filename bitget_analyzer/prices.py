"""Wyceny: kursy bieżące, kursy historyczne (dzienne) i metadane par spot.

Wpłaty/wypłaty wyceniamy kursem z DNIA operacji - inaczej "realny zysk"
byłby zafałszowany zmianą ceny monety po fakcie.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from .client import BitgetClient, extract_list
from .model import to_float

log = logging.getLogger("bitget.prices")

# Monety traktowane jako 1 USD (z dokładnością wystarczającą do tej analizy).
STABLECOINS = {
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDD", "USDE", "PYUSD", "USD",
}

DAY_MS = 24 * 60 * 60 * 1000

# Monety, które zmieniły nazwę - stara nazwa nie ma już notowań, więc bez tego
# operacje sprzed zmiany wyceniałyby się na zero.
COIN_ALIASES = {
    "MATIC": "POL",
    "LUNA2": "LUNA",
    "BTCB": "BTC",
    "WETH": "ETH",
    "WBTC": "BTC",
    "BEP20USDT": "USDT",
}


class PriceBook:
    """Cache kursów: bieżących (tickery) i historycznych (świece dzienne)."""

    def __init__(self, client: BitgetClient, cache_path: Optional[Path] = None):
        self.client = client
        self.cache_path = cache_path
        self._current: Dict[str, float] = {}
        self._daily: Dict[str, float] = {}      # "COIN:YYYY-MM-DD" -> cena
        self._symbols: Dict[str, Tuple[str, str]] = {}  # symbol -> (base, quote)
        self._missing: set = set()
        self._load_cache()

    # ------------------------------------------------------------------ cache

    def _load_cache(self) -> None:
        if self.cache_path and self.cache_path.is_file():
            try:
                self._daily = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._daily = {}

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._daily, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            log.debug("Nie udało się zapisać cache kursów: %s", exc)

    # --------------------------------------------------------------- symbole

    def load_symbols(self) -> None:
        """Pobiera listę par spot, żeby poprawnie rozbić symbol na base/quote."""
        try:
            rows = extract_list(
                self.client.request("GET", "/api/v2/spot/public/symbols", auth=False)
            )
        except Exception as exc:
            log.warning("Nie udało się pobrać listy par spot: %s", exc)
            return
        for row in rows:
            symbol = row.get("symbol")
            base = row.get("baseCoin")
            quote = row.get("quoteCoin")
            if symbol and base and quote:
                self._symbols[symbol.upper()] = (base.upper(), quote.upper())
        log.debug("Załadowano %d par spot.", len(self._symbols))

    def split_symbol(self, symbol: str) -> Tuple[str, str]:
        """Rozbija np. 'BTCUSDT' na ('BTC', 'USDT')."""
        symbol = (symbol or "").upper()
        if symbol in self._symbols:
            return self._symbols[symbol]
        for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "EUR", "BRL", "TRY"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return symbol[: -len(quote)], quote
        return symbol, "USDT"

    # ------------------------------------------------------- kursy bieżące

    def load_current(self) -> None:
        """Jednym zapytaniem pobiera kursy wszystkich par spot."""
        try:
            rows = extract_list(
                self.client.request("GET", "/api/v2/spot/market/tickers", auth=False)
            )
        except Exception as exc:
            log.warning("Nie udało się pobrać tickerów: %s", exc)
            return
        for row in rows:
            symbol = (row.get("symbol") or "").upper()
            price = to_float(row.get("lastPr") or row.get("close"))
            if symbol and price > 0:
                self._current[symbol] = price
        log.debug("Załadowano %d kursów bieżących.", len(self._current))

    def current(self, coin: str) -> Optional[float]:
        """Bieżąca cena monety w USDT."""
        coin = (coin or "").upper()
        if not coin:
            return None
        if coin in STABLECOINS:
            return 1.0
        alias = COIN_ALIASES.get(coin)
        if alias:
            return self.current(alias)
        direct = self._current.get(f"{coin}USDT")
        if direct:
            return direct
        via_usdc = self._current.get(f"{coin}USDC")
        if via_usdc:
            return via_usdc
        # Monety kwotowane w BTC/ETH (rzadkie, ale się zdarzają).
        for bridge in ("BTC", "ETH"):
            cross = self._current.get(f"{coin}{bridge}")
            bridge_price = self._current.get(f"{bridge}USDT")
            if cross and bridge_price:
                return cross * bridge_price
        return None

    # --------------------------------------------------- kursy historyczne

    def at(self, coin: str, ts_ms: int) -> Tuple[float, str]:
        """Cena monety w USDT w dniu `ts_ms`.

        Zwraca (cena, źródło). Źródło: 'stable' | 'candle' | 'current' | 'brak'.
        """
        coin = (coin or "").upper()
        if not coin:
            return 0.0, "brak"
        if coin in STABLECOINS:
            return 1.0, "stable"

        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        cache_key = f"{coin}:{day}"
        if cache_key in self._daily:
            cached = self._daily[cache_key]
            return (cached, "candle") if cached > 0 else (0.0, "brak")

        price = self._fetch_daily_close(coin, ts_ms)
        if price is None:
            alias = COIN_ALIASES.get(coin)
            if alias:
                price = self._fetch_daily_close(alias, ts_ms)
        if price is not None:
            self._daily[cache_key] = price
            return price, "candle"

        self._daily[cache_key] = 0.0
        fallback = self.current(coin)
        if fallback:
            if coin not in self._missing:
                self._missing.add(coin)
                log.warning(
                    "Brak kursu historycznego %s - używam kursu bieżącego "
                    "(wycena tej operacji jest przybliżona).",
                    coin,
                )
            return fallback, "current"
        if coin not in self._missing:
            self._missing.add(coin)
            log.warning(
                "Brak jakiegokolwiek kursu dla %s - wyceniam na 0. "
                "Jeśli trzymasz tę monetę w istotnej ilości, jej wartość "
                "nie wejdzie do rozbicia (w wycenie portfela jest, bo tam "
                "kwoty podaje sam Bitget).",
                coin,
            )
        return 0.0, "brak"

    def _fetch_daily_close(self, coin: str, ts_ms: int) -> Optional[float]:
        """Zamknięcie świecy dziennej z dnia operacji."""
        day_end = (ts_ms // DAY_MS + 1) * DAY_MS
        attempts = [(f"{coin}USDT", None), (f"{coin}USDC", None)]
        if coin != "BTC":
            attempts.append((f"{coin}BTC", "BTC"))
        for symbol, bridge in attempts:
            try:
                rows = self.client.request(
                    "GET",
                    "/api/v2/spot/market/history-candles",
                    {
                        "symbol": symbol,
                        "granularity": "1day",
                        "endTime": day_end,
                        "limit": 1,
                    },
                    auth=False,
                )
            except Exception:
                continue
            candles = rows if isinstance(rows, list) else []
            if not candles or not isinstance(candles[0], list) or len(candles[0]) < 5:
                continue
            close = to_float(candles[0][4])
            if close <= 0:
                continue
            if bridge is None:
                return close
            bridge_price, _ = self.at(bridge, ts_ms)
            if bridge_price > 0:
                return close * bridge_price
        return None

    def value_now(self, coin: str, amount: float) -> float:
        """Wartość `amount` monet po kursie bieżącym."""
        price = self.current(coin)
        return amount * price if price else 0.0

    @property
    def missing_coins(self) -> set:
        return set(self._missing)
