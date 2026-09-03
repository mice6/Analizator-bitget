"""Pamięć podręczna okresów pobranych z API.

Rejestry podatkowe mają wąską pulę zapytań, a kolejne uruchomienia analizy
pytają o dokładnie te same okresy. Raz pobrany okres zapisujemy na dysk
i przy następnym przebiegu bierzemy go stamtąd.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("bitget.cache")

# Granice okien muszą być stabilne między uruchomieniami, inaczej każdy
# przebieg wyliczyłby inne klucze. Zaokrąglamy koniec zakresu do pełnej godziny.
SNAP_MS = 60 * 60 * 1000

# Wersja formatu zapisu. Starsze wersje trzymały surowe wiersze z API; nowa
# trzyma podsumowania okresów. Wczytanie starego pliku dałoby ciche zera,
# więc niezgodny format jest odrzucany.
FORMAT_VERSION = 2


def snap(timestamp_ms: int) -> int:
    return (timestamp_ms // SNAP_MS) * SNAP_MS


class WindowCache:
    """Mapuje (endpoint, okres) na pobrane rekordy."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._data: Dict[str, List[dict]] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.debug("Nie wczytano pamięci podręcznej: %s", exc)
            return
        if not isinstance(loaded, dict):
            return

        version = loaded.get("__format__")
        if version != FORMAT_VERSION:
            log.warning(
                "Pamięć podręczna %s pochodzi ze starszej wersji skryptu - "
                "pomijam ją i pobieram dane od nowa.",
                self.path,
            )
            self._data = {}
            self._dirty = True
            return

        entries = loaded.get("entries")
        if isinstance(entries, dict):
            self._data = {k: v for k, v in entries.items() if isinstance(v, list)}

    @staticmethod
    def key(path: str, window_start: int, window_end: int) -> str:
        return f"{path}|{window_start}|{window_end}"

    def get(self, key: str) -> Optional[List[dict]]:
        rows = self._data.get(key)
        if rows is None:
            self.misses += 1
            return None
        self.hits += 1
        return rows

    def put(self, key: str, rows: List[dict]) -> None:
        self._data[key] = rows
        self._dirty = True

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"__format__": FORMAT_VERSION, "entries": self._data},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._dirty = False
        except OSError as exc:  # pragma: no cover
            log.debug("Nie zapisano pamięci podręcznej: %s", exc)

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        # Bez tego pusta pamięć podręczna byłaby falsy i ciche `if cache`
        # pomijałoby ją przy pierwszym uruchomieniu.
        return True
