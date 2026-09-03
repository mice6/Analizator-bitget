"""Obsługa limitów historii API - wspólna dla wszystkich kolektorów."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import BitgetError, is_range_error
from .model import Dataset, SourceCoverage, to_dt

log = logging.getLogger("bitget.limits")

# Bitget udostępnia dane transakcyjne mniej więcej za ostatnie 2 lata.
API_HISTORY_DAYS = 730

# Endpointy ksiąg i transakcji sięgają tylko ~90 dni; dajemy mały zapas.
SHORT_HISTORY_DAYS = 89


def effective_start(cfg, history_days: int) -> int:
    """Początek zakresu przycięty do tego, co dane źródło w ogóle oddaje.

    Bez tego skrypt wysyłałby setki zapytań o okresy, na które API i tak
    odpowie błędem albo pustką.
    """
    floor_ms = int((datetime.now(timezone.utc) - timedelta(days=history_days)).timestamp() * 1000)
    return max(cfg.start_ms, floor_ms)


def window_guard(
    data: Dataset, coverage: SourceCoverage, label: str
) -> Callable[[BitgetError, int, int], None]:
    """Zamienia błąd okna czasowego w ostrzeżenie zamiast utraty całego źródła."""

    def guard(exc: BitgetError, window_start: int, window_end: int) -> None:
        if is_range_error(exc):
            boundary = to_dt(window_end).strftime("%Y-%m-%d")
            data.warn(
                f"{label}: API nie udostępnia danych starszych niż okolice "
                f"{boundary} - starsze okresy pominięto."
            )
            if not coverage.error:
                coverage.error = "limit historii API"
            log.info("%s: limit historii API (%s)", label, exc.msg)
        else:
            data.warn(f"{label}: {exc.msg}")
            coverage.error = exc.msg
            log.warning("%s: %s", label, exc.msg)

    return guard
