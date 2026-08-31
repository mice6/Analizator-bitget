"""Klient HTTP do Bitget API v2: podpisywanie HMAC-SHA256, retry, paginacja.

Wszystkie żądania są typu GET (read-only). Skrypt nie wysyła nigdzie danych
poza oficjalnym API Bitget.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests

from .config import Config

log = logging.getLogger("bitget")

SUCCESS_CODE = "00000"

# Kody błędów, przy których ponawiamy żądanie (limit zapytań / chwilowy problem).
RETRYABLE_CODES = {
    "429",
    "40018",  # too many requests
    "40725",  # service busy
    "45110",  # frequency limit
    "50001",
    "50067",
}

# Kody oznaczające brak uprawnień - nie ma sensu ponawiać, ale nie przerywamy
# całej analizy (np. klucz bez dostępu do Earn).
PERMISSION_CODES = {"40014", "40012", "40037", "40034", "40309"}

# Pola, po których Bitget stronicuje (parametr idLessThan).
CURSOR_FIELDS: Tuple[str, ...] = (
    "billId",
    "tradeId",
    "orderId",
    "transferId",
    "positionId",
    "id",
    "recordId",
)


class BitgetError(RuntimeError):
    """Błąd zwrócony przez API Bitget."""

    def __init__(self, code: str, msg: str, endpoint: str):
        super().__init__(f"[{code}] {msg} ({endpoint})")
        self.code = code
        self.msg = msg
        self.endpoint = endpoint

    @property
    def is_permission_error(self) -> bool:
        return self.code in PERMISSION_CODES


class RateLimiter:
    """Prosty ogranicznik: minimalny odstęp między żądaniami."""

    def __init__(self, requests_per_second: float):
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


def extract_list(data: Any) -> List[dict]:
    """Wyciąga listę rekordów z odpowiedzi (Bitget bywa niekonsekwentny)."""
    if data is None:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("resultList", "list", "data", "records", "rows", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
        return [data]
    return []


def time_windows(start_ms: int, end_ms: int, window_days: int) -> Iterator[Tuple[int, int]]:
    """Dzieli zakres czasu na okna zgodne z limitami API (np. 90 dni)."""
    span = window_days * 24 * 60 * 60 * 1000 - 1000
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + span, end_ms)
        yield cursor, chunk_end
        cursor = chunk_end + 1


class BitgetClient:
    """Read-only klient Bitget API v2."""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "analizator-bitget/1.0 (read-only)"})
        self._limiter = RateLimiter(cfg.requests_per_second)
        self._time_offset_ms = 0
        self.request_count = 0
        self.retry_count = 0

    # ------------------------------------------------------------------ auth

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000) + self._time_offset_ms)

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.cfg.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def sync_time(self) -> None:
        """Synchronizuje zegar z serwerem (Bitget odrzuca podpis > 30 s różnicy)."""
        try:
            data = self.request("GET", "/api/v2/public/time", auth=False)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("Nie udało się zsynchronizować czasu: %s", exc)
            return
        server_ms = None
        if isinstance(data, dict):
            server_ms = data.get("serverTime") or data.get("ts")
        if server_ms:
            self._time_offset_ms = int(server_ms) - int(time.time() * 1000)
            if abs(self._time_offset_ms) > 1000:
                log.info(
                    "Korekta czasu względem serwera Bitget: %+d ms", self._time_offset_ms
                )

    # --------------------------------------------------------------- request

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Any:
        """Wykonuje pojedyncze żądanie i zwraca pole `data` z odpowiedzi."""
        clean = {
            key: str(value)
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        # Podpis musi obejmować dokładnie ten query string, który wysyłamy.
        query = urlencode(sorted(clean.items()))
        request_path = path + (f"?{query}" if query else "")
        url = self.cfg.base_url + request_path

        last_error: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            headers = {"Content-Type": "application/json", "locale": "en-US"}
            if auth:
                timestamp = self._timestamp()
                headers.update(
                    {
                        "ACCESS-KEY": self.cfg.api_key,
                        "ACCESS-SIGN": self._sign(timestamp, method, request_path),
                        "ACCESS-TIMESTAMP": timestamp,
                        "ACCESS-PASSPHRASE": self.cfg.api_passphrase,
                    }
                )

            self._limiter.wait()
            self.request_count += 1
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=self.cfg.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                self._backoff(attempt, f"błąd sieci: {exc}")
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                self._backoff(attempt, f"HTTP {response.status_code} z {path}")
                continue

            try:
                payload = response.json()
            except ValueError:
                last_error = RuntimeError(f"Odpowiedź nie jest JSON-em: {response.text[:200]}")
                self._backoff(attempt, "nieprawidłowy JSON")
                continue

            code = str(payload.get("code", ""))
            if code == SUCCESS_CODE:
                return payload.get("data")

            msg = str(payload.get("msg", ""))
            if code in RETRYABLE_CODES or "frequen" in msg.lower() or "too many" in msg.lower():
                last_error = BitgetError(code, msg, path)
                self._backoff(attempt, f"limit zapytań ({code} {msg})")
                continue

            raise BitgetError(code, msg, path)

        raise RuntimeError(f"Nie udało się pobrać {path}: {last_error}")

    def _backoff(self, attempt: int, reason: str) -> None:
        if attempt >= self.cfg.max_retries:
            return
        self.retry_count += 1
        delay = min(2 ** attempt, 16) + random.uniform(0, 0.5)
        log.warning("Ponawiam za %.1fs (%s)", delay, reason)
        time.sleep(delay)

    # -------------------------------------------------------------- paginacja

    def paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        limit: Optional[int] = None,
        cursor_field: Optional[str] = None,
        cursor_param: str = "idLessThan",
        max_pages: int = 2000,
    ) -> Iterator[dict]:
        """Stronicowanie kursorem `idLessThan` (od najnowszych do najstarszych)."""
        page_size = limit or self.cfg.page_limit
        base = dict(params or {})
        base["limit"] = page_size
        cursor: Optional[str] = None
        seen_cursors = set()

        for page in range(max_pages):
            call_params = dict(base)
            if cursor is not None:
                call_params[cursor_param] = cursor
            rows = extract_list(self.request("GET", path, call_params))
            if not rows:
                return
            for row in rows:
                yield row
            if len(rows) < page_size:
                return

            field = cursor_field or self._detect_cursor_field(rows[0])
            if not field:
                log.warning(
                    "Brak pola kursora w odpowiedzi %s - przerywam stronicowanie "
                    "(część starszych rekordów mogła nie zostać pobrana).",
                    path,
                )
                return
            next_cursor = self._next_cursor(rows, field)
            if next_cursor is None or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        log.warning("Osiągnięto limit %d stron dla %s.", max_pages, path)

    def paginate_windows(
        self,
        path: str,
        params: Optional[Dict[str, Any]],
        start_ms: int,
        end_ms: int,
        window_days: int,
        **kwargs: Any,
    ) -> Iterator[dict]:
        """Paginacja z podziałem zakresu czasu na okna wymagane przez API."""
        for window_start, window_end in time_windows(start_ms, end_ms, window_days):
            call_params = dict(params or {})
            call_params["startTime"] = window_start
            call_params["endTime"] = window_end
            yield from self.paginate(path, call_params, **kwargs)

    @staticmethod
    def _detect_cursor_field(row: dict) -> Optional[str]:
        for field in CURSOR_FIELDS:
            if row.get(field) not in (None, ""):
                return field
        return None

    @staticmethod
    def _next_cursor(rows: Sequence[dict], field: str) -> Optional[str]:
        values = [str(row.get(field)) for row in rows if row.get(field) not in (None, "")]
        if not values:
            return None
        if all(value.isdigit() for value in values):
            return str(min(int(value) for value in values))
        return values[-1]


def dedupe(rows: Iterable[dict], *keys: str) -> List[dict]:
    """Usuwa duplikaty (okna czasowe potrafią się stykać na granicy)."""
    out: List[dict] = []
    seen = set()
    for row in rows:
        marker = tuple(str(row.get(key, "")) for key in keys) if keys else None
        if marker is None or not any(marker):
            marker = json.dumps(row, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(row)
    return out
