"""Raportowanie: czytelne podsumowanie w konsoli + eksport do CSV."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .analysis import Analysis
from .config import Config
from .model import Dataset, to_dt

NBSP = "\u00a0"  # spacja nierozdzielająca - separator tysięcy
LINE = "=" * 78
THIN = "-" * 78


def money(value: float, decimals: int = 2) -> str:
    """Format liczbowy w stylu polskim: 1 234.56 (spacja jako separator tysięcy)."""
    return f"{value:,.{decimals}f}".replace(",", NBSP)


def plural(count: int, one: str, few: str, many: str) -> str:
    """Polska odmiana rzeczownika po liczebniku (1 operacja / 2 operacje / 5 operacji)."""
    if count == 1:
        return f"{count} {one}"
    if 12 <= count % 100 <= 14:
        return f"{count} {many}"
    return f"{count} {few}" if count % 10 in (2, 3, 4) else f"{count} {many}"


def _pct(value: Optional[float]) -> str:
    return f"{value * 100:+.2f}%" if value is not None else "n/d"


def _stamp(ts: Optional[int]) -> str:
    return to_dt(ts).strftime("%Y-%m-%d") if ts else "-"


class Reporter:
    def __init__(self, cfg: Config, data: Dataset, analysis: Analysis):
        self.cfg = cfg
        self.data = data
        self.analysis = analysis

    # ------------------------------------------------------------- konsola

    def print_console(self) -> None:
        cfg, a = self.cfg, self.analysis
        show = cfg.fmt_money

        print()
        print(LINE)
        print(" ANALIZA RZECZYWISTEJ RENTOWNOŚCI - BITGET")
        print(
            f" Zakres: {cfg.start:%Y-%m-%d} .. {cfg.end:%Y-%m-%d}"
            f"   |   Wygenerowano: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
        )
        if cfg.fx_rate != 1.0:
            print(f" Waluta raportu: {cfg.fx_label} (kurs 1 USDT = {cfg.fx_rate} {cfg.fx_label})")
        print(LINE)

        print("\n1. KAPITAŁ Z ZEWNĄTRZ (wyceniony kursem z dnia operacji)")
        print(THIN)
        deposits_label = plural(len(self.data.deposits), "operacja", "operacje", "operacji")
        withdrawals_label = plural(len(self.data.withdrawals), "operacja", "operacje", "operacji")
        print(f"   Wpłaty:                {show(a.deposits_total):>22}   ({deposits_label})")
        print(f"   Wypłaty:               {show(a.withdrawals_total):>22}   ({withdrawals_label})")
        print(f"   Kapitał netto na giełdzie: {show(a.deposits_total - a.withdrawals_total):>18}")

        print("\n2. AKTUALNA WARTOŚĆ AKTYWÓW")
        print(THIN)
        for account, value in sorted(
            a.equity_by_account.items(), key=lambda item: -item[1]
        ):
            if abs(value) < 0.005:
                continue
            print(f"   {account:<24} {show(value):>22}")
        print(f"   {'RAZEM':<24} {show(a.equity_now):>22}")

        print("\n3. REALNY ZYSK / STRATA")
        print(THIN)
        print("   wartość aktywów  -  wpłaty  +  wypłaty")
        print(
            f"   {money(a.equity_now)}  -  {money(a.deposits_total)}"
            f"  +  {money(a.withdrawals_total)}"
        )
        verdict = "ZYSK" if a.real_pnl >= 0 else "STRATA"
        print(f"\n   >>> {verdict}: {show(a.real_pnl)}   (ROI od wpłaconego kapitału: {_pct(a.roi)})")

        print("\n4. Z CZEGO SIĘ TO SKŁADA")
        print(THIN)
        rows = [
            ("Spot - zrealizowany wynik z transakcji", a.spot_realized_total),
            ("Spot - niezrealizowany (trzymane monety)", a.spot_unrealized),
            ("Futures - wynik na pozycjach", a.futures_pnl_total),
            ("Futures - funding fee", a.futures_funding_total),
            ("Futures - prowizje", a.futures_fees_total),
            ("Earn - odsetki", a.earn_income_total),
            ("Nagrody / rebaty / airdropy", a.rewards_total),
            ("Pozostałe operacje", a.other_total),
        ]
        for label, value in rows:
            print(f"   {label:<45} {show(value):>22}")
        print(THIN)
        print(f"   {'Suma wyjaśnionych składników':<45} {show(a.attributed_total + a.spot_unrealized):>22}")
        print(f"   {'Różnica do wyniku rzeczywistego':<45} {show(a.unexplained):>22}")
        if abs(a.unexplained) > max(1.0, abs(a.real_pnl) * 0.05):
            print(
                "   (Różnica bierze się zwykle ze zmian wyceny monet trzymanych "
                "poza\n    spotem oraz z historii starszej niż limity API - patrz sekcja 8.)"
            )
        print(
            f"\n   Uwaga: prowizje spot ujęte w wyniku powyżej to łącznie "
            f"{show(a.spot_fees_total)}."
        )

        self._print_months()
        self._print_symbols()

        print("\n7. TRANSFERY WEWNĘTRZNE (nie są zyskiem ani stratą)")
        print(THIN)
        transfers_label = plural(a.transfers_count, "transfer", "transfery", "transferów")
        print(
            f"   {transfers_label} o łącznym wolumenie {show(a.transfers_volume)}"
            " - przesunięcia między Funding/Spot/Futures/Earn."
        )

        self._print_coverage()

    def _print_months(self) -> None:
        show = self.cfg.fmt_money
        print("\n5. MIESIĄC PO MIESIĄCU")
        print(THIN)
        header = (
            f"   {'Miesiąc':<9}{'Wpłaty':>12}{'Wypłaty':>12}{'Spot':>12}"
            f"{'Futures':>12}{'Funding':>11}{'Prowizje F':>12}{'Earn':>10}{'Razem':>12}"
        )
        print(header)
        for month in self.analysis.months:
            print(
                f"   {month.month:<9}"
                f"{money(month.deposits):>12}"
                f"{money(month.withdrawals):>12}"
                f"{money(month.spot_realized):>12}"
                f"{money(month.futures_pnl):>12}"
                f"{money(month.futures_funding):>11}"
                f"{money(month.futures_fees):>12}"
                f"{money(month.earn_income):>10}"
                f"{money(month.attributed):>12}"
            )
        if not self.analysis.months:
            print("   (brak danych w zadanym zakresie)")
        else:
            print(THIN)
            print(
                f"   Suma wyniku miesięcznego (bez niezrealizowanych): "
                f"{show(self.analysis.attributed_total)}"
            )
            print("   Kwoty w USDT; pełne rozbicie w pliku miesiace.csv")

    def _print_symbols(self) -> None:
        show = self.cfg.fmt_money
        symbols = [s for s in self.analysis.symbols if s.trades]
        print("\n6. WYNIK ZREALIZOWANY WG PARY (spot)")
        print(THIN)
        if not symbols:
            print("   (brak transakcji spot w zadanym zakresie)")
            return
        def line(item) -> None:
            trades = plural(item.trades, "transakcja", "transakcje", "transakcji")
            print(f"     {item.symbol:<14}{show(item.realized_pnl):>20}   ({trades})")

        if len(symbols) <= 10:
            for item in reversed(symbols):
                line(item)
        else:
            print("   Najgorsze:")
            for item in symbols[:5]:
                line(item)
            print("   Najlepsze:")
            for item in reversed(symbols[-5:]):
                line(item)
        if self.analysis.uncovered_symbols:
            print(
                "\n   Uwaga: dla par "
                + ", ".join(self.analysis.uncovered_symbols[:8])
                + (" ..." if len(self.analysis.uncovered_symbols) > 8 else "")
                + "\n   wystąpiły sprzedaże monet kupionych przed początkiem zakresu."
                "\n   Ich wynik jest zaniżony/niepełny - rozszerz zakres opcją --od."
            )

    def _print_coverage(self) -> None:
        print("\n8. POKRYCIE DANYCH I OSTRZEŻENIA")
        print(THIN)
        print(f"   {'Źródło':<28}{'Rekordów':>10}{'Od':>13}{'Do':>13}")
        for name in sorted(self.data.coverage):
            cov = self.data.coverage[name]
            note = f"  BŁĄD: {cov.error}" if cov.error else ""
            print(
                f"   {name:<28}{cov.records:>10}{_stamp(cov.first_ts):>13}"
                f"{_stamp(cov.last_ts):>13}{note}"
            )
        for warning in self.data.warnings:
            print(f"   ! {warning}")
        print(
            "\n   Bitget udostępnia historię tylko przez ograniczony czas wstecz."
            "\n   Jeśli kolumna 'Od' jest późniejsza niż Twoja data startu, brakujące"
            "\n   wpłaty/wypłaty dopisz ręcznie i uruchom skrypt z --dodatkowe-przeplywy."
        )

    # ----------------------------------------------------------------- CSV

    def export_csv(self) -> List[Path]:
        out_dir = self.cfg.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        written: List[Path] = []
        a = self.analysis

        converted = self.cfg.fx_rate != 1.0
        summary_header = ["pozycja", "wartosc_usdt"]
        if converted:
            summary_header.append(f"wartosc_{self.cfg.fx_label.lower()}")
        written.append(
            self._write(
                out_dir / "podsumowanie.csv",
                summary_header,
                [
                    ([label, value, value * self.cfg.fx_rate] if converted else [label, value])
                    for label, value in [
                        ("Wpłaty z zewnątrz", a.deposits_total),
                        ("Wypłaty na zewnątrz", a.withdrawals_total),
                        ("Kapitał netto na giełdzie", a.deposits_total - a.withdrawals_total),
                        ("Aktualna wartość aktywów", a.equity_now),
                        ("REALNY ZYSK/STRATA", a.real_pnl),
                        ("Spot - zrealizowany", a.spot_realized_total),
                        ("Spot - niezrealizowany", a.spot_unrealized),
                        ("Spot - prowizje", a.spot_fees_total),
                        ("Futures - wynik pozycji", a.futures_pnl_total),
                        ("Futures - funding fee", a.futures_funding_total),
                        ("Futures - prowizje", a.futures_fees_total),
                        ("Earn - odsetki", a.earn_income_total),
                        ("Nagrody/rebaty", a.rewards_total),
                        ("Pozostałe", a.other_total),
                        ("Różnica niewyjaśniona", a.unexplained),
                    ]
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "miesiace.csv",
                [
                    "miesiac", "wplaty", "wyplaty", "kapital_netto",
                    "spot_zrealizowany", "spot_prowizje", "futures_pnl",
                    "futures_funding", "futures_prowizje", "earn_odsetki",
                    "nagrody", "pozostale", "wynik_razem",
                ],
                [
                    [
                        m.month, m.deposits, m.withdrawals, m.net_external,
                        m.spot_realized, m.spot_fees, m.futures_pnl,
                        m.futures_funding, m.futures_fees, m.earn_income,
                        m.rewards, m.other, m.attributed,
                    ]
                    for m in a.months
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "przeplywy_zewnetrzne.csv",
                ["data", "typ", "moneta", "ilosc", "prowizja", "kurs_usdt", "wartosc_usdt", "zrodlo_kursu", "tx", "zrodlo"],
                [
                    [
                        to_dt(f.ts).strftime("%Y-%m-%d %H:%M:%S"), f.direction, f.coin,
                        f.amount, f.fee, f.price, f.usd_value, f.price_source, f.tx_id, f.source,
                    ]
                    for f in self.data.external_flows
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "transfery_wewnetrzne.csv",
                ["data", "moneta", "ilosc", "z_konta", "na_konto", "id"],
                [
                    [
                        to_dt(t.ts).strftime("%Y-%m-%d %H:%M:%S"), t.coin, t.amount,
                        t.from_type, t.to_type, t.transfer_id,
                    ]
                    for t in sorted(self.data.transfers, key=lambda t: t.ts)
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "spot_wynik_wg_pary.csv",
                ["para", "moneta", "zrealizowany_pnl", "prowizje", "transakcje", "kupiono_usdt", "sprzedano_usdt", "sprzedaz_bez_historii"],
                [
                    [
                        s.symbol, s.base, s.realized_pnl, s.fees, s.trades,
                        s.bought_usd, s.sold_usd, s.uncovered_size,
                    ]
                    for s in a.symbols
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "spot_transakcje_zrealizowane.csv",
                ["data", "para", "ilosc", "przychod_usdt", "koszt_usdt", "prowizja_usdt", "pnl_usdt"],
                [
                    [
                        to_dt(t.ts).strftime("%Y-%m-%d %H:%M:%S"), t.symbol, t.size,
                        t.proceeds_usd, t.cost_usd, t.fee_usd, t.pnl,
                    ]
                    for t in a.realized_trades
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "ksiega_futures.csv",
                ["data", "produkt", "symbol", "typ_operacji", "kategoria", "moneta", "kwota", "prowizja"],
                [
                    [
                        to_dt(e.ts).strftime("%Y-%m-%d %H:%M:%S"), e.product_type, e.symbol,
                        e.business_type, e.category, e.coin, e.amount, e.fee,
                    ]
                    for e in sorted(self.data.futures_ledger, key=lambda e: e.ts)
                ],
            )
        )

        written.append(
            self._write(
                out_dir / "ksiega_spot.csv",
                ["data", "typ_operacji", "kategoria", "moneta", "kwota", "prowizja"],
                [
                    [
                        to_dt(e.ts).strftime("%Y-%m-%d %H:%M:%S"), e.business_type,
                        e.category, e.coin, e.amount, e.fee,
                    ]
                    for e in sorted(self.data.spot_ledger, key=lambda e: e.ts)
                ],
            )
        )

        positions = self.data.equity.positions if self.data.equity else []
        written.append(
            self._write(
                out_dir / "saldo_biezace.csv",
                ["konto", "moneta", "ilosc", "kurs_usdt", "wartosc_usdt"],
                [
                    [p["konto"], p["moneta"], p["ilosc"], p["kurs_usdt"], p["wartosc_usdt"]]
                    for p in positions
                ],
            )
        )

        return [path for path in written if path]

    def _write(self, path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> Path:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=self.cfg.csv_sep, lineterminator="\n")
            writer.writerow(header)
            for row in rows:
                writer.writerow([self._cell(value) for value in row])
        return path

    def _cell(self, value) -> str:
        if isinstance(value, float):
            text = f"{value:.8f}".rstrip("0").rstrip(".")
            text = text or "0"
            decimal = self.cfg.csv_decimal
            return text.replace(".", decimal) if decimal != "." else text
        return "" if value is None else str(value)

    # ---------------------------------------------------------------- JSON

    def payload(self) -> dict:
        """Pełny wynik jako słownik - wspólne źródło dla JSON-a i panelu web."""
        a = self.analysis
        equity = self.data.equity
        return {
            "zakres": {
                "od": self.cfg.start.strftime("%Y-%m-%d"),
                "do": self.cfg.end.strftime("%Y-%m-%d"),
            },
            "waluta": {"etykieta": self.cfg.fx_label, "kurs": self.cfg.fx_rate},
            "wplaty_usdt": a.deposits_total,
            "wyplaty_usdt": a.withdrawals_total,
            "liczba_wplat": len(self.data.deposits),
            "liczba_wyplat": len(self.data.withdrawals),
            "wartosc_aktywow_usdt": a.equity_now,
            "wartosc_aktywow_wg_konta": a.equity_by_account,
            "realny_wynik_usdt": a.real_pnl,
            "roi": a.roi,
            "skladniki": {
                "spot_zrealizowany": a.spot_realized_total,
                "spot_niezrealizowany": a.spot_unrealized,
                "spot_prowizje": a.spot_fees_total,
                "futures_pnl": a.futures_pnl_total,
                "futures_funding": a.futures_funding_total,
                "futures_prowizje": a.futures_fees_total,
                "earn_odsetki": a.earn_income_total,
                "nagrody": a.rewards_total,
                "pozostale": a.other_total,
                "suma_wyjasniona": a.attributed_total + a.spot_unrealized,
                "roznica_niewyjasniona": a.unexplained,
            },
            "miesiace": [
                {
                    "miesiac": m.month,
                    "wplaty": m.deposits,
                    "wyplaty": m.withdrawals,
                    "kapital_netto": m.net_external,
                    "spot_zrealizowany": m.spot_realized,
                    "spot_prowizje": m.spot_fees,
                    "futures_pnl": m.futures_pnl,
                    "futures_funding": m.futures_funding,
                    "futures_prowizje": m.futures_fees,
                    "earn_odsetki": m.earn_income,
                    "nagrody": m.rewards,
                    "pozostale": m.other,
                    "wynik_razem": m.attributed,
                }
                for m in a.months
            ],
            "pary": [
                {
                    "para": item.symbol,
                    "moneta": item.base,
                    "zrealizowany_pnl": item.realized_pnl,
                    "prowizje": item.fees,
                    "transakcje": item.trades,
                    "kupiono": item.bought_usd,
                    "sprzedano": item.sold_usd,
                    "bez_historii": item.uncovered_size,
                }
                for item in reversed(a.symbols)
            ],
            "transfery": {
                "liczba": a.transfers_count,
                "wolumen": a.transfers_volume,
            },
            "salda": equity.positions if equity else [],
            "pokrycie": [
                {
                    "zrodlo": name,
                    "rekordow": cov.records,
                    "od": _stamp(cov.first_ts),
                    "do": _stamp(cov.last_ts),
                    "blad": cov.error,
                }
                for name, cov in sorted(self.data.coverage.items())
            ],
            "pary_bez_historii": a.uncovered_symbols,
            "ostrzezenia": self.data.warnings,
        }

    def export_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.payload(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
