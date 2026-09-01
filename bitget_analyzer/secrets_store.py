"""Zaszyfrowane przechowywanie kluczy API poza katalogiem projektu.

Model bezpieczeństwa - powiedziane wprost:

* Plik z kluczami jest zaszyfrowany AES-256-GCM. Klucz szyfrujący leży w
  osobnym pliku (`key.bin`) na tej samej maszynie, oba z prawami 0600.
* To chroni przed: przypadkowym commitem do repozytorium, podejrzeniem
  pliku, wyciekiem przez kopię katalogu projektu, wysłaniem logów.
* To NIE chroni przed kimś, kto ma dostęp do Twojego konta na tym serwerze -
  taka osoba przeczyta oba pliki. Drugą warstwą obrony są uprawnienia klucza
  API ograniczone do odczytu (panel to weryfikuje).
* Klucze nigdy nie trafiają do logów ani z powrotem do przeglądarki -
  interfejs pokazuje wyłącznie zamaskowaną postać.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "analizator-bitget"
CREDENTIALS_FILE = "credentials.enc"
KEY_FILE = "key.bin"
AAD = b"analizator-bitget/credentials/v1"
NONCE_BYTES = 12


class SecretsError(RuntimeError):
    """Problem z zapisem lub odczytem zaszyfrowanych kluczy."""


def _require_backend():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - zależy od środowiska
        raise SecretsError(
            "Brak biblioteki 'cryptography', bez niej nie zaszyfruję kluczy.\n"
            "Zainstaluj: pip install -r requirements.txt"
        ) from exc
    return AESGCM


def default_home() -> Path:
    """Katalog na dane aplikacji - poza repozytorium, żeby nie trafił do gita."""
    override = os.environ.get("BITGET_ANALYZER_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


@dataclass
class Credentials:
    api_key: str
    api_secret: str
    api_passphrase: str
    label: str = ""
    saved_at: str = ""

    @property
    def masked_key(self) -> str:
        return mask(self.api_key)

    def is_complete(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def mask(value: str) -> str:
    """Pokazuje tylko ogon sekretu - reszta nigdy nie opuszcza pamięci."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


def _harden(path: Path, is_dir: bool = False) -> None:
    """Ogranicza prawa do właściciela (na Windows os.chmod działa częściowo)."""
    try:
        path.chmod(stat.S_IRWXU if is_dir else stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


class CredentialStore:
    """Szyfrowany magazyn kluczy API w katalogu użytkownika."""

    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home else default_home()
        self.credentials_path = self.home / CREDENTIALS_FILE
        self.key_path = self.home / KEY_FILE

    # ---------------------------------------------------------------- klucz

    def _ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        _harden(self.home, is_dir=True)

    def _load_or_create_key(self) -> bytes:
        if self.key_path.is_file():
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise SecretsError(
                    f"Plik klucza {self.key_path} jest uszkodzony. "
                    "Usuń go i zapisz klucze API ponownie."
                )
            return key
        self._ensure_home()
        key = secrets.token_bytes(32)
        # Tworzymy plik od razu z prawami 0600, żeby nie było okna, w którym
        # klucz jest czytelny dla innych użytkowników systemu.
        descriptor = os.open(
            self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        _harden(self.key_path)
        return key

    # ------------------------------------------------------------- operacje

    def exists(self) -> bool:
        return self.credentials_path.is_file()

    def save(self, credentials: Credentials) -> None:
        if not credentials.is_complete():
            raise SecretsError("Podaj wszystkie trzy wartości: key, secret i passphrase.")

        aesgcm_cls = _require_backend()
        self._ensure_home()
        key = self._load_or_create_key()

        payload = {
            "api_key": credentials.api_key,
            "api_secret": credentials.api_secret,
            "api_passphrase": credentials.api_passphrase,
            "label": credentials.label,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = aesgcm_cls(key).encrypt(
            nonce, json.dumps(payload).encode("utf-8"), AAD
        )

        temporary = self.credentials_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(nonce + ciphertext)
        os.replace(temporary, self.credentials_path)
        _harden(self.credentials_path)

    def load(self) -> Optional[Credentials]:
        if not self.exists():
            return None
        aesgcm_cls = _require_backend()
        if not self.key_path.is_file():
            raise SecretsError(
                f"Brakuje pliku klucza {self.key_path}, a dane są zaszyfrowane. "
                "Wprowadź klucze API ponownie w panelu."
            )
        blob = self.credentials_path.read_bytes()
        if len(blob) <= NONCE_BYTES:
            raise SecretsError("Plik z kluczami jest uszkodzony.")
        key = self._load_or_create_key()
        try:
            plaintext = aesgcm_cls(key).decrypt(
                blob[:NONCE_BYTES], blob[NONCE_BYTES:], AAD
            )
        except Exception as exc:
            raise SecretsError(
                "Nie udało się odszyfrować kluczy (zły plik klucza lub uszkodzone "
                "dane). Wprowadź klucze API ponownie w panelu."
            ) from exc

        data = json.loads(plaintext.decode("utf-8"))
        return Credentials(
            api_key=data.get("api_key", ""),
            api_secret=data.get("api_secret", ""),
            api_passphrase=data.get("api_passphrase", ""),
            label=data.get("label", ""),
            saved_at=data.get("saved_at", ""),
        )

    def delete(self) -> bool:
        """Usuwa zaszyfrowane dane (klucz szyfrujący zostaje - jest bezużyteczny)."""
        removed = False
        for path in (self.credentials_path, self.credentials_path.with_suffix(".tmp")):
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    def describe(self) -> dict:
        """Bezpieczny opis stanu magazynu - bez żadnych sekretów."""
        info = {
            "katalog": str(self.home),
            "zapisane": self.exists(),
            "zamaskowany_klucz": "",
            "zapisano_dnia": "",
            "etykieta": "",
        }
        if not self.exists():
            return info
        try:
            credentials = self.load()
        except SecretsError as exc:
            info["blad"] = str(exc)
            return info
        if credentials:
            info["zamaskowany_klucz"] = credentials.masked_key
            info["zapisano_dnia"] = credentials.saved_at
            info["etykieta"] = credentials.label
        return info


def resolve_credentials(store: Optional[CredentialStore] = None) -> Optional[Credentials]:
    """Kolejność: zmienne środowiskowe/.env, potem zaszyfrowany magazyn."""
    key = os.environ.get("BITGET_API_KEY", "").strip()
    secret = os.environ.get("BITGET_API_SECRET", "").strip()
    passphrase = os.environ.get("BITGET_API_PASSPHRASE", "").strip()
    if key and secret and passphrase:
        return Credentials(key, secret, passphrase, label="zmienne środowiskowe")

    store = store or CredentialStore()
    if store.exists():
        return store.load()
    return None
