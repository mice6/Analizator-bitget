#!/usr/bin/env python3
"""Lokalny panel web analizatora Bitget.

Uruchomienie:
    python3 panel.py

Panel nasłuchuje domyślnie tylko na 127.0.0.1 i wymaga losowego tokenu,
który wypisuje w terminacie przy starcie. Klucze API podajesz w przeglądarce;
są zapisywane w zaszyfrowanej postaci poza katalogiem projektu, więc nie mogą
trafić do repozytorium.

Dostęp z serwera bez pulpitu - przez tunel SSH, bez wystawiania portu:
    ssh -L 8770:127.0.0.1:8770 uzytkownik@serwer
    (na serwerze) python3 panel.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser

from bitget_analyzer import __version__
from bitget_analyzer.webapp.server import create_server, serve


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="panel.py",
        description="Panel web do analizy rentowności konta Bitget.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Adres nasłuchu. Zmieniaj tylko świadomie.")
    parser.add_argument("--port", type=int, default=8770, help="Port panelu (0 = wolny port).")
    parser.add_argument("--out", default="raport", help="Domyślny katalog na pliki CSV.")
    parser.add_argument("--otworz", action="store_true", help="Otwórz panel w przeglądarce po starcie.")
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

    if not args.otworz:
        serve(args.host, args.port, args.out)
        return 0

    # Przy --otworz musimy znać adres przed wystartowaniem pętli serwera.
    server, url = create_server(args.host, args.port, args.out)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="panel")
    thread.start()
    print(f"\n  Panel analizatora Bitget:\n\n     {url}\n\n  Zatrzymanie: Ctrl+C\n")
    webbrowser.open(url)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("\nZatrzymuję panel...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
