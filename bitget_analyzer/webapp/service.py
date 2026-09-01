"""Logika panelu: przechowywanie kluczy, test połączenia, analiza w tle."""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..account import check_api_key
from ..client import BitgetClient
from ..config import Config, ConfigError, create_config
from ..pipeline import STEPS, run
from ..report import Reporter
from ..secrets_store import CredentialStore, Credentials, SecretsError

log = logging.getLogger("bitget.panel")

MAX_LOG_LINES = 400


class LogCollector(logging.Handler):
    """Zbiera logi analizy, żeby pokazać je w przeglądarce."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: List[str] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover
            return
        with self._lock:
            self.lines.append(message)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self.lines)

    def reset(self) -> None:
        with self._lock:
            self.lines.clear()


class Job:
    """Stan pojedynczego uruchomienia analizy."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"          # idle | running | done | error
        self.step = ""
        self.step_index = 0
        self.step_total = len(STEPS)
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error = ""
        self.result: Optional[dict] = None
        self.files: List[str] = []
        self.out_dir: Optional[Path] = None
        self.logs = LogCollector()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "krok": self.step,
                "krok_nr": self.step_index,
                "krokow": self.step_total,
                "start": self.started_at,
                "koniec": self.finished_at,
                "blad": self.error,
                "pliki": list(self.files),
                "katalog": str(self.out_dir) if self.out_dir else "",
                "logi": self.logs.snapshot()[-60:],
                "wynik": self.result,
            }


class PanelService:
    """Warstwa między HTTP a resztą aplikacji."""

    def __init__(self, store: Optional[CredentialStore] = None, out_dir: str = "raport"):
        self.store = store or CredentialStore()
        self.default_out = out_dir
        self.job = Job()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    # --------------------------------------------------------------- klucze

    def credentials_state(self) -> dict:
        return self.store.describe()

    def save_credentials(self, api_key: str, api_secret: str, api_passphrase: str, label: str = "") -> dict:
        credentials = Credentials(
            api_key=(api_key or "").strip(),
            api_secret=(api_secret or "").strip(),
            api_passphrase=(api_passphrase or "").strip(),
            label=(label or "").strip(),
        )
        if not credentials.is_complete():
            raise SecretsError("Wypełnij wszystkie trzy pola: API key, secret i passphrase.")
        self.store.save(credentials)
        log.info("Zapisano zaszyfrowane klucze API w %s", self.store.credentials_path)
        return self.credentials_state()

    def delete_credentials(self) -> dict:
        self.store.delete()
        log.info("Usunięto zapisane klucze API.")
        return self.credentials_state()

    def _load_credentials(self) -> Credentials:
        credentials = self.store.load()
        if not credentials or not credentials.is_complete():
            raise SecretsError("Najpierw zapisz klucze API w panelu.")
        return credentials

    # ------------------------------------------------------------- działanie

    def test_connection(self) -> dict:
        credentials = self._load_credentials()
        cfg = create_config(credentials, out=self.default_out)
        client = BitgetClient(cfg)
        client.sync_time()
        return check_api_key(client)

    def start_analysis(self, params: dict) -> dict:
        # Jeden bieg naraz - inaczej dwa żądania nadpisałyby sobie stan zadania.
        with self._start_lock:
            with self.job.lock:
                if self.job.status == "running":
                    raise ConfigError("Analiza już trwa - poczekaj na zakończenie.")
            return self._start_analysis(params)

    def _start_analysis(self, params: dict) -> dict:
        credentials = self._load_credentials()
        cfg = create_config(
            credentials,
            since=params.get("od") or None,
            to=params.get("do") or None,
            out=params.get("katalog") or self.default_out,
            fx_rate=params.get("fx_rate") or None,
            fx_label=params.get("fx_label") or None,
            skip=params.get("skip") or "",
            extra_flows=params.get("dodatkowe_przeplywy") or None,
            transfer_coins=params.get("monety_transferow") or None,
        )

        self.job = Job()
        job = self.job
        with job.lock:
            job.status = "running"
            job.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            job.out_dir = cfg.out_dir

        self._thread = threading.Thread(
            target=self._run_analysis, args=(cfg, job), daemon=True, name="analiza"
        )
        self._thread.start()
        return job.snapshot()

    def _run_analysis(self, cfg: Config, job: Job) -> None:
        handler = job.logs
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root = logging.getLogger("bitget")
        root.addHandler(handler)

        def progress(key: str, label: str, index: int, total: int) -> None:
            with job.lock:
                job.step = label
                job.step_index = index
                job.step_total = total

        try:
            data, analysis, _ = run(cfg, progress)
            reporter = Reporter(cfg, data, analysis)
            payload = reporter.payload()
            files = reporter.export_csv()
            files.append(reporter.export_json(cfg.out_dir / "raport.json"))
            with job.lock:
                job.result = payload
                job.files = [path.name for path in files]
                job.status = "done"
                job.step = "Gotowe"
                job.step_index = job.step_total
        except Exception as exc:
            log.error("Analiza przerwana: %s", exc)
            log.debug(traceback.format_exc())
            with job.lock:
                job.status = "error"
                job.error = str(exc)
        finally:
            with job.lock:
                job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            root.removeHandler(handler)

    # ----------------------------------------------------------------- pliki

    def file_path(self, name: str) -> Path:
        """Zwraca ścieżkę pliku raportu, blokując wyjście poza katalog wyniku."""
        job = self.job
        base = (job.out_dir or Path(self.default_out)).resolve()
        candidate = (base / Path(name).name).resolve()
        if candidate.parent != base or not candidate.is_file():
            raise FileNotFoundError(name)
        return candidate
