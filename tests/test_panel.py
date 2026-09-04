"""Testy magazynu kluczy i panelu web (bez kontaktu z API Bitget)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bitget_analyzer.account import _is_write, check_api_key
from bitget_analyzer.secrets_store import (
    CredentialStore,
    Credentials,
    SecretsError,
    mask,
)

KEY = "bg_1234567890abcdef"
SECRET = "sekret-nie-do-pokazania-0001"
PASSPHRASE = "haslo-passphrase-xyz"


class TestMagazynKluczy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self.tmp.name) / "dane")

    def tearDown(self):
        self.tmp.cleanup()

    def test_zapis_i_odczyt(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE, label="konto główne"))
        loaded = self.store.load()
        self.assertEqual(loaded.api_key, KEY)
        self.assertEqual(loaded.api_secret, SECRET)
        self.assertEqual(loaded.api_passphrase, PASSPHRASE)
        self.assertEqual(loaded.label, "konto główne")
        self.assertTrue(loaded.saved_at)

    def test_plik_jest_zaszyfrowany(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE))
        blob = self.store.credentials_path.read_bytes()
        for secret_value in (KEY, SECRET, PASSPHRASE):
            self.assertNotIn(secret_value.encode(), blob, "sekret widoczny w pliku!")

    @unittest.skipIf(os.name == "nt", "prawa POSIX")
    def test_prawa_dostepu(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE))
        self.assertEqual(self.store.credentials_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.home.stat().st_mode & 0o777, 0o700)

    def test_opis_nie_zawiera_sekretow(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE))
        described = json.dumps(self.store.describe(), ensure_ascii=False)
        self.assertNotIn(SECRET, described)
        self.assertNotIn(PASSPHRASE, described)
        self.assertNotIn(KEY, described)
        self.assertIn("cdef", described)  # widoczna tylko końcówka

    def test_usuwanie(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE))
        self.assertTrue(self.store.exists())
        self.store.delete()
        self.assertFalse(self.store.exists())
        self.assertIsNone(self.store.load())

    def test_uszkodzony_klucz_szyfrujacy(self):
        self.store.save(Credentials(KEY, SECRET, PASSPHRASE))
        self.store.key_path.write_bytes(b"\x00" * 32)
        with self.assertRaises(SecretsError):
            self.store.load()

    def test_niekompletne_dane(self):
        with self.assertRaises(SecretsError):
            self.store.save(Credentials(KEY, "", PASSPHRASE))

    def test_maskowanie(self):
        self.assertEqual(mask("abcd"), "****")
        self.assertEqual(mask("bg_1234567890abcdef")[-4:], "cdef")
        self.assertNotIn("bg_123", mask("bg_1234567890abcdef"))
        self.assertEqual(mask(""), "")


class TestUprawnieniaKlucza(unittest.TestCase):
    class FakeClient:
        def __init__(self, authorities):
            self.authorities = authorities

        def request(self, method, path, params=None, auth=True):
            return {"userId": "42", "ips": "", "authorities": self.authorities}

    def test_rozpoznanie_zapisu(self):
        self.assertTrue(_is_write("wwow"))
        self.assertTrue(_is_write("stow"))
        self.assertFalse(_is_write("stor"))
        self.assertFalse(_is_write("p2p"))

    def test_klucz_tylko_do_odczytu(self):
        result = check_api_key(self.FakeClient(["stor", "cpor", "wtor"]))
        self.assertTrue(result["ok"])
        self.assertTrue(result["tylko_odczyt"])
        self.assertFalse(result["moze_wyplacac"])
        # Brak whitelisty IP nadal jest ostrzeżeniem.
        self.assertTrue(any("whitelist" in w for w in result["ostrzezenia"]))

    def test_klucz_z_wyplatami(self):
        result = check_api_key(self.FakeClient(["stor", "wwow"]))
        self.assertFalse(result["tylko_odczyt"])
        self.assertTrue(result["moze_wyplacac"])
        self.assertTrue(any("WYPŁAT" in w for w in result["ostrzezenia"]))

    def test_klucz_z_handlem(self):
        result = check_api_key(self.FakeClient(["stor", "stow"]))
        self.assertFalse(result["tylko_odczyt"])
        self.assertFalse(result["moze_wyplacac"])
        self.assertIn("stow", result["uprawnienia_zapisu"])


class TestPanelHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bitget_analyzer.webapp.server import create_server

        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["BITGET_ANALYZER_HOME"] = str(Path(cls.tmp.name) / "dane")
        cls.out_dir = Path(cls.tmp.name) / "raport"
        cls.out_dir.mkdir(parents=True)
        (cls.out_dir / "miesiace.csv").write_text("miesiac;wynik\n2024-01;10\n", encoding="utf-8")

        cls.server, cls.url = create_server("127.0.0.1", 0, str(cls.out_dir))
        cls.token = cls.server.token
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.environ.pop("BITGET_ANALYZER_HOME", None)
        cls.tmp.cleanup()

    def call(self, path, method="GET", body=None, token=True, headers=None):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-Panel-Token", self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_bez_tokenu_odmowa(self):
        status, _ = self.call("/api/stan", token=False)
        self.assertEqual(status, 401)

    def test_zly_token_odmowa(self):
        status, _ = self.call("/api/stan", headers={"X-Panel-Token": "nie-ten"}, token=False)
        self.assertEqual(status, 401)

    def test_obce_origin_odrzucone(self):
        status, _ = self.call("/api/stan", headers={"Origin": "https://zlosliwa.example"})
        self.assertEqual(status, 403)

    def test_strona_glowna(self):
        status, body = self.call("/?t=" + self.token, token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Analizator rentowno", body)

    def test_zapis_kluczy_i_brak_sekretow_w_odpowiedziach(self):
        status, body = self.call(
            "/api/klucze",
            method="POST",
            body={
                "api_key": KEY,
                "api_secret": SECRET,
                "api_passphrase": PASSPHRASE,
                "etykieta": "test",
            },
        )
        self.assertEqual(status, 200, body)

        status, body = self.call("/api/stan")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(PASSPHRASE, text)
        self.assertNotIn(KEY, text)
        self.assertIn("cdef", text)

        status, _ = self.call("/api/klucze", method="DELETE")
        self.assertEqual(status, 200)
        status, body = self.call("/api/stan")
        self.assertFalse(json.loads(body)["klucze"]["zapisane"])

    def test_analiza_bez_kluczy_konczy_sie_bledem(self):
        status, body = self.call("/api/analiza", method="POST", body={"od": "2024-01-01"})
        self.assertEqual(status, 400)
        self.assertIn("klucze", json.loads(body)["blad"].lower())

    def test_pobieranie_pliku(self):
        status, body = self.call("/api/plik?nazwa=miesiace.csv")
        self.assertEqual(status, 200)
        self.assertIn(b"2024-01", body)

    def test_blokada_wyjscia_poza_katalog(self):
        for probe in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"):
            status, _ = self.call("/api/plik?nazwa=" + probe)
            self.assertEqual(status, 404, f"przeszło: {probe}")

    def test_naglowki_bezpieczenstwa(self):
        request = urllib.request.Request(self.base + "/?t=" + self.token)
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = dict(response.headers)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", headers["Cache-Control"])

    def test_obcy_host_odrzucony(self):
        # DNS rebinding: obca domena wskazująca na 127.0.0.1.
        status, _ = self.call("/api/stan", headers={"Host": "zlosliwa.example"})
        self.assertEqual(status, 403)

    def test_token_nie_trafia_do_logow(self):
        import logging

        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("bitget.panel")
        handler = Collector(level=logging.DEBUG)
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self.call("/?t=" + self.token, token=False)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertTrue(records)
        self.assertFalse([line for line in records if self.token in line])

    def test_nasluch_tylko_na_loopbacku(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()


class TestPrzebiegAnalizyWPanelu(unittest.TestCase):
    """Analiza uruchamiana z panelu: wątek, postęp, wynik i pliki CSV."""

    def test_pelny_przebieg(self):
        import time

        from bitget_analyzer.webapp import service as service_module
        from bitget_analyzer.webapp.service import PanelService

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_analiza import FakeClient, build_dataset

        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp) / "dane")
            store.save(Credentials(KEY, SECRET, PASSPHRASE))
            panel = PanelService(store=store, out_dir=str(Path(tmp) / "raport"))

            def fake_run(cfg, progress=None):
                if progress:
                    progress("wallet", "Historia wpłat i wypłat", 2, 10)
                return build_dataset(cfg, FakeClient(cfg))

            original = service_module.run
            service_module.run = fake_run
            try:
                panel.start_analysis({})
                for _ in range(100):
                    if panel.job.snapshot()["status"] != "running":
                        break
                    time.sleep(0.05)
            finally:
                service_module.run = original

            snapshot = panel.job.snapshot()
            self.assertEqual(snapshot["status"], "done", snapshot.get("blad"))

            result = snapshot["wynik"]
            self.assertAlmostEqual(result["realny_wynik_usdt"], 249.0, places=6)
            self.assertAlmostEqual(result["wplaty_usdt"], 5800.0, places=6)
            self.assertTrue(result["miesiace"])
            self.assertTrue(result["pokrycie"])

            self.assertIn("miesiace.csv", snapshot["pliki"])
            self.assertIn("raport.json", snapshot["pliki"])
            for name in snapshot["pliki"]:
                self.assertTrue(panel.file_path(name).is_file())

            # Wynik przekazywany do przeglądarki nie może zawierać sekretów.
            serialized = json.dumps(snapshot, ensure_ascii=False, default=str)
            for secret_value in (KEY, SECRET, PASSPHRASE):
                self.assertNotIn(secret_value, serialized)


class TestZajetyPort(unittest.TestCase):
    """Zajęty port ma dawać instrukcję, a nie traceback."""

    def test_czytelny_komunikat(self):
        from bitget_analyzer.webapp.server import PortBusyError, create_server

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BITGET_ANALYZER_HOME"] = str(Path(tmp) / "dane")
            self.addCleanup(os.environ.pop, "BITGET_ANALYZER_HOME", None)

            first, _ = create_server("127.0.0.1", 0, tmp)
            self.addCleanup(first.server_close)
            busy_port = first.server_address[1]

            with self.assertRaises(PortBusyError) as ctx:
                create_server("127.0.0.1", busy_port, tmp)

            message = str(ctx.exception)
            self.assertIn("pkill", message)
            self.assertIn(str(busy_port + 1), message)
            self.assertIn("tunel SSH", message)

    def test_port_zero_wybiera_wolny(self):
        from bitget_analyzer.webapp.server import create_server

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BITGET_ANALYZER_HOME"] = str(Path(tmp) / "dane")
            self.addCleanup(os.environ.pop, "BITGET_ANALYZER_HOME", None)
            server, url = create_server("127.0.0.1", 0, tmp)
            self.addCleanup(server.server_close)
            self.assertGreater(server.server_address[1], 0)
            self.assertIn(str(server.server_address[1]), url)


class TestTrybuHistorii(unittest.TestCase):
    """Wybór 'szybka' ma pomijać rejestry podatkowe (limit 1 zapytanie/s)."""

    def test_mapowanie_parametru(self):
        from bitget_analyzer.webapp.service import _skip_from

        self.assertEqual(_skip_from({}), "")
        self.assertEqual(_skip_from({"historia": "pelna"}), "")
        self.assertEqual(_skip_from({"historia": "szybka"}), "rejestry")
        self.assertEqual(_skip_from({"historia": "szybka", "skip": "earn"}), "earn,rejestry")

    def test_tryb_szybki_nie_dotyka_rejestrow(self):
        import sys
        from pathlib import Path as P

        sys.path.insert(0, str(P(__file__).resolve().parent))
        from test_analiza import FakeClient, make_config

        from bitget_analyzer.pipeline import collect
        from bitget_analyzer.prices import PriceBook

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            cfg.requests_per_second = 0
            cfg.skip = ["rejestry"]
            client = FakeClient(cfg)
            data = collect(cfg, client, PriceBook(client))

        rejestry = [
            p for p, _ in client.calls
            if p in ("/api/v2/tax/spot-record", "/api/v2/tax/future-record")
        ]
        self.assertEqual(rejestry, [], "tryb szybki nie może pytać o rejestry")
        # P2P to kapitał zewnętrzny - potrzebny w każdym trybie.
        p2p = [p for p, _ in client.calls if p == "/api/v2/tax/p2p-record"]
        self.assertTrue(p2p, "P2P musi być pobrane także w trybie szybkim")
        # Zamiast tego wchodzi księga rachunku.
        bills = [p for p, _ in client.calls if "account/bills" in p]
        self.assertTrue(bills, "powinien sięgnąć po księgę spot")
        # Dane pochodzą wtedy z księgi rachunku, nie z rejestru.
        self.assertTrue(data.spot_ledger)
        self.assertTrue(data.futures_ledger)
        self.assertTrue(any(e.category == "earn" for e in data.spot_ledger))
