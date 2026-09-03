# Analizator rentowności Bitget

Skrypt w Pythonie, który przez oficjalne **Bitget API v2 (tylko odczyt)** liczy,
ile **realnie** zarobiłeś lub straciłeś na całym koncie — a nie ile pokazuje ROI
pojedynczego bota.

Klucze API podajesz w **panelu w przeglądarce**; są zapisywane w zaszyfrowanej
postaci poza katalogiem projektu, więc nie mogą trafić do repozytorium. Analizę
uruchamiasz z panelu albo z terminala.

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

Panel sam to weryfikuje: przycisk „Sprawdź połączenie i uprawnienia” odpytuje
`/api/v2/spot/account/info` i wypisuje faktyczne uprawnienia klucza. Jeśli klucz
ma prawo do wypłat (`wwow`) albo do handlu (`stow`, `coow`…), zobaczysz czerwone
ostrzeżenie z nazwą uprawnienia.

### Jak przechowywane są klucze

Klucze podajesz w panelu w przeglądarce. Zapisywane są w
`~/.config/analizator-bitget/credentials.enc`, zaszyfrowane **AES-256-GCM**;
klucz szyfrujący leży obok w `key.bin`. Oba pliki mają prawa `0600`, katalog `0700`,
i leżą **poza repozytorium** — nie mogą trafić do gita nawet przez `git add -A`.

**Co to realnie daje, a czego nie daje.** Zrezygnowaliśmy z hasła głównego, więc
klucz szyfrujący musi leżeć na tej samej maszynie co zaszyfrowany plik. To chroni przed:

- przypadkowym commitem kluczy do repozytorium,
- podejrzeniem pliku, zrzutem ekranu, wysłaniem logów czy kopii katalogu projektu,
- odczytaniem kluczy z samego panelu (interfejs pokazuje wyłącznie `****cdef`) —
  raz zapisanego sekretu nie da się wyświetlić z powrotem.

To **nie** chroni przed kimś, kto ma dostęp do Twojego konta na tym serwerze —
taka osoba przeczyta oba pliki. Drugą warstwą obrony są tu uprawnienia klucza
ograniczone do odczytu: najgorsze, co ktoś taki może zrobić, to podejrzeć historię
konta. Wypłacić środków nie może.

Chcesz mocniejszej ochrony? Wtedy potrzebne jest hasło główne podawane przy
każdym uruchomieniu (klucz wyprowadzany funkcją scrypt zamiast trzymany w pliku) —
napisz, dołożę to jako opcję.

### Bezpieczeństwo samego panelu

- nasłuch **tylko na `127.0.0.1`** — panel nie jest widoczny z sieci,
- każde żądanie wymaga **losowego tokenu sesji** generowanego przy starcie i
  wypisywanego w terminalu; bez niego panel zwraca `401`. To blokuje sytuację,
  w której dowolna strona w internecie próbuje odpytać Twój `localhost`,
- brak ciasteczek — token żyje wyłącznie w pamięci karty przeglądarki,
- żądania z obcym nagłówkiem `Origin` są odrzucane (`403`),
- CSP bez zewnętrznych zasobów: żadnego CDN-a, fontów ani skryptów z sieci,
- pobieranie plików ograniczone do katalogu raportu (próby `../` kończą się `404`),
- serwer to czysta biblioteka standardowa Pythona — bez frameworka webowego.

Cały ruch wychodzący to **wyłącznie żądania `GET` do `api.bitget.com`**. Bez
telemetrii, bez zewnętrznych API kursowych — kurs waluty do wyświetlania
podajesz ręcznie.

## 2. Instalacja

Stawiasz to na serwerze (Oracle Cloud, VPS)? Pełna instrukcja krok po kroku,
razem z tunelem SSH i whitelistą IP: **[INSTALL.md](INSTALL.md)**.

Lokalnie wystarczy:

