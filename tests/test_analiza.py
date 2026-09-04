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
    ("BTCUSDT", day_of(41)): 40000.0,
    ("BTCUSDT", day_of(40)): 44000.0,
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
    "/api/v2/tax/p2p-record": [
        {"id": "p1", "coin": "USDT", "p2pTaxType": "buy", "balance": "300", "ts": str(days_ago(300))},
        {"id": "p2", "coin": "USDT", "p2pTaxType": "sell", "balance": "50", "ts": str(days_ago(150))},
        {"id": "p3", "coin": "USDT", "p2pTaxType": "transfer_in", "balance": "999", "ts": str(days_ago(140))},
    ],
    "/api/v2/tax/future-record": [
        {"id": "f1", "symbol": "BTCUSDT", "marginCoin": "USDT", "futureTaxType": "close_long", "amount": "100", "fee": "-0.5", "ts": str(days_ago(35))},
        {"id": "f2", "symbol": "BTCUSDT", "marginCoin": "USDT", "futureTaxType": "contract_settle_fee", "amount": "-3", "fee": "0", "ts": str(days_ago(34))},
        {"id": "f3", "symbol": "", "marginCoin": "USDT", "futureTaxType": "trans_from_exchange", "amount": "300", "fee": "0", "ts": str(days_ago(45))},
    ],
    # Księgi rachunków - ścieżka awaryjna i tryb szybki (90 dni).
    "/api/v2/spot/account/bills": [
        {"billId": "b1", "coin": "USDT", "groupType": "financial", "businessType": "INTEREST",
         "size": "2", "fees": "0", "cTime": str(days_ago(40))},
        {"billId": "b2", "coin": "USDT", "groupType": "transaction", "businessType": "SELL",
         "size": "5000", "fees": "-5", "cTime": str(days_ago(30))},
        {"billId": "b3", "coin": "USDT", "groupType": "transfer", "businessType": "TRANSFER_OUT",
         "size": "-300", "fees": "0", "cTime": str(days_ago(45))},
    ],
    "/api/v2/mix/account/bill": [
        {"billId": "m1", "coin": "USDT", "businessType": "close_long", "amount": "100",
         "fee": "-0.5", "symbol": "BTCUSDT", "cTime": str(days_ago(35))},
        {"billId": "m2", "coin": "USDT", "businessType": "contract_settle_fee", "amount": "-3",
         "fee": "0", "symbol": "BTCUSDT", "cTime": str(days_ago(34))},
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
        # 1000 USDT + 0.1 BTC * 45 000 (kurs z dnia wpłaty) + 300 z P2P
        self.assertAlmostEqual(self.analysis.deposits_total, 5800.0, places=6)
        self.assertEqual(len(self.data.deposits), 3, "wpłata 'pending' nie powinna być liczona")

    def test_wyplaty_pomniejszone_o_prowizje(self):
        # 499 z wypłaty on-chain (500 minus prowizja) + 50 sprzedane przez P2P
        self.assertAlmostEqual(self.analysis.withdrawals_total, 549.0, places=6)

    def test_realny_wynik(self):
        # 5500 (portfel) - 5800 (wpłaty) + 549 (wypłaty)
        self.assertAlmostEqual(self.analysis.equity_now, 5500.0, places=6)
        self.assertAlmostEqual(self.analysis.real_pnl, 249.0, places=6)

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
        self.assertAlmostEqual(sum(r.deposits for r in self.analysis.months), 5800.0, places=6)
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
        self.assertIn("249", summary)

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
        data.spot_orders["DZIWNE"] = [
            LedgerEntry(
                ts=days_ago(150), account="spot", coin=coin, amount=amount,
                category=CAT_TRADE, symbol="DZIWNE", entry_id=str(index),
            )
            for index, (coin, amount) in enumerate([("USDT", -100), ("BTC", 0.001), ("BGB", 0.5)])
        ]
        added = synthesize_fills(data, PriceBook(FakeClient(make_config(Path("."))))) 
        self.assertEqual(added, 0)
        self.assertTrue(any("Nie udało się odtworzyć" in w for w in data.warnings))


class TestKapitaluZaCaleZycieKonta(unittest.TestCase):
    """Wąski zakres analizy nie może uciąć wpłat - inaczej wynik jest zawyżony."""

    def test_wplaty_spoza_zakresu_sa_liczone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            cfg.requests_per_second = 0
            # Użytkownik pyta tylko o ostatnie 30 dni...
            cfg.start = NOW - timedelta(days=30)
            _, analysis, _ = build_dataset(cfg, FakeClient(cfg))

        # ...ale wpłaty sprzed 200 i 300 dni muszą wejść do wyniku.
        self.assertAlmostEqual(analysis.deposits_total, 5800.0, places=6)
        self.assertAlmostEqual(analysis.withdrawals_total, 549.0, places=6)
        self.assertAlmostEqual(analysis.real_pnl, 249.0, places=6)

    def test_wynik_nie_zalezy_od_szerokosci_zakresu(self):
        wyniki = []
        for dni in (30, 120, 400):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(Path(tmp))
                cfg.requests_per_second = 0
                cfg.start = NOW - timedelta(days=dni)
                _, analysis, _ = build_dataset(cfg, FakeClient(cfg))
                wyniki.append(round(analysis.real_pnl, 6))
        self.assertEqual(len(set(wyniki)), 1, f"wynik zmienia się z zakresem: {wyniki}")


class TestKapitaluZP2P(unittest.TestCase):
    """Zakup krypto za walutę przez P2P to kapitał z zewnątrz, nie zysk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))
        self.cfg.requests_per_second = 0
        self.data, self.analysis, _ = build_dataset(self.cfg, FakeClient(self.cfg))

    def test_zakup_liczy_sie_jak_wplata(self):
        p2p = [flow for flow in self.data.deposits if flow.source == "p2p"]
        self.assertEqual(len(p2p), 1)
        self.assertAlmostEqual(p2p[0].usd_value, 300.0, places=6)
        # Wpłaty on-chain (5500) plus zakup P2P (300).
        self.assertAlmostEqual(self.analysis.deposits_total, 5800.0, places=6)

    def test_sprzedaz_liczy_sie_jak_wyplata(self):
        p2p = [flow for flow in self.data.withdrawals if flow.source == "p2p"]
        self.assertEqual(len(p2p), 1)
        self.assertAlmostEqual(self.analysis.withdrawals_total, 549.0, places=6)

    def test_transfery_p2p_nie_sa_kapitalem(self):
        """transfer_in to ruch w obrębie giełdy, nie nowe pieniądze."""
        wszystkie = self.data.deposits + self.data.withdrawals
        self.assertFalse([f for f in wszystkie if f.amount == 999])

    def test_wynik_uwzglednia_kapital_z_p2p(self):
        # 5500 (portfel) - 5800 (wpłaty) + 549 (wypłaty) = 249
        self.assertAlmostEqual(self.analysis.real_pnl, 249.0, places=6)

    def test_p2p_widoczne_w_eksporcie(self):
        reporter = Reporter(self.cfg, self.data, self.analysis)
        reporter.export_csv()
        csv = (self.cfg.out_dir / "przeplywy_zewnetrzne.csv").read_text(encoding="utf-8-sig")
        self.assertIn("p2p", csv)


class TestKlasyfikacjiEarn(unittest.TestCase):
    """Wpłata do Earn to przesunięcie kapitału, nie ujemne odsetki."""

    def test_grupa_financial_w_ksiedze(self):
        from bitget_analyzer.spot import _financial_category

        self.assertEqual(_financial_category("INTEREST"), "earn")
        self.assertEqual(_financial_category("SAVINGS_PROFIT"), "earn")
        self.assertEqual(_financial_category("SUBSCRIBE"), "transfer")
        self.assertEqual(_financial_category("SAVINGS_REDEEM"), "transfer")
        self.assertEqual(_financial_category("PRINCIPAL_RETURN"), "transfer")
        # Nieznany typ nie może zostać uznany za zysk.
        self.assertEqual(_financial_category("COS_NOWEGO"), "transfer")

    def test_rejestr_podatkowy(self):
        self.assertEqual(classify_spot_tax("Subscribe"), "transfer")
        self.assertEqual(classify_spot_tax("Savings redeem"), "transfer")
        self.assertEqual(classify_spot_tax("Interest"), "earn")
        self.assertEqual(classify_spot_tax("Savings interest"), "earn")


class TestFuturesZPozycji(unittest.TestCase):
    """Gdy księga futures milczy, wynik liczymy z zamkniętych pozycji."""

    def test_rozbicie_netProfit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            prices = PriceBook(FakeClient(cfg))
            data = Dataset()
            data.closed_positions.append(
                {
                    "_ts": days_ago(20), "marginCoin": "USDT",
                    "netProfit": "100", "openFee": "-0.6", "closeFee": "-0.4",
                    "totalFunding": "-3",
                }
            )
            analysis = Analyzer(data, prices).build()

        # netProfit zawiera prowizje i funding - rozbijamy bez podwójnego liczenia.
        self.assertAlmostEqual(analysis.futures_fees_total, -1.0, places=6)
        self.assertAlmostEqual(analysis.futures_funding_total, -3.0, places=6)
        self.assertAlmostEqual(analysis.futures_pnl_total, 104.0, places=6)
        suma = (
            analysis.futures_pnl_total
            + analysis.futures_fees_total
            + analysis.futures_funding_total
        )
        self.assertAlmostEqual(suma, 100.0, places=6, msg="suma musi dać netProfit")
        self.assertTrue(any("zamkniętych pozycji" in w for w in data.warnings))

    def test_ksiega_ma_pierwszenstwo(self):
        """Mając księgę, nie dubluj wyniku pozycjami."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            data, analysis, _ = build_dataset(cfg, FakeClient(cfg))
            data.closed_positions.append(
                {"_ts": days_ago(20), "marginCoin": "USDT", "netProfit": "9999"}
            )
            powtorzona = Analyzer(data, PriceBook(FakeClient(cfg))).build()
        self.assertAlmostEqual(powtorzona.futures_pnl_total, 100.0, places=6)


class TestWynikuBotowZKsiegi(unittest.TestCase):
    """Zlecenia botów nie trafiają do /spot/trade/fills - liczymy je z księgi.

    Kluczowe: wynik musi wynikać z RÓŻNICY KURSÓW między kupnem a sprzedażą.
    Sumowanie obu nóg transakcji zawsze dałoby zero, bo wymiana odbywa się
    po kursie rynkowym.
    """

    def _dataset(self, buy_days_ago: int, sell_days_ago: int, with_fill: bool = False):
        from bitget_analyzer.model import CAT_TRADE, Fill, LedgerEntry

        data = Dataset()
        legs = [
            (buy_days_ago, "USDT", -100.0, -0.1),
            (buy_days_ago, "BTC", 0.0025, 0.0),
            (sell_days_ago, "BTC", -0.0025, 0.0),
            (sell_days_ago, "USDT", 110.0, -0.11),
        ]
        for index, (dni, coin, amount, fee) in enumerate(legs):
            data.spot_ledger.append(
                LedgerEntry(
                    ts=days_ago(dni) + index, account="spot", coin=coin,
                    amount=amount, fee=fee, category=CAT_TRADE,
                    business_type="BUY" if amount > 0 else "SELL",
                    entry_id=str(index),
                )
            )
        if with_fill:
            data.fills.append(
                Fill(ts=days_ago(sell_days_ago), symbol="BTCUSDT", base="BTC",
                     quote="USDT", side="buy", price=40000, size=0.0025,
                     quote_amount=100, fee=-0.1, fee_coin="USDT", trade_id="realny")
            )
        return data

    def _run(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            prices = PriceBook(FakeClient(cfg))
            prices.load_symbols()
            return Analyzer(data, prices).build()

    def test_wynik_odzwierciedla_ruch_kursu(self):
        # Kupno po 40 000, sprzedaż po 44 000: 0,0025 * 4 000 = 10, minus 0,21 prowizji.
        data = self._dataset(buy_days_ago=41, sell_days_ago=40)
        analysis = self._run(data)
        self.assertAlmostEqual(analysis.spot_realized_total, 9.79, places=6)
        self.assertTrue(any("z księgi rachunku" in w for w in data.warnings))

    def test_bez_ruchu_kursu_zostaja_same_prowizje(self):
        # Kupno i sprzedaż tego samego dnia po tym samym kursie: zysku nie ma.
        data = self._dataset(buy_days_ago=40, sell_days_ago=40)
        analysis = self._run(data)
        self.assertAlmostEqual(analysis.spot_realized_total, -0.21, places=6)

    def test_brak_podwojnego_liczenia_gdy_transakcje_sa(self):
        """Miesiąc pokryty dokładnymi transakcjami nie może być liczony dwa razy."""
        data = self._dataset(buy_days_ago=41, sell_days_ago=40, with_fill=True)
        analysis = self._run(data)
        self.assertNotAlmostEqual(analysis.spot_realized_total, 9.79, places=6)
        self.assertFalse(any("z księgi rachunku" in w for w in data.warnings))

    def test_sprzedaz_bez_zakupu_nie_tworzy_zysku(self):
        from bitget_analyzer.model import CAT_TRADE, LedgerEntry

        data = Dataset()
        data.spot_ledger.append(
            LedgerEntry(ts=days_ago(40), account="spot", coin="BTC", amount=-0.0025,
                        fee=0.0, category=CAT_TRADE, entry_id="1")
        )
        data.spot_ledger.append(
            LedgerEntry(ts=days_ago(40), account="spot", coin="USDT", amount=110.0,
                        fee=-0.11, category=CAT_TRADE, entry_id="2")
        )
        analysis = self._run(data)
        # Bez znanej ceny nabycia zostaje sama prowizja, nie fikcyjne 110 USDT.
        self.assertAlmostEqual(analysis.spot_realized_total, -0.11, places=6)
        self.assertTrue(any("kupionych przed początkiem" in w for w in data.warnings))


class TestWynikuZSumMiesiecznych(unittest.TestCase):
    """Tryb pełny zwija rejestr do sum miesięcznych - inna metoda liczenia."""

    def _analyse(self, legs):
        from bitget_analyzer.model import CAT_TRADE, LedgerEntry

        data = Dataset()
        for index, (coin, amount, fee) in enumerate(legs):
            data.spot_ledger.append(
                LedgerEntry(
                    ts=days_ago(40), account="spot", coin=coin, amount=amount,
                    fee=fee, category=CAT_TRADE, entry_id=str(index),
                    aggregated=True,
                )
            )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            prices = PriceBook(FakeClient(cfg))
            prices.load_symbols()
            prices.load_current()
            return data, Analyzer(data, prices).build()

    def test_zamkniety_cykl_bota_daje_zysk(self):
        # Bot obrócił BTC i wrócił do stanu wyjściowego, zostawiając +40 USDT.
        data, analysis = self._analyse([("USDT", 40.0, -2.0), ("BTC", 0.0, 0.0)])
        self.assertAlmostEqual(analysis.spot_realized_total, 38.0, places=6)
        self.assertTrue(any("sum rejestru" in w for w in data.warnings))

    def test_zakup_bez_sprzedazy_obniza_wynik_miesiaca(self):
        # -60 000 USDT i +1 BTC: przy kursie bieżącym 60 000 wychodzi zero.
        _, analysis = self._analyse([("USDT", -60000.0, 0.0), ("BTC", 1.0, 0.0)])
        self.assertAlmostEqual(analysis.spot_realized_total, 0.0, places=2)

    def test_sumy_nie_ida_przez_silnik_kosztu_nabycia(self):
        """Na sumach miesięcznych koszt nabycia nie ma sensu - nie wolno go użyć."""
        data, _ = self._analyse([("USDT", 40.0, 0.0), ("BTC", 0.0, 0.0)])
        self.assertFalse(any("z księgi rachunku" in w for w in data.warnings))


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


class TestOdpornoscNaLimity(unittest.TestCase):
    """Uparte 429 nie może wysadzić przebiegu ani go zawiesić."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))
        self.cfg.requests_per_second = 0

    def test_rodzina_tax_dzieli_jeden_limit(self):
        client = BitgetClient(self.cfg)
        self.assertEqual(client._bucket_for("/api/v2/tax/spot-record"), "tax")
        self.assertEqual(client._bucket_for("/api/v2/tax/future-record"), "tax")
        self.assertIs(
            client._limiter_for("/api/v2/tax/spot-record"),
            client._limiter_for("/api/v2/tax/future-record"),
        )
        self.assertIsNot(
            client._limiter_for("/api/v2/tax/spot-record"),
            client._limiter_for("/api/v2/spot/account/bills"),
        )

    def test_po_wyczerpaniu_prob_endpoint_jest_pomijany_bez_sieci(self):
        from bitget_analyzer.client import RateLimitError

        client = BitgetClient(self.cfg)
        client._exhausted.add("tax")
        with self.assertRaises(RateLimitError):
            client.request("GET", "/api/v2/tax/spot-record", {})
        # Nie wykonano żadnego żądania sieciowego.
        self.assertEqual(client.request_count, 0)

    def test_blad_limitu_nie_przerywa_analizy(self):
        """Gdy rejestr odbije limitem, wchodzi źródło awaryjne."""
        from bitget_analyzer.client import RateLimitError

        class ThrottledClient(FakeClient):
            def request(self, method, path, params=None, auth=True):
                if path.startswith("/api/v2/tax/"):
                    raise RateLimitError(path, 5)
                return super().request(method, path, params, auth)

        data, analysis, _ = build_dataset(self.cfg, ThrottledClient(self.cfg))
        # Analiza dobiegła końca i twarda liczba jest policzona.
        self.assertAlmostEqual(analysis.real_pnl, 499.0, places=6)
        self.assertTrue(any("ogranicza zapytania" in w for w in data.warnings), data.warnings)

    def test_przerwanie_po_serii_pustych_okresow(self):
        """Zakres 'od 2000 roku' nie może kosztować setek zapytań."""
        cfg = make_config(Path(self.tmp.name))
        cfg.start = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cfg.requests_per_second = 0
        client = FakeClient(cfg)
        build_dataset(cfg, client)

        wallet_calls = [
            path for path, _ in client.calls if "wallet/deposit" in path or "wallet/withdraw" in path
        ]
        self.assertLess(len(wallet_calls), 70, f"za dużo zapytań o portfel: {len(wallet_calls)}")


class TestPamieciPodrecznej(unittest.TestCase):
    """Rejestry podatkowe mają wąską pulę zapytań - drugi przebieg nie może jej palić."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))
        self.cfg.requests_per_second = 0

    def _run(self):
        from bitget_analyzer.cache import WindowCache
        from bitget_analyzer.pipeline import collect

        client = FakeClient(self.cfg)
        cache = WindowCache(self.cfg.out_dir / "cache_okresow.json")
        prices = PriceBook(client)
        data = collect(self.cfg, client, prices, None, cache)
        cache.save()
        return data, cache, client

    @staticmethod
    def _calls(client, prefix):
        return [path for path, _ in client.calls if path.startswith(prefix)]

    def test_drugi_przebieg_pyta_tylko_o_biezacy_okres(self):
        _, _, first = self._run()
        pierwszy_raz = self._calls(first, "/api/v2/tax/")
        self.assertTrue(pierwszy_raz, "pierwszy przebieg musi coś pobrać")

        data, cache, second = self._run()
        drugi_raz = self._calls(second, "/api/v2/tax/")
        # Zamknięte okresy z pamięci; dopytujemy tylko o trwający okres
        # (po jednym na endpoint: spot, futures, p2p).
        self.assertLessEqual(len(drugi_raz), 3, drugi_raz)
        self.assertLess(len(drugi_raz), len(pierwszy_raz))
        self.assertTrue(cache.hits)
        self.assertTrue(data.spot_ledger)
        self.assertTrue(data.futures_ledger)

    def test_stare_wplaty_nie_sa_pobierane_ponownie(self):
        """Wpłata z 2021 roku już się nie zmieni - nie ma po co o nią pytać."""
        _, _, first = self._run()
        pierwszy_raz = self._calls(first, "/api/v2/spot/wallet/deposit-records")
        self.assertGreater(len(pierwszy_raz), 5, "zakres obejmuje wiele okresów")

        _, _, second = self._run()
        drugi_raz = self._calls(second, "/api/v2/spot/wallet/deposit-records")
        self.assertLessEqual(len(drugi_raz), 1, drugi_raz)

    def test_granice_okresow_nie_zmieniaja_sie_z_uplywem_czasu(self):
        """Klucze muszą przeżyć restart nazajutrz, a nie tylko w tej samej godzinie."""
        from bitget_analyzer.client import time_windows

        teraz = int(NOW.timestamp() * 1000)
        godzine_pozniej = teraz + 3_600_000
        start = teraz - 400 * 86_400_000

        dzis = list(time_windows(start, teraz, 30, align_to_grid=True))
        pozniej = list(time_windows(start, godzine_pozniej, 30, align_to_grid=True))
        # Wszystkie zamknięte okresy (poza najnowszym) muszą być identyczne.
        self.assertEqual(dzis[:-1], pozniej[:-1])

    def test_pusta_pamiec_nie_jest_falsy(self):
        """Pusty magazyn musi być prawdziwy w warunku - inaczej byłby pomijany."""
        from bitget_analyzer.cache import WindowCache

        cache = WindowCache(Path(self.tmp.name) / "pusty.json")
        self.assertEqual(len(cache), 0)
        self.assertTrue(cache, "pusta pamięć podręczna nie może być falsy")

    def test_zapis_i_odczyt_z_dysku(self):
        from bitget_analyzer.cache import WindowCache

        path = Path(self.tmp.name) / "c.json"
        cache = WindowCache(path)
        key = cache.key("/api/v2/tax/spot-record", 100, 200)
        self.assertIsNone(cache.get(key))
        cache.put(key, [{"id": "1"}])
        cache.save()

        reopened = WindowCache(path)
        self.assertEqual(reopened.get(key), [{"id": "1"}])
        self.assertEqual(reopened.hits, 1)

    def test_granice_okresow_stabilne_miedzy_przebiegami(self):
        from bitget_analyzer.cache import snap

        # Dwa uruchomienia w odstępie minut muszą dać ten sam klucz.
        base = 1_800_000_000_000
        self.assertEqual(snap(base), snap(base + 5 * 60 * 1000))
        self.assertNotEqual(snap(base), snap(base + 2 * 60 * 60 * 1000))


class TestDuzejLiczbyRekordow(unittest.TestCase):
    """Konta z grid botami: rekordy muszą być zwijane, nie trzymane w pamięci."""

    def test_rejestr_jest_agregowany_do_sum_miesiecznych(self):
        from bitget_analyzer.tax import LedgerAggregator

        aggregator = LedgerAggregator("spot")
        # 50 000 drobnych transakcji w jednym miesiącu.
        for index in range(50_000):
            aggregator.add(days_ago(40) + index, "USDT", "trade", 1.5, -0.01)

        entries = list(aggregator.entries())
        self.assertEqual(len(entries), 1, "wszystko z jednego miesiąca to jedna suma")
        self.assertEqual(aggregator.records, 50_000)
        self.assertAlmostEqual(entries[0].amount, 75_000.0, places=4)
        self.assertAlmostEqual(entries[0].fee, -500.0, places=4)
        self.assertIn("50000 operacji", entries[0].business_type)

    def test_sumy_z_pamieci_podrecznej_sa_identyczne(self):
        from bitget_analyzer.tax import LedgerAggregator

        source = LedgerAggregator("futures")
        for index in range(1000):
            source.add(days_ago(35) + index, "USDT", "trade", 0.25, -0.001)
        summary = source.to_summary()

        restored = LedgerAggregator("futures")
        restored.merge_summary(summary)

        self.assertEqual(restored.records, source.records)
        original = list(source.entries())[0]
        recovered = list(restored.entries())[0]
        self.assertAlmostEqual(recovered.amount, original.amount, places=6)
        self.assertAlmostEqual(recovered.fee, original.fee, places=6)
        self.assertEqual(recovered.ts, original.ts)

    def test_podsumowanie_okresu_da_sie_zapisac_w_json(self):
        import json

        from bitget_analyzer.tax import LedgerAggregator

        aggregator = LedgerAggregator("spot")
        aggregator.add(days_ago(40), "BTC", "trade", 0.5, -0.0001)
        aggregator.unknown_types["Nowy typ"] += 2
        # Pamięć podręczna trzyma to jako JSON - musi przejść tam i z powrotem.
        restored = json.loads(json.dumps(aggregator.to_summary()))
        again = LedgerAggregator("spot")
        again.merge_summary(restored)
        self.assertEqual(again.records, 1)
        self.assertEqual(again.unknown_types["Nowy typ"], 2)


class TestPostepuWLogu(unittest.TestCase):
    def test_log_zawiera_zakresy_dat(self):
        import logging

        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("bitget")
        handler = Collector(level=logging.INFO)
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(Path(tmp))
                cfg.requests_per_second = 0
                build_dataset(cfg, FakeClient(cfg))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        postep = [line for line in records if "pobieram okres" in line]
        self.assertTrue(postep, "brak informacji o postępie")
        # Każda linia mówi który okres z ilu i jakich dat dotyczy.
        self.assertTrue(any("Rejestr spot: pobieram okres 1/" in line for line in postep))
        self.assertTrue(any("→" in line for line in postep))


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


class TestZgodnosciPamieciPodrecznej(unittest.TestCase):
    """Plik z poprzedniej wersji nie może cicho dać zerowych sum."""

    def test_stary_format_jest_odrzucany(self):
        import json

        from bitget_analyzer.cache import WindowCache

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache_okresow.json"
            # Format sprzed zmiany: surowe wiersze z API, bez wersji.
            path.write_text(
                json.dumps({"/api/v2/tax/spot-record|1|2": [{"id": "1", "amount": "5"}]}),
                encoding="utf-8",
            )
            cache = WindowCache(path)
            self.assertEqual(len(cache), 0, "stary format musi zostać odrzucony")
            self.assertIsNone(cache.get("/api/v2/tax/spot-record|1|2"))

    def test_nowy_format_przezywa_zapis_i_odczyt(self):
        from bitget_analyzer.cache import FORMAT_VERSION, WindowCache

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            cache = WindowCache(path)
            cache.put("k", [{"b": [["2026-08", "USDT", "trade", 1.0, -0.1, 3, 1]]}])
            cache.save()

            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["__format__"], FORMAT_VERSION)

            reopened = WindowCache(path)
            self.assertEqual(len(reopened), 1)
            self.assertIsNotNone(reopened.get("k"))

    def test_uszkodzony_plik_nie_wywraca_analizy(self):
        from bitget_analyzer.cache import WindowCache

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{to nie jest json", encoding="utf-8")
            cache = WindowCache(path)
            self.assertEqual(len(cache), 0)
