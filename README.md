# Analizator rentowności Bitget

Skrypt w Pythonie, który przez oficjalne **Bitget API v2 (tylko odczyt)** liczy,
ile **realnie** zarobiłeś lub straciłeś na całym koncie — a nie ile pokazuje ROI
pojedynczego bota.

Główne pytanie, na które odpowiada:

```
REALNY WYNIK = (aktualna wartość wszystkich aktywów)
             − (suma wpłat z zewnątrz)
             + (suma wypłat na zewnątrz)
```

To liczba odporna na złudzenia: nie da się jej "poprawić" zyskownym botem, jeśli
gdzie indziej wyparowały pieniądze. Reszta raportu pokazuje, **gdzie** ten wynik
powstał: spot, futures, funding fee, prowizje, Earn — miesiąc po miesiącu.

---

## 1. Bezpieczeństwo — przeczytaj przed uruchomieniem

> **Wygeneruj klucz API wyłącznie do ODCZYTU.**
>
> Bitget → *API Management* → *Create API Key* → **System-generated API key**
> - Uprawnienia: zaznacz **tylko `Read-only`**
> - **NIE** zaznaczaj: `Trade`, `Spot Trade`, `Futures Trade`, `Withdraw`, `Transfer`
> - Ustaw **whitelistę IP** na adres serwera, na którym uruchamiasz skrypt
> - Zapisz **passphrase** — Bitget pokaże go tylko raz

Dodatkowo:

- Klucze czytane są **wyłącznie ze zmiennych środowiskowych / pliku `.env`** —
  nigdy nie wpisuj ich do kodu.
- `.env` jest w `.gitignore`; katalog `raport/` z wynikami również.
- Skrypt wykonuje **tylko żądania `GET` do `api.bitget.com`**. Nie wysyła danych
  do żadnego innego serwisu, nie ma telemetrii, nie korzysta z zewnętrznych API
  kursowych — kurs waluty do wyświetlania podajesz ręcznie (`--fx-rate`).
- Jedyna zależność zewnętrzna to `requests`.

---

## 2. Instalacja

```bash
git clone <adres-repo> Analizator-bitget
cd Analizator-bitget

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env          # wklej BITGET_API_KEY / SECRET / PASSPHRASE
```

Wymagany Python 3.9+.

---

## 3. Uruchomienie

```bash
# ostatni rok (domyślnie)
python3 analizuj.py

# konkretny zakres
python3 analizuj.py --od 2024-01-01 --do 2025-08-31

# wynik pokazywany dodatkowo w PLN (kurs podajesz sam - brak zapytań na zewnątrz)
python3 analizuj.py --od 2024-01-01 --fx-rate 4.05 --fx-label PLN

# szybciej: bez historii transakcji spot (pomija wynik per para)
python3 analizuj.py --od 2024-01-01 --skip fills,positions
```

Najważniejsze opcje:

| Opcja | Znaczenie |
|---|---|
| `--od`, `--do` | zakres analizy (`YYYY-MM-DD`) |
| `--out KATALOG` | gdzie zapisać CSV (domyślnie `raport/`) |
| `--fx-rate`, `--fx-label` | przeliczenie wyświetlanych kwot, np. na PLN |
| `--dodatkowe-przeplywy PLIK.csv` | ręczne uzupełnienie starych wpłat/wypłat |
| `--skip` | pomiń moduły: `wallet,spot,fills,futures,positions,earn,transfers` |
| `--product-types` | domyślnie `USDT-FUTURES,COIN-FUTURES,USDC-FUTURES` |
| `--rps` | limit żądań na sekundę (domyślnie 8) |
| `--csv-sep`, `--csv-decimal` | domyślnie `;` i `,` — CSV otwiera się wprost w polskim Excelu |
| `--json` | dodatkowo `raport/raport.json` |

---

## 4. Co skrypt pobiera

