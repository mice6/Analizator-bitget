"""Dzienny snapshot stanu konta - jeden wiersz CSV na uruchomienie.

Rekonstrukcja historii z rejestrów podatkowych sięga około dwóch lat wstecz
i jest obarczona wszystkim, czego API nie pamięta. Snapshot omija ten problem:
zapisuje dziś twarde liczby (kapitał z zewnątrz i wycena aktywów), a po
miesiącu uruchamiania z crona daje czystą krzywą kapitału bez zgadywania.

Pobiera tylko to, czego potrzebuje wynik całkowity: kursy, wpłaty/wypłaty
(w tym P2P) i aktualną wycenę. Rejestry spot/futures, pozycje i transfery są
pomijane - dzięki temu przebieg trwa kilkadziesiąt sekund zamiast godzin.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import Config
from .pipeline import run

log = logging.getLogger("bitget.snapshot")

# Moduły zbędne przy liczeniu samego wyniku całkowitego.
SNAPSHOT_SKIP = [
    "spot",
    "futures",
    "fills",
    "positions",
    "earn",
    "transfers",
    "rejestry",
]

COLUMNS = [
    "data",
    "kapital_wplacony",
    "w_tym_p2p",
    "kapital_wyplacony",
    "kapital_netto",
    "wycena_aktywow",
    "wynik",
    "roi_proc",
    "zmiana_wyceny",
    "zmiana_wyniku",
    "dni_od_poprzedniego",
    "salda_kont",
    "uwagi",
]


def _fmt(value: Optional[float], decimal: str, decimals: int = 2) -> str:
    if value is None:
        return ""
    text = f"{value:.{decimals}f}"
    return text.replace(".", decimal) if decimal != "." else text


def _parse(text: str, decimal: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    if decimal != ".":
        text = text.replace(decimal, ".")
    try:
        return float(text)
    except ValueError:
        return None


class SnapshotError(RuntimeError):
    """Plik docelowy istnieje, ale nie jest plikiem snapshotów."""


def _is_our_file(path: Path, sep: str) -> bool:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = handle.readline()
    if not header.strip():
        return True  # pusty plik - dopiszemy nagłówek
    return [cell.strip() for cell in header.rstrip("\r\n").split(sep)] == COLUMNS


def read_rows(path: Path, sep: str = ";") -> List[Dict[str, str]]:
    """Wcześniejsze snapshoty z pliku. Nie istnieje albo pusty - zero wierszy."""
    if not path.is_file() or not _is_our_file(path, sep):
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=sep)
        if not reader.fieldnames:
            return []
        return [row for row in reader if (row.get("data") or "").strip()]


def build_row(cfg: Config) -> dict:
    """Uruchamia lekki przebieg i zwraca liczby do zapisania."""
    data, analysis, _ = run(cfg)

    p2p_in = sum(
        flow.usd_value for flow in data.deposits if getattr(flow, "source", "") == "p2p"
    )
    notes = []
    for name, coverage in sorted(data.coverage.items()):
        if coverage.error:
            notes.append(f"{name}: {coverage.error}")
    if data.equity is None:
        notes.append("brak wyceny z API")

    accounts = "|".join(
        f"{name}={value:.2f}"
        for name, value in sorted(analysis.equity_by_account.items())
        if abs(value) >= 0.01
    )

    return {
        "ts": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        "data": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "kapital_wplacony": analysis.deposits_total,
        "w_tym_p2p": p2p_in,
        "kapital_wyplacony": analysis.withdrawals_total,
        "kapital_netto": analysis.deposits_total - analysis.withdrawals_total,
        "wycena_aktywow": analysis.equity_now,
        "wynik": analysis.real_pnl,
        "roi_proc": None if analysis.roi is None else analysis.roi * 100,
        "salda_kont": accounts,
        "uwagi": "; ".join(notes),
    }


def _days_between(earlier: str, later: str) -> Optional[float]:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            a = datetime.strptime(earlier.strip(), fmt)
            b = datetime.strptime(later.strip(), fmt)
        except ValueError:
            continue
        return round((b - a).total_seconds() / 86_400, 2)
    return None


def append(path: Path, row: dict, cfg: Config) -> Path:
    """Dopisuje wiersz do pliku, dokładając różnice względem poprzedniego."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not _is_our_file(path, cfg.csv_sep):
        raise SnapshotError(
            f"Plik {path} ma inny nagłówek niż snapshoty. Nie dopisuję do niego, "
            "żeby go nie zepsuć - zmień nazwę pliku albo wskaż inny (--plik)."
        )
    previous = read_rows(path, cfg.csv_sep)
    last = previous[-1] if previous else None
    needs_header = not path.is_file() or path.stat().st_size == 0

    change_equity: Optional[float] = None
    change_pnl: Optional[float] = None
    days: Optional[float] = None
    if last:
        prev_equity = _parse(last.get("wycena_aktywow", ""), cfg.csv_decimal)
        prev_pnl = _parse(last.get("wynik", ""), cfg.csv_decimal)
        if prev_equity is not None:
            change_equity = row["wycena_aktywow"] - prev_equity
        if prev_pnl is not None:
            change_pnl = row["wynik"] - prev_pnl
        days = _days_between(last.get("data", ""), row["data"])

    cells: Sequence[str] = [
        row["data"],
        _fmt(row["kapital_wplacony"], cfg.csv_decimal),
        _fmt(row["w_tym_p2p"], cfg.csv_decimal),
        _fmt(row["kapital_wyplacony"], cfg.csv_decimal),
        _fmt(row["kapital_netto"], cfg.csv_decimal),
        _fmt(row["wycena_aktywow"], cfg.csv_decimal),
        _fmt(row["wynik"], cfg.csv_decimal),
        _fmt(row["roi_proc"], cfg.csv_decimal),
        _fmt(change_equity, cfg.csv_decimal),
        _fmt(change_pnl, cfg.csv_decimal),
        _fmt(days, cfg.csv_decimal),
        row["salda_kont"],
        row["uwagi"],
    ]

    # BOM należy do początku pliku; przy dopisywaniu trafiłby w środek.
    encoding = "utf-8-sig" if needs_header else "utf-8"
    with path.open("a", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=cfg.csv_sep, lineterminator="\n")
        if needs_header:
            writer.writerow(COLUMNS)
        writer.writerow(cells)
    return path