```bash
git clone <adres-repo> Analizator-bitget
cd Analizator-bitget

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Wymagany Python 3.9+. Zależności: `requests` i `cryptography` (szyfrowanie kluczy).

Kluczy **nie** wpisujesz do żadnego pliku — podajesz je w panelu przy pierwszym
uruchomieniu. Jeśli wolisz zmienne środowiskowe (np. do crona), skopiuj
`.env.example` do `.env`; mają wtedy pierwszeństwo przed panelem.

---

## 3. Panel w przeglądarce

```bash
python3 panel.py
```

W terminalu pojawi się adres z tokenem, np.
`http://127.0.0.1:8770/?t=LMyY1-rcywHNTs9YKBsIwciKGLyItdU_` — otwórz go w przeglądarce.
Token zmienia się przy każdym starcie panelu.

Panel ma trzy kroki:

1. **Klucze API** — wpisujesz key / secret / passphrase, zapisujesz (szyfrowanie
   następuje od razu), sprawdzasz połączenie i uprawnienia klucza.
2. **Zakres analizy** — daty, opcjonalny kurs PLN, katalog na CSV. Przycisk
   „Analizuj” uruchamia pobieranie w tle; widzisz pasek postępu i log na żywo.
3. **Wynik** — realny zysk/strata, rozbicie na składniki, tabela miesiąc po
   miesiącu, ranking par, pokrycie danych, ostrzeżenia i linki do plików CSV.

Opcje: `--port 8770`, `--out raport`, `--otworz` (otwiera przeglądarkę),
`-v` (więcej logów).

### Praca na serwerze bez pulpitu

Nie wystawiaj panelu na świat — użyj tunelu SSH:

```bash
# na swoim komputerze
ssh -L 8770:127.0.0.1:8770 uzytkownik@serwer

# w sesji SSH, na serwerze
cd Analizator-bitget && python3 panel.py
```

Potem otwierasz `http://127.0.0.1:8770/?t=...` u siebie. Panel nadal nasłuchuje
wyłącznie na loopbacku serwera. `--host 0.0.0.0` istnieje, ale wypisuje wtedy
ostrzeżenie — używaj tylko za firewallem i świadomie.

## 4. Uruchomienie z terminala (bez panelu)

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
| `--rps` | globalny limit żądań na sekundę (domyślnie 8) |
| `--csv-sep`, `--csv-decimal` | domyślnie `;` i `,` — CSV otwiera się wprost w polskim Excelu |
| `--json` | dodatkowo `raport/raport.json` |

---

## 5. Co skrypt pobiera

| Dane | Endpoint API v2 | Jak daleko wstecz |
|---|---|---|
| Wpłaty | `/api/v2/spot/wallet/deposit-records` | pełna historia (okna 89 dni) |
| Wypłaty | `/api/v2/spot/wallet/withdrawal-records` | pełna historia (okna 89 dni) |
| **Rejestr spot** | `/api/v2/tax/spot-record` | **~2 lata** (okna 30 dni) |
| **Rejestr futures** | `/api/v2/tax/future-record` | **~2 lata** (okna 30 dni) |
| Szczegóły transakcji spot | `/api/v2/spot/trade/fills` | ~90 dni |
| Zamknięte pozycje | `/api/v2/mix/position/history-position` | ~90 dni |
| Transfery wewnętrzne | `/api/v2/spot/account/transferRecords` | ~90 dni, per moneta |
| Księga spot (awaryjnie) | `/api/v2/spot/account/bills` | ~90 dni |
| Księga futures (awaryjnie) | `/api/v2/mix/account/bill` | okna 30 dni |
| Earn | `/api/v2/earn/account/assets`, `/api/v2/earn/savings/*` | okna 89 dni |
| Wycena teraz | `/api/v2/account/all-account-balance` + salda kont | — |
| Kursy | `/api/v2/spot/market/tickers`, `history-candles` (publiczne) | — |

Rodzina `tax/*` jest rozliczana przez Bitget **wspólnym limitem 1 zapytania na
sekundę** (reszta endpointów znosi 8–10), więc te ścieżki dzielą jeden licznik
tempa. Po odbiciu limitem skrypt zwalnia (5 s, 15 s, 30 s, 60 s), a gdy endpoint
odbija mimo wszystko — **pomija go i idzie dalej**, przechodząc na źródło
awaryjne, zamiast wysadzać cały przebieg albo mielić w nieskończoność.
Pobranie dwóch lat historii zajmuje około minuty.

