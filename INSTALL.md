# Instalacja na serwerze (np. Oracle Cloud)

Instrukcja zakłada, że logujesz się na serwer po SSH i chcesz korzystać z panelu
przez przeglądarkę na swoim komputerze. **Nie otwieramy żadnego portu w internecie** —
panel zostaje na loopbacku serwera, a ruch idzie tunelem SSH.

---

## 1. Na serwerze: przygotowanie systemu

Zaloguj się jak zwykle:

```bash
ssh mice66
```

Sprawdź, jaki to system i jaki Python:

```bash
cat /etc/os-release | head -2
python3 --version
```

Potrzebny jest **Python 3.9 lub nowszy**.

Pakiety trzeba doinstalować **zawsze**, nawet jeśli `python3 --version` pokazuje
dobrą wersję: Ubuntu nie ma domyślnie `venv` ani `pip`, a bez nich krok 3 padnie
komunikatem *„ensurepip is not available"*.

**Ubuntu / Debian (apt):**
```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
```

Jeśli `python3-venv` nie znajdzie się w repozytorium, podaj wersję wprost —
dla Ubuntu 22.04 (Python 3.10) będzie to:

```bash
sudo apt install -y python3.10-venv
```

**Oracle Linux / RHEL / Rocky (dnf):**
```bash
sudo dnf install -y git python3.11 python3.11-pip
# dalej używaj wtedy 'python3.11' zamiast 'python3'
```

---

## 2. Pobranie projektu

```bash
cd ~
git clone https://github.com/mice6/Analizator-bitget.git
cd Analizator-bitget
```

Repozytorium jest publiczne, więc nie trzeba żadnego tokenu ani klucza SSH.

Aktualizacja w przyszłości:

```bash
cd ~/Analizator-bitget && git pull
```

---

## 3. Środowisko wirtualne i zależności

Wykonuj te komendy **pojedynczo** i sprawdzaj wynik każdej — gdy pierwsza padnie,
kolejne nie mają na czym pracować i posypią się kaskadą:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip        # WAŻNE: stary pip próbuje kompilować cryptography ze źródeł
pip install -r requirements.txt
```

Po `source .venv/bin/activate` w wierszu poleceń powinno pojawić się `(.venv)`.
Jeśli go nie ma, środowisko nie powstało — wróć do punktu 1.

Jeśli `pip install` mimo to próbuje kompilować `cryptography` (długo mieli, sypie
błędami o `rust` albo `openssl`), doinstaluj narzędzia budowania:

```bash
# Oracle Linux / RHEL
sudo dnf install -y gcc openssl-devel libffi-devel python3-devel cargo
# Ubuntu / Debian
sudo apt install -y build-essential libssl-dev libffi-dev python3-dev cargo
```

Sprawdzenie, że wszystko działa (testy nie łączą się z Bitgetem):

```bash
python3 -m unittest discover -s tests
```

Powinno wypisać `OK` i liczbę testów.

---

## 4. Klucz API na Bitget — z właściwym IP

Zapytania do Bitget wychodzą **z serwera**, więc whitelista IP w kluczu musi
zawierać publiczny adres serwera, a nie Twojego komputera. Sprawdź go:

```bash
curl -s ifconfig.me; echo
```

Ten adres wpisz w Bitget przy tworzeniu klucza:

> Bitget → API Management → Create API Key → System-generated API key
> - uprawnienia: **tylko Read-only** (bez Trade, bez Withdraw, bez Transfer)
> - IP whitelist: adres z komendy powyżej
> - zapisz passphrase — Bitget pokaże go tylko raz

---

## 5. Uruchomienie panelu

Na serwerze, w katalogu projektu:

```bash
source .venv/bin/activate
python3 panel.py
```

Zobaczysz adres z tokenem, np.:

```
http://127.0.0.1:8770/?t=LMyY1-rcywHNTs9YKBsIwciKGLyItdU_
```

**Skopiuj cały ten adres.**

---

## 6. Tunel SSH i otwarcie panelu

W **drugim oknie terminala na swoim komputerze** (nie na serwerze):

```bash
ssh -L 8770:127.0.0.1:8770 mice66
```

To okno musi pozostać otwarte — trzyma tunel. Teraz wklej skopiowany adres do
przeglądarki na swoim komputerze. Panel się otworzy.

Port 8770 zajęty u Ciebie lokalnie? Użyj innego po lewej stronie:
`ssh -L 9000:127.0.0.1:8770 mice66`, a w przeglądarce zmień `8770` na `9000`
(token zostaw bez zmian).

W Oracle Cloud **nie trzeba** nic zmieniać w Security List ani w `firewalld` —
tunel SSH idzie po porcie 22, który i tak masz otwarty.

---

## 7. W panelu

1. **Klucze API** — wklej key / secret / passphrase, kliknij „Zapisz zaszyfrowane".
   Zapisują się w `~/.config/analizator-bitget/` na serwerze, zaszyfrowane AES-256-GCM.
   Robisz to raz — przy kolejnych uruchomieniach panel już je ma.
2. **Sprawdź połączenie i uprawnienia** — potwierdzi, że klucz działa i że jest
   tylko do odczytu. Jeśli zobaczysz ostrzeżenie o uprawnieniu do wypłat, usuń
   klucz w Bitget i wygeneruj nowy.
3. **Zakres analizy** — ustaw daty i opcjonalnie kurs PLN, potem „Analizuj".
   Sensowne „Od" to data pierwszej wpłaty, ale **nie wcześniej niż 2 lata wstecz** —
   dalej Bitget po prostu nie udostępnia historii transakcyjnej.
4. **Wynik** — po zakończeniu zobaczysz realny zysk/stratę, rozbicie i tabele.
   Pliki CSV pobierzesz linkami na dole strony.

Zatrzymanie panelu: `Ctrl+C` w oknie, gdzie działa.

---

## 8. Żeby panel przeżył rozłączenie SSH

Analiza kilku lat historii potrafi trwać kilka minut. Jeśli boisz się, że SSH
padnie w trakcie, uruchom panel w `tmux`:

```bash
sudo dnf install -y tmux     # albo: sudo apt install -y tmux

