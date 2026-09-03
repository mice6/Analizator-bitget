#!/usr/bin/env python3
"""Analizator rzeczywistej rentowności konta Bitget (API v2, tylko odczyt).

Odpowiada na pytanie: ile REALNIE zarobiłem/straciłem, licząc od kapitału
wpłaconego z zewnątrz - a nie od ROI pojedynczych botów.

Użycie:
    python3 analizuj.py --od 2024-01-01
    python3 analizuj.py --od 2023-01-01 --do 2024-12-31 --fx-rate 4.05 --fx-label PLN

Klucz API czytany jest z pliku .env (patrz .env.example). Skrypt wykonuje
wyłącznie żądania GET do api.bitget.com i nie wysyła danych nigdzie indziej.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bitget_analyzer import __version__
from bitget_analyzer.client import BitgetError
from bitget_analyzer.config import ALL_PRODUCT_TYPES, ConfigError, build_config
from bitget_analyzer.pipeline import run
from bitget_analyzer.report import Reporter

log = logging.getLogger("bitget")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analizuj.py",
        description="Rzeczywista rentowność konta Bitget (spot + futures + earn).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--od", "--from", dest="since", help="Początek zakresu (YYYY-MM-DD). Domyślnie rok wstecz.")
    parser.add_argument("--do", "--to", dest="to", help="Koniec zakresu (YYYY-MM-DD). Domyślnie dziś.")
    parser.add_argument("--out", default="raport", help="Katalog na pliki CSV.")
    parser.add_argument("--fx-rate", type=float, default=None, help="Kurs 1 USDT w walucie wyświetlania (np. 4.05).")
    parser.add_argument("--fx-label", default=None, help="Nazwa waluty wyświetlania (np. PLN).")
    parser.add_argument(
        "--product-types",
        default=",".join(ALL_PRODUCT_TYPES),
        help="Typy kontraktów futures do pobrania.",
    )
    parser.add_argument("--transfer-coins", default=None, help="Monety do sprawdzenia w historii transferów (domyślnie: wykryte automatycznie).")
    parser.add_argument("--dodatkowe-przeplywy", "--extra-flows", dest="extra_flows", default=None, help="CSV z wpłatami/wypłatami starszymi niż limity API.")
    parser.add_argument(
        "--historia",
        choices=["pelna", "szybka"],
        default="pelna",
        help="pelna: 2 lata z rejestrów podatkowych (wolne przy grid botach, "
             "limit 1 zapytanie/s). szybka: 90 dni z ksiąg rachunków.",
    )
    parser.add_argument("--skip", default="", help="Moduły do pominięcia: wallet,spot,fills,futures,positions,earn,transfers,rejestry")
    parser.add_argument("--rps", type=float, default=8.0, help="Maksymalna liczba żądań na sekundę.")
    parser.add_argument("--csv-sep", default=";", help="Separator kolumn w CSV.")
    parser.add_argument("--csv-decimal", default=",", help="Separator dziesiętny w CSV.")
    parser.add_argument("--json", dest="json_out", action="store_true", help="Zapisz dodatkowo raport.json.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Więcej logów.")
    parser.add_argument("--version", action="version", version=f"analizator-bitget {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = build_config(args)
    except ConfigError as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return 2

    try:
        data, analysis, _ = run(cfg)
    except BitgetError as exc:
        print(f"\nAPI Bitget zwróciło błąd: {exc}", file=sys.stderr)
        if exc.is_permission_error:
            print(
                "Wygląda na brak uprawnień klucza API. Sprawdź, czy klucz ma "
                "zaznaczony odczyt dla Spot, Futures i Earn.",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        return 130

    reporter = Reporter(cfg, data, analysis)
    reporter.print_console()

    files = reporter.export_csv()
    if args.json_out:
        files.append(reporter.export_json(cfg.out_dir / "raport.json"))

    print("\nZapisane pliki:")
    for path in files:
        print(f"   {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
