# Mój Portfel

Lokalna aplikacja do śledzenia portfela inwestycyjnego – **bez logowania i bez chmury**, działa w przeglądarce, ale tylko na Twoim komputerze.
Wczytujesz eksporty od brokera, a aplikacja liczy:

* **wartość portfela** = ilość × aktualna cena z Yahoo Finance × kurs waluty (wszystko w PLN) + gotówka oprocentowana jako lokata,
* **zysk** z inwestycji (niezrealizowany i zrealizowany) oraz dywidendy, odsetki i inne dochody, razem zysk całkowity,
* **wskaźniki**: zwrot TWR, XIRR, od początku roku, rok do roku, obsunięcia, zmienność, stopa dywidendy,
* **wykresy** z przybliżaniem, alokację, dochody miesięczne – i **sprawdza poprawność liczb** (niezależne przeliczenie i tożsamości księgowe).

Obsługiwani brokerzy: **XTB** (pliki .xlsx) i **Trade Republic** (plik .csv „Eksport transakcji”). Kolejne eksporty można wczytywać w każdej
chwili – operacje już wczytane są pomijane, dochodzą tylko nowe.

## Instalacja (Windows 10/11)

Pobierz `MojPortfel-Setup-X.Y.Z.exe` z **Releases** tego repozytorium na GitHubie i uruchom. Szczegóły, aktualizacje i odinstalowanie:
[docs/INSTALATOR.md](docs/INSTALATOR.md).

## Prywatność

* Dane (operacje, salda, baza) zostają w `%LOCALAPPDATA%\AplikacjaInwestycyjna` na Twoim komputerze. Do internetu trafiają tylko symbole
  instrumentów (po ceny z Yahoo Finance) i zapytania o kursy NBP oraz o nową wersję aplikacji (GitHub).
* Z eksportów nie są zapisywane dane osobowe (IBAN, imiona i nazwiska z opisów przelewów) – tylko skrót i maska.
* Aplikacja nasłuchuje wyłącznie na 127.0.0.1; wbudowana baza PostgreSQL ma losowe hasła i przyjmuje połączenia tylko z tego komputera.

## Uruchomienie ze źródeł (dla programistów)

```
python -m pip install -r requirements.txt
python uruchom.py                      # albo: python -m invapp [--port 8000] [--data-dir D:\proba] [--no-browser] [--stop]
```
Wersja ze źródeł używa **osobnych danych testowych** (`%LOCALAPPDATA%\AplikacjaInwestycyjna-dev`), więc nie miesza się z zainstalowaną
aplikacją – opis pracy nad zmianami i wydawania wersji: [docs/ROZWOJ.md](docs/ROZWOJ.md).

Python 3.12; PostgreSQL nie jest potrzebny (wbudowany, z pakietu `pgserver`). Katalog danych: `baza\`, `kopie\` (kopie zapasowe przed każdą
zmianą schematu), `logi\`, `hasla_bazy.json` (losowe hasła ról bazy).

Testy (dane syntetyczne):
```
python -m unittest tests.test_metrics tests.test_omi_parser tests.test_pricing tests.test_history tests.test_deposits tests.test_ponowny_import tests.test_aktualizacje tests.test_tryby tests.test_engine_and_brokers tests.test_app_skeleton
```

Dokumentacja: [docs/ARCHITEKTURA.md](docs/ARCHITEKTURA.md), [docs/DODAWANIE_BROKERA.md](docs/DODAWANIE_BROKERA.md) (nowy broker, np. Revolut),
[docs/INSTALATOR.md](docs/INSTALATOR.md) (instalator, wydania, aktualizacje), [docs/ROZWOJ.md](docs/ROZWOJ.md) (praca nad zmianami).

## Zastrzeżenia

Aplikacja do prywatnego użytku, bez gwarancji. Nie jest doradztwem inwestycyjnym ani podatkowym (koszt w aplikacji = gotówka z księgi,
nie koszt podatkowy). Ceny pochodzą z nieoficjalnego API Yahoo Finance i mogą być opóźnione.