tmux new -s panel
source ~/Analizator-bitget/.venv/bin/activate
cd ~/Analizator-bitget && python3 panel.py
# odłączenie od sesji: Ctrl+B, potem D
```

Powrót do działającego panelu: `tmux attach -t panel`.
Tunel SSH z punktu 6 możesz w tym czasie otwierać i zamykać dowolnie.

---

## 9. Bez panelu, prosto z terminala

Jeśli wolisz nie stawiać tunelu, a klucze są już zapisane (punkt 7.1), wystarczy:

```bash
source .venv/bin/activate
python3 analizuj.py --od 2023-01-01 --fx-rate 4.05 --fx-label PLN
```

Raport wypisze się w konsoli, a CSV wylądują w `~/Analizator-bitget/raport/`.
Ściągnięcie ich na swój komputer:

```bash
scp -r mice66:~/Analizator-bitget/raport ./raport-bitget
```

---

## Najczęstsze problemy

| Objaw | Przyczyna i rozwiązanie |
|---|---|
| `python3: command not found` albo wersja < 3.9 | Zainstaluj nowszego Pythona (punkt 1) i używaj np. `python3.11` |
| `ensurepip is not available` przy tworzeniu venv | Brak pakietu venv: `sudo apt install -y python3-venv` (albo wprost `python3.10-venv`), potem powtórz krok 3 |
| `Command 'pip' not found` | Venv nie powstał albo nie jest aktywny — sprawdź `(.venv)` w wierszu poleceń |
| Przeglądarka: „nie można połączyć" | Tunel SSH nie działa — sprawdź okno z `ssh -L`, czy nadal jest otwarte |
| Panel odpowiada `401` | Zły albo brakujący token — użyj pełnego adresu z `?t=...` wypisanego przy starcie |
| `[40099]` albo błąd o IP | Publiczny adres serwera nie jest na whiteliście klucza (punkt 4) |
| `[40012]` / `[40037]` | Zły klucz API albo klucz usunięty w Bitget |
| Błąd o `_cffi_backend` przy starcie | `pip install --upgrade pip && pip install --force-reinstall cryptography` |
| `Port 8770 jest już zajęty` | Panel działa w innym oknie: `pkill -f "python3 panel.py"`, albo wystartuj na innym porcie i popraw tunel |
| `git pull` mówi „Already up to date", a poprawek nie ma | `git fetch origin && git reset --hard origin/claude/bitget-profitability-analyzer-xftfmy` |
| W pokryciu danych „limit historii API" | Normalne — Bitget nie oddaje danych sprzed ~2 lat. Brakujące wpłaty dopisz ręcznie (README, punkt 8) |
