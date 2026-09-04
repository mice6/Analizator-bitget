"""Wspólna ścieżka pobierania i liczenia - używana przez CLI i przez panel web."""

from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

from .analysis import Analysis, Analyzer
from .cache import WindowCache
from .client import BitgetClient
from .config import Config
from .earn import fetch_savings_history
from .futures import fetch_closed_positions, fetch_futures_bills
from .model import Dataset
from .prices import PriceBook
from .limits import API_HISTORY_DAYS, effective_start
from .spot import coins_seen, fetch_spot_bills, fetch_spot_fills
from .tax import (
    fetch_futures_records,
    fetch_spot_records,
    oldest_fill_ts,
    synthesize_fills,
)
from .valuation import fetch_equity
from .wallet import fetch_deposits, fetch_transfers, fetch_withdrawals, load_extra_flows

log = logging.getLogger("bitget.pipeline")

# (klucz modułu, opis pokazywany użytkownikowi)
STEPS = [
    ("prices", "Kursy i lista par"),
    ("wallet", "Historia wpłat i wypłat"),
    ("spot", "Rejestr transakcji spot"),
    ("futures", "Rejestr transakcji futures"),
    ("fills", "Szczegóły transakcji spot"),
    ("positions", "Zamknięte pozycje futures"),
    ("earn", "Produkty Earn"),
    ("transfers", "Transfery wewnętrzne"),
    ("equity", "Aktualna wycena portfela"),
    ("analysis", "Przeliczanie wyniku"),
]

ProgressFn = Optional[Callable[[str, str, int, int], None]]


def _noop(step: str, label: str, index: int, total: int) -> None:
    log.info("[%d/%d] %s", index, total, label)


def collect(
    cfg: Config,
    client: BitgetClient,
    prices: PriceBook,
    progress: ProgressFn = None,
    cache: Optional[WindowCache] = None,
) -> Dataset:
    """Pobiera z API wszystko, czego potrzebuje analiza."""
    report = progress or _noop
    data = Dataset()
    total = len(STEPS)

    def step(key: str, index: int) -> bool:
        label = dict(STEPS)[key]
        report(key, label, index, total)
        return cfg.enabled(key)

    if step("prices", 1):
        prices.load_symbols()
        prices.load_current()

    if step("wallet", 2):
        fetch_deposits(client, cfg, prices, data, cache)
        fetch_withdrawals(client, cfg, prices, data, cache)

    # Rejestry podatkowe sięgają ~2 lat wstecz - to podstawowe źródło historii.
    # Księgi rachunków (90 dni) uruchamiamy tylko wtedy, gdy rejestr zawiódł.
    if step("spot", 3):
        if cfg.enabled("rejestry"):
            fetch_spot_records(client, cfg, data, effective_start(cfg, API_HISTORY_DAYS), cache)
        if not data.spot_ledger:
            log.info("Sięgam po księgę rachunku spot (90 dni, szybszy endpoint).")
            fetch_spot_bills(client, cfg, data, cache)

    if step("futures", 4):
        if cfg.enabled("rejestry"):
            fetch_futures_records(
                client, cfg, data, effective_start(cfg, API_HISTORY_DAYS), cache
            )
        if not data.futures_ledger:
            log.info("Sięgam po księgę rachunku futures.")
            fetch_futures_bills(client, cfg, data, cache)

    if step("fills", 5):
        fetch_spot_fills(client, cfg, prices, data, cache)
        # Starsze zlecenia odtwarzamy z rejestru; granicą jest najstarsza
        # transakcja pobrana dokładnym endpointem, żeby nic nie policzyć dwa razy.
        synthesize_fills(data, prices, before_ts=oldest_fill_ts(data.fills))

    if step("positions", 6):
        fetch_closed_positions(client, cfg, data, cache)

    if step("earn", 7):
        fetch_savings_history(client, cfg, data, cache)

    if step("transfers", 8):
        fetch_transfers(client, cfg, data, coins_seen(data), cache)

    if cfg.extra_flows:
        load_extra_flows(cfg.extra_flows, prices, data)

    step("equity", 9)
    fetch_equity(client, cfg, prices, data)

    return data


def run(
    cfg: Config, progress: ProgressFn = None
) -> Tuple[Dataset, Analysis, PriceBook]:
    """Pełny przebieg: pobranie danych + przeliczenie wyniku."""
    client = BitgetClient(cfg)
    client.sync_time()
    prices = PriceBook(client, cache_path=cfg.out_dir / "price_cache.json")
    cache = WindowCache(cfg.out_dir / "cache_okresow.json")
    try:
        data = collect(cfg, client, prices, progress, cache)
    finally:
        prices.save_cache()
        cache.save()
        if cache.hits:
            log.info(
                "Z pamięci podręcznej: %d okresów (oszczędzone zapytania).", cache.hits
            )

    (progress or _noop)("analysis", "Przeliczanie wyniku", len(STEPS), len(STEPS))
    analysis = Analyzer(data, prices).build()
    log.info(
        "Wykonano %d żądań do API (%d ponowień).", client.request_count, client.retry_count
    )
    return data, analysis, prices