| Dane | Endpoint API v2 | Limit okna |
|---|---|---|
| Wpłaty | `/api/v2/spot/wallet/deposit-records` | 90 dni |
| Wypłaty | `/api/v2/spot/wallet/withdrawal-records` | 90 dni |
| Transfery wewnętrzne | `/api/v2/spot/account/transferRecords` | 90 dni, per moneta |
| Księga spot | `/api/v2/spot/account/bills` | 90 dni |
| Transakcje spot | `/api/v2/spot/trade/fills` | 90 dni |
| Księga futures | `/api/v2/mix/account/bill` | **30 dni** |
| Zamknięte pozycje | `/api/v2/mix/position/history-position` | ~90 dni |
| Earn | `/api/v2/earn/account/assets`, `/api/v2/earn/savings/assets`, `/api/v2/earn/savings/records` | 90 dni |
| Wycena teraz | `/api/v2/account/all-account-balance` + salda spot / funding / futures / earn | — |
| Kursy | `/api/v2/spot/market/tickers`, `/api/v2/spot/market/history-candles` (publiczne) | — |

Zakres jest automatycznie dzielony na okna zgodne z powyższymi limitami, a każde
okno stronicowane kursorem `idLessThan` aż do wyczerpania rekordów. Powtórzenia
na styku okien są odfiltrowywane po identyfikatorach.

Podpis żądania: `ACCESS-SIGN = base64(HMAC_SHA256(secret, timestamp + "GET" +
ścieżka_z_query + body))`, zegar synchronizowany z `/api/v2/public/time`
(Bitget odrzuca podpisy starsze niż ~30 s).

---

## 5. Jak czytać raport

### Sekcja 3 — REALNY ZYSK / STRATA
Twarda liczba. Wpłaty i wypłaty są wyceniane **kursem z dnia operacji**, a nie
dzisiejszym — inaczej wpłata 0,1 BTC z 2023 r. zafałszowałaby cały wynik.
Prowizja za wypłatę jest kosztem, więc jako "odzyskany kapitał" liczy się kwota
netto (`size − fee`).

### Sekcja 4 — z czego się to składa
Rozbicie wyniku na źródła. Suma tych składników **nie musi** równać się liczbie
z sekcji 3 — różnica jest pokazana osobno i bierze się głównie z:

- zmian wyceny monet trzymanych poza spotem (Earn, futures, funding),
- historii starszej niż to, co API jeszcze zwraca (patrz sekcja 8 raportu),
- konwersji, airdropów i operacji, których API nie kategoryzuje.

Traktuj sekcję 3 jako prawdę, a sekcję 4 jako wyjaśnienie.

### Sekcja 5 — miesiąc po miesiącu
To samo rozbicie w ujęciu miesięcznym (pełne dane w `miesiace.csv`). Tu zwykle
widać, że pojedyncze złe miesiące zjadły dorobek kilku dobrych.

### Sekcja 6 — wynik wg pary
Zrealizowany P&L spot liczony metodą **średniej ważonej ceny nabycia**: zakupy
podnoszą podstawę kosztową, sprzedaże realizują różnicę; prowizje są wliczone.
Ranking odpowiada na pytanie "które pary faktycznie zabrały pieniądze",
niezależnie od tego, co pokazuje ROI bota.

### Sekcja 7 — transfery wewnętrzne
Przesunięcia Funding ↔ Spot ↔ Futures ↔ Earn. Są raportowane **wyłącznie do
kontroli** i celowo nie wchodzą do żadnej pozycji wyniku.

### Sekcja 8 — pokrycie danych
Dla każdego źródła: ile rekordów i z jakiego okresu. **Sprawdzaj tę sekcję.**
Jeśli kolumna „Od" jest późniejsza niż Twoja data startu, Bitget nie oddał już
starszej historii i wynik jest niepełny — patrz punkt 7 niżej.

---

## 6. Pliki wynikowe (`raport/`)

