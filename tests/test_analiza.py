"""Testy offline: cała ścieżka liczenia na sztucznych odpowiedziach API.

Daty w danych testowych są liczone względem "dziś", bo kolektory przycinają
zakres do okresu, który API realnie udostępnia.

Uruchomienie:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bitget_analyzer.analysis import Analyzer
from bitget_analyzer.client import BitgetClient, BitgetError, is_range_error, time_windows
from bitget_analyzer.config import Config
from bitget_analyzer.model import Dataset
from bitget_analyzer.pipeline import collect
from bitget_analyzer.prices import PriceBook
from bitget_analyzer.report import Reporter
from bitget_analyzer.tax import classify_spot_tax, synthesize_fills

NOW = datetime.now(timezone.utc)


def days_ago(count: int) -> int:
    moment = (NOW - timedelta(days=count)).replace(hour=12, minute=0, second=0, microsecond=0)
    return int(moment.timestamp() * 1000)


def day_of(count: int) -> str:
    return (NOW - timedelta(days=count)).strftime("%Y-%m-%d")


def month_of(count: int) -> str:
    return (NOW - timedelta(days=count)).strftime("%Y-%m")


# Kursy dzienne BTC/ETH używane przy wycenie operacji historycznych.
DAILY_PRICES = {
    ("BTCUSDT", day_of(180)): 45000.0,
    ("BTCUSDT", day_of(60)): 40000.0,
    ("BTCUSDT", day_of(30)): 50000.0,
    ("ETHUSDT", day_of(100)): 2000.0,
    ("ETHUSDT", day_of(90)): 2500.0,
}

FIXTURES = {
    "/api/v2/spot/wallet/deposit-records": [
        {"orderId": "d1", "tradeId": "t1", "coin": "USDT", "size": "1000", "status": "success", "cTime": str(days_ago(200))},
        {"orderId": "d2", "tradeId": "t2", "coin": "BTC", "size": "0.1", "status": "success", "cTime": str(days_ago(180))},
        {"orderId": "d3", "tradeId": "t3", "coin": "USDT", "size": "99", "status": "pending", "cTime": str(days_ago(179))},
    ],
    "/api/v2/spot/wallet/withdrawal-records": [
        {"orderId": "w1", "coin": "USDT", "size": "500", "fee": "1", "status": "success", "cTime": str(days_ago(120))},
    ],
    "/api/v2/spot/account/transferRecords": [
        {"transferId": "tr1", "coin": "USDT", "size": "300", "fromType": "spot", "toType": "usdt_futures", "cTime": str(days_ago(45))},
    ],
    # Rejestr podatkowy - główne źródło historii (2 lata).
    "/api/v2/tax/spot-record": [
        {"id": "1", "coin": "USDT", "spotTaxType": "Interest", "amount": "2", "fee": "0", "bizOrderId": "", "ts": str(days_ago(40))},
        # Zlecenie starsze niż dokładna historia transakcji - do odtworzenia.
        {"id": "2", "coin": "USDT", "spotTaxType": "Sell", "amount": "-2000", "fee": "0", "bizOrderId": "O1", "ts": str(days_ago(100))},
        {"id": "3", "coin": "ETH", "spotTaxType": "Buy", "amount": "1.0", "fee": "0", "bizOrderId": "O1", "ts": str(days_ago(100))},
        {"id": "4", "coin": "ETH", "spotTaxType": "Sell", "amount": "-1.0", "fee": "0", "bizOrderId": "O2", "ts": str(days_ago(90))},
        {"id": "5", "coin": "USDT", "spotTaxType": "Buy", "amount": "2500", "fee": "-2.5", "bizOrderId": "O2", "ts": str(days_ago(90))},
        {"id": "6", "coin": "USDT", "spotTaxType": "Transfer", "amount": "-300", "fee": "0", "bizOrderId": "", "ts": str(days_ago(45))},
    ],
    "/api/v2/tax/future-record": [
        {"id": "f1", "symbol": "BTCUSDT", "marginCoin": "USDT", "futureTaxType": "close_long", "amount": "100", "fee": "-0.5", "ts": str(days_ago(35))},
        {"id": "f2", "symbol": "BTCUSDT", "marginCoin": "USDT", "futureTaxType": "contract_settle_fee", "amount": "-3", "fee": "0", "ts": str(days_ago(34))},
        {"id": "f3", "symbol": "", "marginCoin": "USDT", "futureTaxType": "trans_from_exchange", "amount": "300", "fee": "0", "ts": str(days_ago(45))},
    ],
    # Dokładne transakcje - tylko ostatnie 90 dni.
    "/api/v2/spot/trade/fills": [
        {"tradeId": "f1", "orderId": "o1", "symbol": "BTCUSDT", "side": "buy", "priceAvg": "40000", "size": "0.1", "amount": "4000",
         "feeDetail": {"feeCoin": "USDT", "totalFee": "-4"}, "cTime": str(days_ago(60))},
        {"tradeId": "f2", "orderId": "o2", "symbol": "BTCUSDT", "side": "sell", "priceAvg": "50000", "size": "0.1", "amount": "5000",
         "feeDetail": '{"feeCoin":"USDT","totalFee":"-5"}', "cTime": str(days_ago(30))},
    ],
    "/api/v2/account/all-account-balance": [
        {"accountType": "spot", "usdtBalance": "3000"},
        {"accountType": "futures", "usdtBalance": "2000"},
        {"accountType": "earn", "usdtBalance": "500"},
    ],
    "/api/v2/spot/account/assets": [
        {"coin": "USDT", "available": "3000", "frozen": "0", "locked": "0"},
    ],
    "/api/v2/spot/public/symbols": [
        {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT"},
        {"symbol": "ETHUSDT", "baseCoin": "ETH", "quoteCoin": "USDT"},
    ],
    "/api/v2/spot/market/tickers": [
        {"symbol": "BTCUSDT", "lastPr": "60000"},
        {"symbol": "ETHUSDT", "lastPr": "3000"},
    ],
}


class FakeClient(BitgetClient):
    """Podstawia sztuczne odpowiedzi zamiast prawdziwych żądań HTTP."""

    def __init__(self, cfg, oldest_allowed_ms=None):
        super().__init__(cfg)
        self.calls = []
        self.oldest_allowed_ms = oldest_allowed_ms

    def request(self, method, path, params=None, auth=True):
        params = params or {}
        self.calls.append((path, dict(params)))

        if path == "/api/v2/spot/market/history-candles":
            symbol = params.get("symbol", "")
            end_time = int(params.get("endTime", 0))
            day = datetime.fromtimestamp((end_time - 1) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            price = DAILY_PRICES.get((symbol, day))
            if price:
                return [[str(end_time), "0", "0", "0", str(price), "0", "0"]]
            return []

        rows = FIXTURES.get(path)
        if rows is None:
            return []

        start = int(params.get("startTime", 0) or 0)
        end = int(params.get("endTime", 0) or 0)

        # Odwzorowanie limitu historii: API odmawia okien sprzed granicy.
        if self.oldest_allowed_ms and start and start < self.oldest_allowed_ms:
            raise BitgetError("43111", "param error time range illegal", path)

        if start or end:
            rows = [
                row
                for row in rows
                if not (row.get("cTime") or row.get("ts"))
                or start <= int(row.get("cTime") or row.get("ts")) <= end
            ]
        return rows


def make_config(out_dir: Path) -> Config:
    return Config(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        start=NOW - timedelta(days=400),
        end=NOW,
        out_dir=out_dir,
        requests_per_second=0,
    )


def build_dataset(cfg, client):
    """Pełny przebieg pobierania (ta sama ścieżka, co produkcyjna)."""
    prices = PriceBook(client)
    data = collect(cfg, client, prices)
    return data, Analyzer(data, prices).build(), prices


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = make_config(Path(cls.tmp.name))
        cls.data, cls.analysis, cls.prices = build_dataset(cls.cfg, FakeClient(cls.cfg))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_wplaty_wyceniane_kursem_z_dnia(self):
        # 1000 USDT + 0.1 BTC * 45 000 (kurs z dnia wpłaty) = 5500
        self.assertAlmostEqual(self.analysis.deposits_total, 5500.0, places=6)
        self.assertEqual(len(self.data.deposits), 2, "wpłata 'pending' nie powinna być liczona")

    def test_wyplaty_pomniejszone_o_prowizje(self):
        self.assertAlmostEqual(self.analysis.withdrawals_total, 499.0, places=6)

    def test_realny_wynik(self):
        # 5500 (portfel) - 5500 (wpłaty) + 499 (wypłaty)
        self.assertAlmostEqual(self.analysis.equity_now, 5500.0, places=6)
        self.assertAlmostEqual(self.analysis.real_pnl, 499.0, places=6)

    def test_zrealizowany_pnl_spot(self):
        # BTC z dokładnych transakcji: 5000 - 4000 - 9 prowizji = 991
        # ETH odtworzone z rejestru:   2500 - 2000 - 2.5 prowizji = 497.5
        self.assertAlmostEqual(self.analysis.spot_realized_total, 1488.5, places=6)
        by_symbol = {item.symbol: item for item in self.analysis.symbols}
        self.assertAlmostEqual(by_symbol["BTCUSDT"].realized_pnl, 991.0, places=6)
        self.assertAlmostEqual(by_symbol["ETHUSDT"].realized_pnl, 497.5, places=6)
        self.assertFalse(self.analysis.uncovered_symbols)

    def test_futures_rozbite_na_skladniki(self):
        self.assertAlmostEqual(self.analysis.futures_pnl_total, 100.0, places=6)
        self.assertAlmostEqual(self.analysis.futures_funding_total, -3.0, places=6)
        self.assertAlmostEqual(self.analysis.futures_fees_total, -0.5, places=6)

    def test_transfery_nie_wchodza_do_wyniku(self):
        self.assertEqual(self.analysis.transfers_count, 1)
        self.assertAlmostEqual(self.analysis.transfers_volume, 300.0, places=6)
        self.assertAlmostEqual(self.analysis.other_total, 0.0, places=6)

    def test_odsetki_earn(self):
        self.assertAlmostEqual(self.analysis.earn_income_total, 2.0, places=6)

    def test_podzial_miesieczny(self):
        by_month = {row.month: row for row in self.analysis.months}
        self.assertAlmostEqual(by_month[month_of(200)].deposits, 1000.0, places=6)
        self.assertAlmostEqual(by_month[month_of(180)].deposits, 4500.0, places=6)
        self.assertAlmostEqual(by_month[month_of(120)].withdrawals, 499.0, places=6)
        self.assertAlmostEqual(sum(r.deposits for r in self.analysis.months), 5500.0, places=6)
        self.assertAlmostEqual(
            sum(r.spot_realized for r in self.analysis.months), 1488.5, places=6
        )

    def test_ksiega_spot_nie_dubluje_rejestru(self):
        # Rejestr podatkowy zwrócił dane, więc awaryjna księga 90-dniowa
        # nie powinna zostać w ogóle odpytana.
        paths = {path for path, _ in self.data.coverage.items()}
        self.assertIn("rejestr spot (2 lata)", paths)
        self.assertNotIn("księga spot (90 dni)", paths)

    def test_eksport_csv(self):
        reporter = Reporter(self.cfg, self.data, self.analysis)
        files = reporter.export_csv()
        self.assertTrue(files)
        for path in files:
            self.assertTrue(path.is_file(), f"brak pliku {path}")
        summary = (self.cfg.out_dir / "podsumowanie.csv").read_text(encoding="utf-8-sig")
        self.assertIn("REALNY ZYSK/STRATA", summary)
        self.assertIn("499", summary)

    def test_raport_konsolowy_nie_wybucha(self):
        Reporter(self.cfg, self.data, self.analysis).print_console()


class TestLimitHistorii(unittest.TestCase):
    """Błąd starego okna nie może unieważnić danych z okien nowszych."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dane_z_nowszych_okien_ocalaja(self):
        # API udaje, że nie zna niczego starszego niż 70 dni.
        client = FakeClient(self.cfg, oldest_allowed_ms=days_ago(70))
        data, analysis, _ = build_dataset(self.cfg, client)

        # Transakcja BTC sprzed 60 i 30 dni mieści się w limicie.
        self.assertTrue(data.fills, "nowsze transakcje powinny zostać pobrane")
        self.assertAlmostEqual(analysis.futures_pnl_total, 100.0, places=6)
        # Wpłaty sprzed 200 dni wypadły poza limit - musi być o tym ostrzeżenie.
        self.assertTrue(
            any("nie udostępnia danych starszych" in w for w in data.warnings),
            data.warnings,
        )

    def test_nie_marnujemy_zapytan_na_stare_okna(self):
        client = FakeClient(self.cfg, oldest_allowed_ms=days_ago(70))
        build_dataset(self.cfg, client)
        # Bez wczesnego przerywania zakres 400 dni to setki zapytań.
        self.assertLess(len(client.calls), 120, f"za dużo zapytań: {len(client.calls)}")

    def test_rozpoznanie_bledu_zakresu(self):
        self.assertTrue(is_range_error(BitgetError("43111", "param error time range illegal", "/x")))
        self.assertTrue(
            is_range_error(BitgetError("40000", "startTime is before currentTime 90 day", "/x"))
        )
        self.assertTrue(
            is_range_error(
                BitgetError("40000", "startTime and endTime interval cannot be greater than 90 days", "/x")
            )
        )
        self.assertFalse(is_range_error(BitgetError("40012", "invalid api key", "/x")))


