#!/usr/bin/env python3
"""Podgląd surowych rekordów z API - do sprawdzenia, co naprawdę znaczą pola.

Pobiera JEDNĄ stronę wskazanego rejestru (jedno zapytanie), więc nie rusza
limitów ani pamięci podręcznej. Służy do rozstrzygania pytań w rodzaju
"czy pole amount to zmiana salda, czy wolumen operacji".

Użycie:
    python3 diagnostyka.py 2024-11
    python3 diagnostyka.py 2024-11 --endpoint futures --limit 30
    python3 diagnostyka.py 2024-11 --coin USDT
"""

from __future__ import annotations

import argparse
import json
import sys
from calendar import monthrange
from datetime import datetime, timezone

from bitget_analyzer.client import BitgetClient
from bitget_analyzer.config import ConfigError, create_config
from bitget_analyzer.secrets_store import SecretsError, resolve_credentials

ENDPOINTS = {
    "spot": "/api/v2/tax/spot-record",
    "futures": "/api/v2/tax/future-record",
    "p2p": "/api/v2/tax/p2p-record",
    "bills": "/api/v2/spot/account/bills",
    "fills": "/api/v2/spot/trade/fills",
}


def month_range(month: str):
    year, mon = (int(part) for part in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    last_day = monthrange(year, mon)[1]
    end = datetime(year, mon, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _policz(client: BitgetClient, path: str, params: dict, args) -> int:
    """Sprawdza, czy stronicowanie nie zwraca tych samych rekordów wielokrotnie.

    Używa dokładnie tej samej funkcji, co produkcyjne pobieranie - jeśli liczba
    wierszy przewyższa liczbę unikalnych identyfikatorów, mamy pętlę.
    """
    from collections import Counter

    base = dict(params)
    limit = base.pop("limit", 500)

    widziane = Counter()
    wiersze = 0
    strony = 0
    kursory = []

    for row in client.paginate(path, base, limit=limit, max_pages=args.strony):
        wiersze += 1
        widziane[str(row.get("id") or row.get("billId") or row.get("tradeId"))] += 1
        if wiersze % limit == 1:
            strony += 1
            kursory.append(str(row.get("id", "?")))

    unikalne = len(widziane)
    print(f"\n{path}   {args.miesiac}" + (f"   moneta={args.coin}" if args.coin else ""))
    print(f"  stron pobranych:        {strony}")
    print(f"  wierszy zwróconych:     {wiersze}")
    print(f"  unikalnych rekordów:    {unikalne}")
    if unikalne:
        print(f"  współczynnik duplikacji: {wiersze / unikalne:.2f}x")

    powtorki = [(rid, n) for rid, n in widziane.most_common(3) if n > 1]
    if powtorki:
        print("\n  BŁĄD: te same rekordy wracają wielokrotnie:")
        for rid, n in powtorki:
            print(f"    {rid}  ->  {n} razy")
    else:
        print("\n  OK: każdy rekord wystąpił dokładnie raz.")

    print("\n  pierwszy id na kolejnych stronach (kursor powinien maleć):")
    for index, cursor in enumerate(kursory[:10], 1):
        print(f"    strona {index}: {cursor}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Surowe rekordy z API Bitget.")
    parser.add_argument("miesiac", help="Miesiąc w formacie RRRR-MM, np. 2024-11")
    parser.add_argument("--endpoint", choices=sorted(ENDPOINTS), default="spot")
    parser.add_argument("--limit", type=int, default=20, help="Ile rekordów pokazać.")
    parser.add_argument("--coin", default=None, help="Filtr po monecie.")
    parser.add_argument(
        "--policz",
        action="store_true",
        help="Przepuść okres przez produkcyjne stronicowanie i policz duplikaty.",
    )
    parser.add_argument("--strony", type=int, default=15, help="Limit stron przy --policz.")
    args = parser.parse_args(argv)

    try:
        cfg = create_config(resolve_credentials())
    except (ConfigError, SecretsError) as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return 2

    start_ms, end_ms = month_range(args.miesiac)
    path = ENDPOINTS[args.endpoint]

    client = BitgetClient(cfg)
    client.sync_time()

    params = {"startTime": start_ms, "endTime": end_ms, "limit": max(args.limit, 20)}
    if args.coin:
        params["coin"] = args.coin.upper()

    if args.policz:
        return _policz(client, path, params, args)

    rows = client.request("GET", path, params) or []
    if isinstance(rows, dict):
        rows = rows.get("resultList") or rows.get("list") or [rows]

    print(f"\n{path}   {args.miesiac}   (jedna strona, {len(rows)} rekordów)\n")
    if not rows:
        print("Pusto - w tym okresie API nic nie zwraca.")
        return 0

    print("Pola w rekordzie:", ", ".join(sorted(rows[0].keys())), "\n")
    for row in rows[: args.limit]:
        print(json.dumps(row, ensure_ascii=False))

    # Podsumowanie po typie operacji - od razu widać, co dominuje.
    typy = {}
    for row in rows:
        key = str(
            row.get("spotTaxType")
            or row.get("futureTaxType")
            or row.get("p2pTaxType")
            or row.get("businessType")
            or "?"
        )
        bucket = typy.setdefault(key, [0, 0.0])
        bucket[0] += 1
        try:
            bucket[1] += float(row.get("amount") or row.get("size") or 0)
        except (TypeError, ValueError):
            pass

    print("\nTypy operacji na tej stronie:")
    for name, (count, total) in sorted(typy.items(), key=lambda item: -item[1][0]):
        print(f"  {name:<28} {count:>5} szt.   suma amount: {total:>18.8f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