| Plik | Zawartość |
|---|---|
| `podsumowanie.csv` | liczby z sekcji 1–4 |
| `miesiace.csv` | rozbicie miesiąc po miesiącu |
| `przeplywy_zewnetrzne.csv` | każda wpłata i wypłata z kursem użytym do wyceny |
| `transfery_wewnetrzne.csv` | transfery między kontami |
| `spot_wynik_wg_pary.csv` | zrealizowany P&L, prowizje i obroty per para |
| `spot_transakcje_zrealizowane.csv` | każde zamknięcie pozycji spot z kosztem i przychodem |
| `ksiega_spot.csv`, `ksiega_futures.csv` | surowe księgi rachunków |
| `saldo_biezace.csv` | aktualne salda per konto i moneta |
| `price_cache.json` | cache kursów dziennych (przyspiesza kolejne uruchomienia) |

---

## 7. Gdy API nie oddaje starszej historii

Bitget przechowuje historię przez ograniczony czas. Jeśli sekcja 8 pokazuje, że
wpłaty zaczynają się później niż w rzeczywistości, uzupełnij brakujące operacje
ręcznie (z wyciągu bankowego lub eksportu z panelu Bitget) w pliku CSV:

```csv
data;typ;moneta;ilosc;wartosc_usd
2022-11-14;deposit;USDT;2500;
2023-01-08;deposit;BTC;0.05;850.20
2023-06-30;withdraw;USDT;1200;
```

- `typ`: `deposit` albo `withdraw`
- `wartosc_usd` możesz zostawić pustą — skrypt wyceni operację kursem z podanego dnia
- akceptowany jest separator `;` i `,` oraz liczby w zapisie `1 234,56`

```bash
python3 analizuj.py --od 2022-01-01 --dodatkowe-przeplywy przyklad_dodatkowe_przeplywy.csv
```

Wzór pliku: `przyklad_dodatkowe_przeplywy.csv`.

---

## 8. Ograniczenia, o których warto wiedzieć

- **Wynik zrealizowany spot** wymaga pełnej historii zakupów. Sprzedaż monety
  kupionej przed początkiem zakresu nie ma znanej ceny nabycia — skrypt **nie
  dopisuje wtedy fikcyjnego zysku**, tylko wymienia takie pary w ostrzeżeniu.
  Lekarstwo: rozszerz `--od`.
- **Futures** liczone są z księgi rachunku (`amount + fee` dla wpisów innych niż
  transfery), co obejmuje P&L pozycji, funding fee i prowizje. Historia
  zamkniętych pozycji służy jako kontrola krzyżowa (API oddaje ~3 miesiące).
- **Earn**: część produktów nie raportuje odsetek w API. Odsetki dopisywane
  bezpośrednio na Spot są jednak widoczne w księdze spot (grupa `financial`).
- **Kursy historyczne** to zamknięcia świec dziennych. Dla operacji w środku dnia
  to przybliżenie (kolumna `zrodlo_kursu` w CSV pokazuje, skąd wziął się kurs).
- **Konta subkont i copy-trading** nie są ujęte — skrypt widzi to samo, co klucz API.

---

## 9. Testy

Pełna ścieżka liczenia jest pokryta testami offline (bez kontaktu z API):

```bash
python3 -m unittest discover -s tests -v
```

Testy sprawdzają m.in. wycenę wpłat kursem historycznym, wyłączenie transferów
wewnętrznych z wyniku, poprawność P&L spot, rozbicie futures na P&L/funding/prowizje,
podpis HMAC-SHA256 oraz podział zakresu na okna czasowe.

---

## 10. Struktura projektu

```
analizuj.py                  # CLI i orkiestracja
bitget_analyzer/
├── config.py                # .env, argumenty, zakres dat
├── client.py                # HMAC-SHA256, retry, rate limit, paginacja
├── prices.py                # kursy bieżące i historyczne + cache
├── model.py                 # struktury danych
├── wallet.py                # wpłaty, wypłaty, transfery
├── spot.py                  # księga spot + transakcje
├── futures.py               # księga futures + zamknięte pozycje
├── earn.py                  # produkty Earn
├── valuation.py             # aktualna wycena portfela
├── analysis.py              # P&L, bilans miesięczny
└── report.py                # konsola + CSV/JSON
tests/test_analiza.py        # testy offline
```
