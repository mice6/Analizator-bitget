#!/usr/bin/env python3
"""Dzienny snapshot rentowności konta Bitget - jeden wiersz CSV na uruchomienie.

Odtwarzanie historii z rejestrów sięga tylko tam, gdzie sięga pamięć API.
Snapshot zapisuje to, co dziś jest pewne: ile kapitału weszło z zewnątrz i ile
warte są aktywa. Uruchamiany codziennie z crona buduje krzywą kapitału, która
po miesiącu odpowiada na pytanie "czy konto zarabia" bez żadnej rekonstrukcji.

Użycie:
    python3 snapshot.py
    python3 snapshot.py --plik /home/ubuntu/raport/snapshoty.csv --cicho

Wpis do crona (codziennie o 23:50, log w osobnym pliku):
    50 23 * * * cd /home/ubuntu/Analizator-bitget && ./.venv/bin/python snapshot.py --cicho >> raport/snapshot.log 2>&1

Klucz API czytany jest z .env albo z zaszyfrowanego magazynu panelu. Skrypt
wykonuje wyłącznie żądania GET do api.bitget.com i nie wysyła danych nigdzie
indziej.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from argparse import Namespace
from pathlib import Path

from bitget_analyzer import __version__
from bitget_analyzer.client import BitgetError
from bitget_analyzer.config import ALL_PRODUCT_TYPES, ConfigError, build_config
from bitget_analyzer.snapshot import SNAPSHOT_SKIP, SnapshotError, append, build_row

log = logging.getLogger("bitget")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="snapshot.py",
        description="Dopisuje dzienny stan konta Bitget do pliku CSV (do crona).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--plik", default=None, help="Plik CSV ze snapshotami. Domyślnie <katalog>/snapshoty.csv.")
    parser.add_argument("--out", default="raport", help="Katalog roboczy (pamięć podręczna kursów i okresów).")
    parser.add_argument("--fx-rate", type=float, default=None, help="Kurs 1 USDT w walucie wyświetlania (np. 4.05).")
    parser.add_argument("--fx-label", default=None, help="Nazwa waluty wyświetlania (np. PLN).")
    parser.add_argument("--dodatkowe-przeplywy", "--extra-flows", dest="extra_flows", default=None, help="CSV z wpłatami/wypłatami starszymi niż limity API.")
    parser.add_argument("--rps", type=float, default=8.0, help="Maksymalna liczba żądań na sekundę.")
    parser.add_argument("--csv-sep", default=";", help="Separator kolumn w CSV.")
    parser.add_argument("--csv-decimal", default=",", help="Separator dziesiętny w CSV.")
    parser.add_argument("--cicho", "--quiet", dest="quiet", action="store_true", help="Bez podsumowania na ekranie (tryb cronowy).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Więcej logów.")
    parser.add_argument("--version", action="version", version=f"analizator-bitget {__version__}")
    return parser.parse_args(argv)


def _config(args: argparse.Namespace):
    """Snapshotowi wystarczą kursy, przepływy zewnętrzne i wycena."""
    return build_config(
        Namespace(
            since=None,
            to=None,
            out=args.out,
            fx_rate=args.fx_rate,
            fx_label=args.fx_label,
            product_types=",".join(ALL_PRODUCT_TYPES),
            transfer_coins=None,
            extra_flows=args.extra_flows,
            skip=",".join(SNAPSHOT_SKIP),
            rps=args.rps,
            csv_sep=args.csv_sep,
            csv_decimal=args.csv_decimal,
            verbose=args.verbose,
        )
    )


def _lock(path: Path):
    """Cron potrafi odpalić kolejny przebieg, zanim skończy poprzedni."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = _config(args)
    except ConfigError as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return 2

    target = Path(args.plik) if args.plik else cfg.out_dir / "snapshoty.csv"

    lock = _lock(cfg.out_dir / ".snapshot.lock")
    if lock is None:
        print("Poprzedni snapshot jeszcze się liczy - pomijam ten przebieg.", file=sys.stderr)
        return 0

    try:
        row = build_row(cfg)
        path = append(target, row, cfg)
    except BitgetError as exc:
        print(f"API Bitget zwróciło błąd: {exc}", file=sys.stderr)
        return 1
    except SnapshotError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Przerwano.", file=sys.stderr)
        return 130
    finally:
        lock.close()

    if not args.quiet:
        print(f"\nSnapshot {row['data']} UTC")
        print(f"   Kapitał z zewnątrz : {cfg.fmt_money(row['kapital_netto'])}")
        print(f"   Wycena aktywów     : {cfg.fmt_money(row['wycena_aktywow'])}")
        print(f"   Wynik              : {cfg.fmt_money(row['wynik'])}")
        if row["roi_proc"] is not None:
            print(f"   ROI                : {row['roi_proc']:+.2f}%")
        if row["uwagi"]:
            print(f"   Uwagi              : {row['uwagi']}")
        print(f"\nDopisane do: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
