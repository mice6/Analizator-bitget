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
from bitget_analyzer.analysis import Analyzer
from bitget_analyzer.client import BitgetClient, BitgetError
from bitget_analyzer.config import ALL_PRODUCT_TYPES, ConfigError, build_config
from bitget_analyzer.earn import fetch_savings_history
from bitget_analyzer.futures import fetch_closed_positions, fetch_futures_bills
from bitget_analyzer.model import Dataset
from bitget_analyzer.prices import PriceBook
from bitget_analyzer.report import Reporter
from bitget_analyzer.spot import coins_seen, fetch_spot_bills, fetch_spot_fills
from bitget_analyzer.valuation import fetch_equity
from bitget_analyzer.wallet import (
    fetch_deposits,
    fetch_transfers,
    fetch_withdrawals,
    load_extra_flows,
)

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
    parser.add_argument("--skip", default="", help="Moduły do pominięcia: wallet,spot,fills,futures,positions,earn,transfers")
    parser.add_argument("--rps", type=float, default=8.0, help="Maksymalna liczba żądań na sekundę.")
    parser.add_argument("--csv-sep", default=";", help="Separator kolumn w CSV.")
    parser.add_argument("--csv-decimal", default=",", help="Separator dziesiętny w CSV.")
    parser.add_argument("--json", dest="json_out", action="store_true", help="Zapisz dodatkowo raport.json.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Więcej logów.")
    parser.add_argument("--version", action="version", version=f"analizator-bitget {__version__}")
    return parser.parse_args(argv)


def collect(cfg, client: BitgetClient, prices: PriceBook) -> Dataset:
    """Pobiera wszystkie potrzebne dane z API."""
    data = Dataset()

    prices.load_symbols()
    prices.load_current()

    if cfg.enabled("wallet"):
        log.info("Pobieram historię wpłat...")
        fetch_deposits(client, cfg, prices, data)
        log.info("Pobieram historię wypłat...")
        fetch_withdrawals(client, cfg, prices, data)

    if cfg.enabled("spot"):
        log.info("Pobieram księgę rachunku spot...")
        fetch_spot_bills(client, cfg, data)

    if cfg.enabled("futures"):
        log.info("Pobieram księgę rachunku futures...")
        fetch_futures_bills(client, cfg, data)

    if cfg.enabled("fills"):
        log.info("Pobieram historię transakcji spot...")
        fetch_spot_fills(client, cfg, prices, data)

    if cfg.enabled("positions"):
        log.info("Pobieram historię zamkniętych pozycji futures...")
        fetch_closed_positions(client, cfg, data)

    if cfg.enabled("earn"):
        log.info("Pobieram dane Earn...")
        fetch_savings_history(client, cfg, data)

    if cfg.enabled("transfers"):
        log.info("Pobieram transfery wewnętrzne...")
        fetch_transfers(client, cfg, data, coins_seen(data))

    if cfg.extra_flows:
        load_extra_flows(cfg.extra_flows, prices, data)

    log.info("Pobieram aktualną wycenę portfela...")
    fetch_equity(client, cfg, prices, data)

    return data


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

    client = BitgetClient(cfg)
    client.sync_time()

    prices = PriceBook(client, cache_path=cfg.out_dir / "price_cache.json")
    try:
        data = collect(cfg, client, prices)
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
    finally:
        prices.save_cache()

    analysis = Analyzer(data, prices).build()
    reporter = Reporter(cfg, data, analysis)
    reporter.print_console()

    files = reporter.export_csv()
    if args.json_out:
        files.append(reporter.export_json(cfg.out_dir / "raport.json"))

    print("\nZapisane pliki:")
    for path in files:
        print(f"   {path}")
    log.info(
        "Wykonano %d żądań do API (%d ponowień).", client.request_count, client.retry_count
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
