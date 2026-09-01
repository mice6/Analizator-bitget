"""Weryfikacja klucza API: czy działa i czy naprawdę jest tylko do odczytu."""

from __future__ import annotations

from typing import Dict, List

from .client import BitgetClient, BitgetError

ACCOUNT_INFO_PATH = "/api/v2/spot/account/info"

# Kody uprawnień zwracane przez Bitget w polu `authorities`.
# Kończące się na "r" to odczyt, na "w" - zapis.
PERMISSION_LABELS: Dict[str, str] = {
    "coor": "Futures - zlecenia (odczyt)",
    "cpor": "Futures - pozycje (odczyt)",
    "stor": "Spot - handel (odczyt)",
    "smor": "Margin (odczyt)",
    "ttor": "Copy trading (odczyt)",
    "wtor": "Portfel - transfery (odczyt)",
    "taxr": "Dane podatkowe (odczyt)",
    "chor": "Subkonta (odczyt)",
    "p2pr": "P2P (odczyt)",
    "pllr": "Pożyczki pod zastaw (odczyt)",
    "coow": "Futures - zlecenia (ZAPIS: składanie zleceń)",
    "cpow": "Futures - pozycje (ZAPIS)",
    "stow": "Spot - handel (ZAPIS: składanie zleceń)",
    "smow": "Margin (ZAPIS)",
    "ttow": "Copy trading (ZAPIS)",
    "wtow": "Portfel - transfery (ZAPIS: przenoszenie środków)",
    "wwow": "Portfel - WYPŁATY (ZAPIS: wyprowadzanie środków)",
    "chow": "Subkonta (ZAPIS)",
    "pllw": "Pożyczki pod zastaw (ZAPIS)",
    "taxw": "Dane podatkowe (ZAPIS)",
    "p2p": "P2P",
}

# Uprawnienie, którego obecność jest najgroźniejsza.
WITHDRAW_CODE = "wwow"


def _is_write(code: str) -> bool:
    return code.endswith("w") and code != "p2p"


def check_api_key(client: BitgetClient) -> dict:
    """Sprawdza połączenie i zwraca opis uprawnień klucza.

    Nie zwraca żadnych sekretów - tylko identyfikator konta, whitelistę IP
    i listę uprawnień w czytelnej formie.
    """
    try:
        data = client.request("GET", ACCOUNT_INFO_PATH)
    except BitgetError as exc:
        return {
            "ok": False,
            "blad": f"[{exc.code}] {exc.msg}",
            "podpowiedz": _hint_for(exc.code),
        }
    except Exception as exc:  # problemy sieciowe, timeouty
        return {"ok": False, "blad": str(exc), "podpowiedz": ""}

    if not isinstance(data, dict):
        data = {}

    raw = data.get("authorities") or []
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]

    permissions: List[dict] = []
    write_codes: List[str] = []
    for code in raw:
        code = str(code).strip()
        if not code:
            continue
        write = _is_write(code)
        if write:
            write_codes.append(code)
        permissions.append(
            {
                "kod": code,
                "opis": PERMISSION_LABELS.get(code, "nieznane uprawnienie"),
                "zapis": write,
            }
        )

    ips = data.get("ips") or ""
    if isinstance(ips, list):
        ips = ", ".join(str(item) for item in ips)

    result = {
        "ok": True,
        "user_id": str(data.get("userId", "")),
        "ips": str(ips),
        "uprawnienia": permissions,
        "uprawnienia_zapisu": write_codes,
        "moze_wyplacac": WITHDRAW_CODE in write_codes,
        "tylko_odczyt": not write_codes,
        "ostrzezenia": [],
    }

    if result["moze_wyplacac"]:
        result["ostrzezenia"].append(
            "Ten klucz ma uprawnienie do WYPŁAT. Do analizy nie jest potrzebne - "
            "usuń klucz w panelu Bitget i wygeneruj nowy, tylko do odczytu."
        )
    elif write_codes:
        result["ostrzezenia"].append(
            "Klucz ma uprawnienia do zapisu ("
            + ", ".join(write_codes)
            + "). Analiza wymaga wyłącznie odczytu - bezpieczniej wygenerować "
            "nowy klucz bez tych uprawnień."
        )
    if not result["ips"]:
        result["ostrzezenia"].append(
            "Klucz nie ma whitelisty IP - może być użyty z dowolnego adresu. "
            "Ustaw IP serwera w panelu Bitget."
        )
    return result


def _hint_for(code: str) -> str:
    hints = {
        "40001": "Sprawdź, czy klucz, sekret i passphrase są kompletne.",
        "40006": "Nieprawidłowy podpis - najczęściej zły API secret.",
        "40009": "Nieprawidłowy podpis - sprawdź API secret i passphrase.",
        "40011": "Brak lub zły passphrase.",
        "40012": "Nieprawidłowy klucz API.",
        "40018": "Zbyt wiele zapytań - odczekaj chwilę.",
        "40037": "Klucz API nie istnieje lub został usunięty.",
        "40099": "Adres IP spoza whitelisty klucza.",
    }
    return hints.get(code, "")