class TestRejestrPodatkowy(unittest.TestCase):
    def test_klasyfikacja_typow(self):
        self.assertEqual(classify_spot_tax("Buy"), "trade")
        self.assertEqual(classify_spot_tax("SELL"), "trade")
        self.assertEqual(classify_spot_tax("Interest"), "earn")
        self.assertEqual(classify_spot_tax("Deposit"), "deposit")
        self.assertEqual(classify_spot_tax("Transfer in"), "transfer")
        self.assertEqual(classify_spot_tax("Rebate rewards"), "reward")
        self.assertEqual(classify_spot_tax("Cokolwiek nowego"), "other")

    def test_synteza_nie_dubluje_okresu_z_dokladnych_danych(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_config(Path(tmp.name))
        client = FakeClient(cfg)
        data, _, _ = build_dataset(cfg, client)

        odtworzone = [f for f in data.fills if f.trade_id.startswith("tax:")]
        dokladne = [f for f in data.fills if not f.trade_id.startswith("tax:")]
        self.assertEqual(len(dokladne), 2)
        self.assertEqual(len(odtworzone), 2, "ETH sprzed 90-100 dni")
        # Żadna odtworzona transakcja nie może wpaść w okres pokryty dokładnie.
        najstarsza_dokladna = min(f.ts for f in dokladne)
        self.assertTrue(all(f.ts < najstarsza_dokladna for f in odtworzone))

    def test_synteza_pomija_nietypowe_zlecenia(self):
        from bitget_analyzer.model import CAT_TRADE, LedgerEntry

        data = Dataset()
        # Zlecenie z trzema nogami - nie da się jednoznacznie odczytać pary.
        for index, (coin, amount) in enumerate([("USDT", -100), ("BTC", 0.001), ("BGB", 0.5)]):
            data.spot_ledger.append(
                LedgerEntry(
                    ts=days_ago(150), account="spot", coin=coin, amount=amount,
                    category=CAT_TRADE, symbol="DZIWNE", entry_id=str(index),
                )
            )
        added = synthesize_fills(data, PriceBook(FakeClient(make_config(Path("."))))) 
        self.assertEqual(added, 0)
        self.assertTrue(any("Nie udało się odtworzyć" in w for w in data.warnings))


class TestSprzedazBezHistorii(unittest.TestCase):
    """Sprzedaż monety kupionej przed początkiem zakresu nie może udawać zysku."""

    def test_uncovered(self):
        from bitget_analyzer.model import Fill

        cfg = make_config(Path(tempfile.gettempdir()))
        client = FakeClient(cfg)
        prices = PriceBook(client)
        prices.load_symbols()
        prices.load_current()

        data = Dataset()
        data.fills.append(
            Fill(ts=days_ago(30), symbol="BTCUSDT", base="BTC", quote="USDT",
                 side="sell", price=50000, size=0.2, quote_amount=10000,
                 fee=-10, fee_coin="USDT", trade_id="x")
        )
        analysis = Analyzer(data, prices).build()
        self.assertEqual(analysis.uncovered_symbols, ["BTCUSDT"])
        # Brak podstawy kosztowej => nie doliczamy fikcyjnego zysku (tylko prowizja).
        self.assertAlmostEqual(analysis.spot_realized_total, -10.0, places=6)


class TestPodpisIPaginacja(unittest.TestCase):
    def test_podpis_hmac_sha256(self):
        cfg = make_config(Path(tempfile.gettempdir()))
        cfg.api_secret = "tajne"
        client = BitgetClient(cfg)
        signature = client._sign("1700000000000", "GET", "/api/v2/spot/account/assets")
        expected = base64.b64encode(
            hmac.new(
                b"tajne",
                b"1700000000000GET/api/v2/spot/account/assets",
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(signature, expected)

    def test_okna_czasowe_pokrywaja_caly_zakres(self):
        start, end = days_ago(365), days_ago(0)
        windows = list(time_windows(start, end, 30))
        self.assertEqual(windows[0][0], start)
        self.assertEqual(windows[-1][1], end)
        for previous, following in zip(windows, windows[1:]):
            self.assertEqual(following[0], previous[1] + 1)
            self.assertLessEqual(previous[1] - previous[0], 30 * 86_400_000)

    def test_kursor_paginacji(self):
        rows = [{"billId": "50"}, {"billId": "20"}, {"billId": "35"}]
        self.assertEqual(BitgetClient._next_cursor(rows, "billId"), "20")


class TestLimitowZapytan(unittest.TestCase):
    """Endpointy podatkowe mają limit 1 zapytanie/s - dużo ostrzejszy od reszty."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))
        self.cfg.requests_per_second = 8.0
        self.client = BitgetClient(self.cfg)

    def test_wolniej_dla_rejestrow_podatkowych(self):
        self.assertEqual(self.client._rate_for("/api/v2/tax/spot-record"), 1.0)
        self.assertEqual(self.client._rate_for("/api/v2/tax/future-record"), 1.0)
        self.assertEqual(self.client._rate_for("/api/v2/spot/account/bills"), 8.0)

    def test_globalne_ustawienie_nie_moze_przyspieszyc(self):
        self.cfg.requests_per_second = 0.5
        client = BitgetClient(self.cfg)
        self.assertEqual(client._rate_for("/api/v2/tax/spot-record"), 0.5)

    def test_zero_oznacza_brak_limitu(self):
        self.cfg.requests_per_second = 0
        client = BitgetClient(self.cfg)
        self.assertEqual(client._rate_for("/api/v2/tax/spot-record"), 0.0)

    def test_adaptacyjne_zwalnianie(self):
        limiter = self.client._limiter_for("/api/v2/tax/spot-record")
        self.assertAlmostEqual(limiter.min_interval, 1.0)
        self.assertAlmostEqual(limiter.slow_down(), 2.0)
        self.assertAlmostEqual(limiter.slow_down(), 4.0)
        # Sufit chroni przed zatrzymaniem analizy na zawsze.
        limiter.slow_down()
        self.assertAlmostEqual(limiter.min_interval, 5.0)
        self.assertAlmostEqual(limiter.slow_down(), 5.0)

    def test_zwalnianie_rusza_z_zera(self):
        limiter = self.client._limiter_for("/api/v2/spot/account/bills")
        limiter.min_interval = 0.0
        self.assertAlmostEqual(limiter.slow_down(), 0.25)

    def test_limity_sa_niezalezne_per_endpoint(self):
        self.client._limiter_for("/api/v2/tax/spot-record").slow_down()
        self.assertAlmostEqual(
            self.client._limiter_for("/api/v2/spot/account/bills").min_interval, 0.125
        )


class TestDodatkowePrzeplywy(unittest.TestCase):
    """Ręczne uzupełnienie historii starszej niż limity API."""

    def test_wczytanie_pliku(self):
        from bitget_analyzer.wallet import load_extra_flows

        cfg = make_config(Path(tempfile.gettempdir()))
        prices = PriceBook(FakeClient(cfg))
        data = Dataset()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stare.csv"
            path.write_text(
                "data;typ;moneta;ilosc;wartosc_usd\n"
                "2022-11-14;deposit;USDT;2 500,50;\n"
                "2023-01-08;deposit;BTC;0,05;850.20\n"
                "2023-06-30;withdraw;USDT;1200;\n"
                "2023-07-01;bzdura;USDT;10;\n",
                encoding="utf-8",
            )
            load_extra_flows(path, prices, data)

        self.assertEqual(len(data.deposits), 2)
        self.assertEqual(len(data.withdrawals), 1)
        self.assertAlmostEqual(data.deposits[0].usd_value, 2500.50, places=6)
        self.assertAlmostEqual(data.deposits[1].usd_value, 850.20, places=6)
        self.assertTrue(any("Pominięto wiersz" in w for w in data.warnings))


class TestParsowanieLiczb(unittest.TestCase):
    def test_to_float(self):
        from bitget_analyzer.model import to_float

        self.assertAlmostEqual(to_float("1234.56"), 1234.56)
        self.assertAlmostEqual(to_float("1 234,56"), 1234.56)
        self.assertAlmostEqual(to_float("1,234.56"), 1234.56)
        self.assertEqual(to_float(""), 0.0)
        self.assertEqual(to_float(None), 0.0)
        self.assertEqual(to_float("abc"), 0.0)


if __name__ == "__main__":
    unittest.main()
