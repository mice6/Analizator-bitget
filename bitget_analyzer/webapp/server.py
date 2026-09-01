"""Serwer panelu - wyłącznie stdlib, nasłuch na 127.0.0.1.

Zabezpieczenia:
* nasłuch domyślnie tylko na loopbacku (bez dostępu z sieci),
* losowy token sesji wymagany w każdym żądaniu (nagłówek X-Panel-Token),
  dzięki czemu strona z internetu nie dobierze się do panelu przez localhost,
* brak ciasteczek - token żyje wyłącznie w pamięci karty przeglądarki,
* odrzucanie żądań z obcym nagłówkiem Origin,
* CSP bez zewnętrznych zasobów, Cache-Control: no-store,
* sekrety nigdy nie wracają do przeglądarki - tylko postać zamaskowana.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..config import ConfigError
from ..secrets_store import SecretsError
from .service import PanelService

log = logging.getLogger("bitget.panel")

INDEX_FILE = Path(__file__).with_name("index.html")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, max-age=0",
}

MAX_BODY_BYTES = 256 * 1024


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "AnalizatorBitget"
    sys_version = ""

    # ---------------------------------------------------------------- pomoc

    @property
    def service(self) -> PanelService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        # Token bywa w ścieżce - nie może trafić nawet do logów diagnostycznych.
        message = (fmt % args).replace(self.token, "***")
        log.debug("%s - %s", self.address_string(), message)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"blad": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Żądanie jest zbyt duże.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("Nieprawidłowy JSON w żądaniu.") from exc
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------- autoryzacja

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def _host_ok(self) -> bool:
        """Blokuje DNS rebinding: obca domena wskazująca na 127.0.0.1."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        allowed = self.server.allowed_hosts  # type: ignore[attr-defined]
        return not host or host in allowed

    def _authorized(self, query: dict) -> bool:
        supplied = self.headers.get("X-Panel-Token") or ""
        if not supplied:
            supplied = (query.get("t") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    # ------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - nazwa wymagana przez BaseHTTPRequestHandler
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if not self._host_ok():
            self._error(
                "Nieoczekiwany nagłówek Host - żądanie odrzucone.", HTTPStatus.FORBIDDEN
            )
            return
        if not self._origin_ok():
            self._error("Żądanie z obcego źródła zostało odrzucone.", HTTPStatus.FORBIDDEN)
            return
        if not self._authorized(query):
            self._error(
                "Brak lub nieprawidłowy token panelu. Otwórz adres wypisany "
                "w terminalu po uruchomieniu panelu.",
                HTTPStatus.UNAUTHORIZED,
            )
            return

        try:
            handler = self._route(method, path)
            if handler is None:
                self._error("Nie znaleziono zasobu.", HTTPStatus.NOT_FOUND)
                return
            handler(query)
        except (SecretsError, ConfigError, ValueError) as exc:
            self._error(str(exc))
        except FileNotFoundError:
            self._error("Nie znaleziono pliku raportu.", HTTPStatus.NOT_FOUND)
        except Exception as exc:  # nieprzewidziane - nie ujawniamy szczegółów
            log.exception("Błąd obsługi żądania %s %s", method, path)
            self._error(f"Błąd serwera: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _route(self, method: str, path: str):
        routes = {
            ("GET", "/"): self._page,
            ("GET", "/api/stan"): self._state,
            ("POST", "/api/klucze"): self._save_credentials,
            ("DELETE", "/api/klucze"): self._delete_credentials,
            ("POST", "/api/test"): self._test,
            ("POST", "/api/analiza"): self._start,
            ("GET", "/api/zadanie"): self._job,
            ("GET", "/api/plik"): self._download,
        }
        return routes.get((method, path))

    # -------------------------------------------------------------- widoki

    def _page(self, query: dict) -> None:
        html = INDEX_FILE.read_text(encoding="utf-8")
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _state(self, query: dict) -> None:
        self._json(
            {
                "klucze": self.service.credentials_state(),
                "zadanie": self.service.job.snapshot(),
                "katalog_domyslny": self.service.default_out,
            }
        )

    def _save_credentials(self, query: dict) -> None:
        body = self._read_json()
        state = self.service.save_credentials(
            body.get("api_key", ""),
            body.get("api_secret", ""),
            body.get("api_passphrase", ""),
            body.get("etykieta", ""),
        )
        self._json({"klucze": state})

    def _delete_credentials(self, query: dict) -> None:
        self._json({"klucze": self.service.delete_credentials()})

    def _test(self, query: dict) -> None:
        self._json(self.service.test_connection())

    def _start(self, query: dict) -> None:
        self._json(self.service.start_analysis(self._read_json()))

    def _job(self, query: dict) -> None:
        self._json(self.service.job.snapshot())

    def _download(self, query: dict) -> None:
        name = (query.get("nazwa") or [""])[0]
        if not name:
            self._error("Podaj nazwę pliku.")
            return
        path = self.service.file_path(name)
        content_type = (
            "application/json; charset=utf-8"
            if path.suffix == ".json"
            else "text/csv; charset=utf-8"
        )
        self._send(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            {"Content-Disposition": f'attachment; filename="{path.name}"'},
        )


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: PanelService, token: str):
        super().__init__(address, PanelHandler)
        self.service = service
        self.token = token
        self.allowed_hosts = {address[0], "localhost", "127.0.0.1", "::1"}


def create_server(
    host: str = "127.0.0.1", port: int = 8770, out_dir: str = "raport"
) -> Tuple[PanelServer, str]:
    """Tworzy serwer panelu i zwraca go razem z adresem do otwarcia."""
    token = secrets.token_urlsafe(24)
    service = PanelService(out_dir=out_dir)
    server = PanelServer((host, port), service, token)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/?t={token}"
    return server, url


def serve(host: str = "127.0.0.1", port: int = 8770, out_dir: str = "raport") -> None:
    server, url = create_server(host, port, out_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="panel")
    thread.start()
    print("\n  Panel analizatora Bitget działa. Otwórz w przeglądarce:\n")
    print(f"     {url}\n")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  UWAGA: panel nasłuchuje na adresie dostępnym z sieci. Trzymaj go\n"
            "  za firewallem albo używaj tunelu SSH:  ssh -L 8770:127.0.0.1:8770 serwer\n"
        )
    print("  Zatrzymanie: Ctrl+C\n")
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("\nZatrzymuję panel...")
    finally:
        server.shutdown()
        server.server_close()