Pobrane okresy trafiają do **pamięci podręcznej** (`raport/cache_okresow.json`),
więc powtórne uruchomienie analizy nie pyta o nie ponownie. To istotne, bo pula
zapytań do rejestrów podatkowych jest wąska i kilka przebiegów pod rząd potrafi
ją wyczerpać — wtedy trzeba odczekać kilkanaście minut. Skasowanie tego pliku
wymusza pełne pobranie od nowa.

W logu widać, który okres jest właśnie pobierany (`Rejestr spot: pobieram
okres 7/25 (2026-02-05 → 2026-03-07)...`), więc od razu wiadomo, czy analiza
posuwa się do przodu, czy stoi.

Podstawą historii są **rejestry podatkowe** (`tax/*-record`) — sięgają około dwóch
lat, podczas gdy księgi rachunków oddają tylko ostatnie 90 dni. Księgi zostają
jako źródło awaryjne, używane dopiero gdy rejestr nic nie zwróci.

Zakres jest dzielony na okna **liczone wstecz od daty końcowej**, żeby najnowsze
okno zawsze mieściło się w oknie retencji API. Idziemy od najnowszych do
najstarszych i przerywamy, gdy API odmówi z powodu limitu historii — starsze
okna i tak nie mają szans, a każde kosztowałoby zapytanie. Jeśli realna granica
retencji okaże się węższa, niż zakłada skrypt, najnowsze okno jest automatycznie
zawężane aż API je przyjmie.

Błąd jednego okna **nigdy** nie unieważnia danych pobranych z okien nowszych —
zamiast tego trafia do ostrzeżeń i do tabeli pokrycia. Każde okno jest
stronicowane kursorem `idLessThan`, a powtórzenia na styku okien odfiltrowywane
po identyfikatorach.

Podpis żądania: `ACCESS-SIGN = base64(HMAC_SHA256(secret, timestamp + "GET" +
ścieżka_z_query + body))`, zegar synchronizowany z `/api/v2/public/time`
(Bitget odrzuca podpisy starsze niż ~30 s).

---

## 6. Jak czytać raport

### Sekcja 3 — REALNY ZYSK / STRATA
Twarda liczba. Wpłaty i wypłaty są wyceniane **kursem z dnia operacji**, a nie
dzisiejszym — inaczej wpłata 0,1 BTC z 2023 r. zafałszowałaby cały wynik.
Prowizja za wypłatę jest kosztem, więc jako "odzyskany kapitał" liczy się kwota
netto (`size − fee`).

### Sekcja 4 — z czego się to składa
Rozbicie wyniku na źródła. Suma tych składników **nie musi** równać się liczbie
z sekcji 3 — różnica jest pokazana osobno i bierze się głównie z:

- zmian wyceny monet trzymanych poza spotem (Earn, futures, funding),
- historii starszej niż to, co API jeszcze zwraca (patrz „Pokrycie danych”),
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

### Pokrycie danych
Dla każdego źródła: ile rekordów i z jakiego okresu. **Sprawdzaj tę sekcję.**
Jeśli kolumna „Od" jest późniejsza niż Twoja data startu, Bitget nie oddał już
starszej historii i wynik jest niepełny — patrz punkt 7 niżej.

---

## 7. Pliki wynikowe (`raport/`)

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

## 8. Gdy API nie oddaje starszej historii

Bitget przechowuje historię przez ograniczony czas. Jeśli sekcja z pokryciem danych pokazuje, że
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

## 9. Ograniczenia, o których warto wiedzieć

- **Bitget przechowuje historię transakcyjną około 2 lat.** Ustawianie `--od`
  wcześniej niż dwa lata wstecz nic nie da — skrypt i tak przytnie zakres do tego,
  co API oddaje, i napisze o tym w ostrzeżeniach. Wpłaty i wypłaty bywają dostępne
  dłużej; jeśli i one się urwą, dopisz je ręcznie (punkt 8).
