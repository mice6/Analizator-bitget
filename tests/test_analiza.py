"""Testy offline: cała ścieżka liczenia na sztucznych odpowiedziach API.

Uruchomienie:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bitget_analyzer.analysis import Analyzer
from bitget_analyzer.client import BitgetClient, time_windows
from bitget_analyzer.config import Config
from bitget_analyzer.prices import PriceBook
from bitget_analyzer.report import Reporter
from bitget_analyzer.spot import coins_seen
from bitget_analyzer.wallet import fetch_deposits, fetch_transfers, fetch_withdrawals
from bitget_analyzer.spot import fetch_spot_bills, fetch_spot_fills
from bitget_analyzer.futures import fetch_futures_bills
from bitget_analyzer.earn import fetch_savings_history
from bitget_analyzer.valuation import fetch_equity
from bitget_analyzer.model import Dataset


def ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


BTC_DAILY = {
    "2024-02-10": 45000.0,
    "2024-03-15": 40000.0,
    "2024-04-20": 50000.0,
}

FIXTURES = {
    "/api/v2/spot/wallet/deposit-records": [
        {"orderId": "d1", "tradeId": "t1", "coin": "USDT", "size": "1000", "status": "success", "cTime": str(ms("2024-01-05"))},
        {"orderId": "d2", "tradeId": "t2", "coin": "BTC", "size": "0.1", "status": "success", "cTime": str(ms("2024-02-10"))},
        {"orderId": "d3", "tradeId": "t3", "coin": "USDT", "size": "99", "status": "pending", "cTime": str(ms("2024-02-11"))},
    ],
    "/api/v2/spot/wallet/withdrawal-records": [
        {"orderId": "w1", "coin": "USDT", "size": "500", "fee": "1", "status": "success", "cTime": str(ms("2024-05-01"))},
    ],
    "/api/v2/spot/account/transferRecords": [
        {"transferId": "tr1", "coin": "USDT", "size": "300", "fromType": "spot", "toType": "usdt_futures", "cTime": str(ms("2024-03-01"))},
    ],
    "/api/v2/spot/account/bills": [
        {"billId": "b1", "coin": "USDT", "groupType": "financial", "businessType": "INTEREST", "size": "2", "fees": "0", "cTime": str(ms("2024-04-01"))},
        {"billId": "b2", "coin": "USDT", "groupType": "transaction", "businessType": "SELL", "size": "4500", "fees": "-4.5", "cTime": str(ms("2024-04-20"))},
        {"billId": "b3", "coin": "USDT", "groupType": "transfer", "businessType": "TRANSFER_OUT", "size": "-300", "fees": "0", "cTime": str(ms("2024-03-01"))},
    ],
    "/api/v2/spot/trade/fills": [
        {"tradeId": "f1", "orderId": "o1", "symbol": "BTCUSDT", "side": "buy", "priceAvg": "40000", "size": "0.1", "amount": "4000",
         "feeDetail": {"feeCoin": "USDT", "totalFee": "-4"}, "cTime": str(ms("2024-03-15"))},
        {"tradeId": "f2", "orderId": "o2", "symbol": "BTCUSDT", "side": "sell", "priceAvg": "50000", "size": "0.1", "amount": "5000",
         "feeDetail": '{"feeCoin":"USDT","totalFee":"-5"}', "cTime": str(ms("2024-04-20"))},
    ],
    "/api/v2/mix/account/bill": [
        {"billId": "m1", "coin": "USDT", "businessType": "close_long", "amount": "100", "fee": "-0.5", "symbol": "BTCUSDT", "cTime": str(ms("2024-04-05"))},
        {"billId": "m2", "coin": "USDT", "businessType": "contract_settle_fee", "amount": "-3", "fee": "0", "symbol": "BTCUSDT", "cTime": str(ms("2024-04-06"))},
        {"billId": "m3", "coin": "USDT", "businessType": "trans_from_exchange", "amount": "300", "fee": "0", "cTime": str(ms("2024-03-01"))},
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
    ],
    "/api/v2/spot/market/tickers": [
        {"symbol": "BTCUSDT", "lastPr": "60000"},
    ],
}


class FakeClient(BitgetClient):
    """Podstawia sztuczne odpowiedzi zamiast prawdziwych żądań HTTP."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = []

    def request(self, method, path, params=None, auth=True):
        params = params or {}
        self.calls.append((path, dict(params)))

        if path == "/api/v2/spot/market/history-candles":
            symbol = params.get("symbol", "")
            end_time = int(params.get("endTime", 0))
            day = datetime.fromtimestamp((end_time - 1) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if symbol == "BTCUSDT" and day in BTC_DAILY:
                price = BTC_DAILY[day]
                return [[str(end_time), "0", "0", "0", str(price), "0", "0"]]
            return []

        rows = FIXTURES.get(path)
        if rows is None:
            return []

        start = int(params.get("startTime", 0) or 0)
        end = int(params.get("endTime", 0) or 0)
        if start or end:
            filtered = []
            for row in rows:
                ts = int(row.get("cTime", 0) or 0)
                if not ts:
                    filtered.append(row)
                elif (not start or ts >= start) and (not end or ts <= end):
                    filtered.append(row)
            rows = filtered
        return rows


def make_config(out_dir: Path) -> Config:
    return Config(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        out_dir=out_dir,
        requests_per_second=0,
    )


def build_dataset(cfg, client):
    """Pobiera komplet danych ze sztucznego API - odpowiednik pipeline.run()."""
    prices = PriceBook(client)
    prices.load_symbols()
    prices.load_current()

    data = Dataset()
    fetch_deposits(client, cfg, prices, data)
    fetch_withdrawals(client, cfg, prices, data)
    fetch_spot_bills(client, cfg, data)
    fetch_futures_bills(client, cfg, data)
    fetch_spot_fills(client, cfg, prices, data)
    fetch_savings_history(client, cfg, data)
    fetch_transfers(client, cfg, data, coins_seen(data))
    fetch_equity(client, cfg, prices, data)

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
        # 1000 USDT + 0.1 BTC * 45 000 (kurs z 2024-02-10) = 5500
        self.assertAlmostEqual(self.analysis.deposits_total, 5500.0, places=6)
        self.assertEqual(len(self.data.deposits), 2, "wpłata 'pending' nie powinna być liczona")

    def test_wyplaty_pomniejszone_o_prowizje(self):
        self.assertAlmostEqual(self.analysis.withdrawals_total, 499.0, places=6)

    def test_realny_wynik(self):
        # 5500 (portfel) - 5500 (wpłaty) + 499 (wypłaty)
        self.assertAlmostEqual(self.analysis.equity_now, 5500.0, places=6)
        self.assertAlmostEqual(self.analysis.real_pnl, 499.0, places=6)

    def test_zrealizowany_pnl_spot(self):
        # kupno 0.1 BTC za 4000 (+4 prowizji), sprzedaż za 5000 (-5 prowizji)
        self.assertAlmostEqual(self.analysis.spot_realized_total, 991.0, places=6)
        self.assertFalse(self.analysis.uncovered_symbols)
        self.assertEqual(self.analysis.inventory, {})

    def test_futures_rozbite_na_skladniki(self):
        self.assertAlmostEqual(self.analysis.futures_pnl_total, 100.0, places=6)
        self.assertAlmostEqual(self.analysis.futures_funding_total, -3.0, places=6)
        self.assertAlmostEqual(self.analysis.futures_fees_total, -0.5, places=6)

    def test_transfery_nie_wchodza_do_wyniku(self):
        # Transfer 300 USDT ze spota na futures nie może pojawić się w P&L.
        self.assertEqual(self.analysis.transfers_count, 1)
        self.assertAlmostEqual(self.analysis.transfers_volume, 300.0, places=6)
        self.assertAlmostEqual(self.analysis.other_total, 0.0, places=6)

    def test_odsetki_earn(self):
        self.assertAlmostEqual(self.analysis.earn_income_total, 2.0, places=6)

    def test_podzial_miesieczny(self):
        by_month = {row.month: row for row in self.analysis.months}
        self.assertAlmostEqual(by_month["2024-01"].deposits, 1000.0, places=6)
        self.assertAlmostEqual(by_month["2024-02"].deposits, 4500.0, places=6)
        self.assertAlmostEqual(by_month["2024-05"].withdrawals, 499.0, places=6)
        self.assertAlmostEqual(by_month["2024-04"].spot_realized, 991.0, places=6)
        self.assertAlmostEqual(sum(r.deposits for r in self.analysis.months), 5500.0, places=6)

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
            Fill(ts=ms("2024-04-20"), symbol="BTCUSDT", base="BTC", quote="USDT",
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
        start, end = ms("2024-01-01"), ms("2024-12-31")
        windows = list(time_windows(start, end, 30))
        self.assertEqual(windows[0][0], start)
        self.assertEqual(windows[-1][1], end)
        for previous, following in zip(windows, windows[1:]):
            self.assertEqual(following[0], previous[1] + 1)
            self.assertLessEqual(previous[1] - previous[0], 30 * 86_400_000)

    def test_kursor_paginacji(self):
        rows = [{"billId": "50"}, {"billId": "20"}, {"billId": "35"}]
        self.assertEqual(BitgetClient._next_cursor(rows, "billId"), "20")


if __name__ == "__main__":
    unittest.main()


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