- **Wynik zrealizowany spot** wymaga pełnej historii zakupów. Sprzedaż monety
  kupionej przed początkiem zakresu nie ma znanej ceny nabycia — skrypt **nie
  dopisuje wtedy fikcyjnego zysku**, tylko wymienia takie pary w ostrzeżeniu.
- **Transakcje starsze niż 90 dni** są odtwarzane z rejestru podatkowego: dwa
  wpisy o wspólnym `bizOrderId` (moneta wydana i otrzymana) dają parę, kierunek,
  wolumen i cenę. To wystarcza do policzenia wyniku, ale jest odtworzeniem, nie
  zapisem transakcji — prowizja może być liczona z dokładnością do zaokrągleń.
  Dla ostatnich 90 dni używane są dokładne dane z `trade/fills`.
- **Futures** liczone są z księgi rachunku (`amount + fee` dla wpisów innych niż
  transfery), co obejmuje P&L pozycji, funding fee i prowizje. Historia
  zamkniętych pozycji służy jako kontrola krzyżowa (API oddaje ~3 miesiące).
- **Earn**: część produktów nie raportuje odsetek w API. Odsetki dopisywane
  bezpośrednio na Spot są jednak widoczne w księdze spot (grupa `financial`).
- **Kursy historyczne** to zamknięcia świec dziennych. Dla operacji w środku dnia
  to przybliżenie (kolumna `zrodlo_kursu` w CSV pokazuje, skąd wziął się kurs).
- **Konta subkont i copy-trading** nie są ujęte — skrypt widzi to samo, co klucz API.

---

## 10. Testy

Wszystko jest pokryte testami offline (bez kontaktu z API Bitget):

```bash
python3 -m unittest discover -s tests -v
```

Testy liczenia (`test_analiza.py`): wycena wpłat kursem historycznym, wyłączenie
transferów wewnętrznych z wyniku, P&L spot metodą średniej ceny nabycia, sprzedaż
bez znanej ceny nabycia, rozbicie futures na P&L/funding/prowizje, podpis
HMAC-SHA256, podział zakresu na okna czasowe, eksport CSV.

Testy panelu i kluczy (`test_panel.py`): szyfrowanie i odszyfrowanie kluczy, brak
sekretów w zapisanym pliku, prawa `0600`, wykrywanie uprawnień zapisu i wypłat,
odmowa bez tokenu, odrzucanie obcego `Origin`, blokada wyjścia poza katalog
raportu, nagłówki bezpieczeństwa, nasłuch tylko na loopbacku oraz pełny przebieg
analizy uruchomionej z panelu.

---

## 11. Struktura projektu

```
panel.py                     # panel web (zalecane wejście)
analizuj.py                  # CLI
bitget_analyzer/
├── config.py                # .env, argumenty, zakres dat
├── secrets_store.py         # szyfrowanie i przechowywanie kluczy API
├── account.py               # weryfikacja uprawnień klucza
├── client.py                # HMAC-SHA256, retry, rate limit, paginacja
├── prices.py                # kursy bieżące i historyczne + cache
├── model.py                 # struktury danych
├── wallet.py                # wpłaty, wypłaty, transfery
├── spot.py                  # księga spot + transakcje
├── futures.py               # księga futures + zamknięte pozycje
├── earn.py                  # produkty Earn
├── valuation.py             # aktualna wycena portfela
├── pipeline.py              # wspólna ścieżka pobierania (CLI + panel)
├── analysis.py              # P&L, bilans miesięczny
├── report.py                # konsola + CSV/JSON
└── webapp/
    ├── server.py            # serwer HTTP (stdlib), token, nagłówki
    ├── service.py           # klucze, test połączenia, analiza w tle
    └── index.html           # interfejs panelu
tests/
├── test_analiza.py          # testy liczenia
└── test_panel.py            # testy kluczy i panelu
```
