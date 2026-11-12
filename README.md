# Mój Portfel

Lokalna aplikacja do śledzenia portfela inwestycyjnego – **bez logowania i bez chmury**: działa we własnym oknie (silnik Edge WebView2), tylko na Twoim komputerze.
Wczytujesz eksporty od brokera, a aplikacja liczy:

* **wartość portfela** = ilość × aktualna cena z Yahoo Finance × kurs waluty (wszystko w PLN) + gotówka oprocentowana jako lokata,
* **zysk** z inwestycji (niezrealizowany i zrealizowany) oraz dywidendy, odsetki i inne dochody, razem zysk całkowity,
* **wskaźniki**: zwrot TWR, XIRR, od początku roku, rok do roku, obsunięcia, zmienność, stopa dywidendy,
* **wykresy** z przybliżaniem, alokację, dochody miesięczne; wykres „Zwrot %” z kilkoma miarami naraz (TWR, TWR z dywidendami, zysk od wpłat,
  XIRR, CAGR) i **porównaniem z indeksami** (Nasdaq-100, S&P 500, MSCI World, WIG20 – w PLN),
* zakładki **Ekspozycja** (typ, sektor, rynek notowania, waluta, koncentracja), **Gotówka** (konta bankowe wpisywane ręcznie, wolne środki XTB –
  do wliczenia w wartość portfela na życzenie) i **Symulacje** (projekcja z celem, „co gdybym inwestował w indeks”),
* **eksport** do Excela i PDF, wybór **źródła cen** (Yahoo na żywo albo wartości z ostatniego eksportu XTB) i **kursów** (rynkowe albo NBP),
* i **sprawdza poprawność liczb** (niezależne przeliczenie i tożsamości księgowe); przy każdej uwadze podaje prawdopodobną przyczynę i przycisk
  rozwiązania (np. „Zdecyduj za mnie” przy podziale akcji niewidocznym w eksporcie) – bez ręcznego dopisywania kwot.

Obsługiwani brokerzy: **XTB** (pliki .xlsx) i **Trade Republic** (plik .csv „Eksport transakcji”). Kolejne eksporty można wczytywać w każdej
chwili – operacje już wczytane są pomijane, dochodzą tylko nowe.

## Instalacja (Windows 10/11)

Pobierz `MojPortfel-Setup-X.Y.Z.exe` z **Releases** tego repozytorium na GitHubie i uruchom. Szczegóły, aktualizacje i odinstalowanie:
[docs/INSTALATOR.md](docs/INSTALATOR.md).

## Prywatność i bezpieczeństwo

* Dane (operacje, salda, baza) zostają w `%LOCALAPPDATA%\AplikacjaInwestycyjna` na Twoim komputerze. Do internetu trafiają tylko symbole
  instrumentów (ceny, sektory i indeksy z Yahoo Finance), zapytania o kursy NBP, specyfikacja instrumentów XTB i pytanie o nową wersję aplikacji (GitHub).
  Aplikacja nie ma kont, logowania, telemetrii ani wysyłania raportów.
* Z eksportów nie są zapisywane dane osobowe (IBAN, imiona i nazwiska z opisów przelewów) – tylko skrót i maska. Konta bankowe w zakładce
  „Gotówka” mają tylko nazwę i saldo (bez numerów).
* Aplikacja nasłuchuje wyłącznie na 127.0.0.1 i odrzuca żądania z obcymi nagłówkami Host/Origin (strona otwarta w przeglądarce nie odczyta
  danych portfela); strona nie ładuje niczego z internetu i nie może być osadzona w cudzej stronie. Wbudowana baza PostgreSQL ma losowe hasła
  i przyjmuje połączenia tylko z tego komputera.
* Eksport do Excela/PDF zapisuje plik tylko tam, gdzie wskażesz. Aktualizacje pobierane są wyłącznie z wydań tego repozytorium i sprawdzane sumą SHA-256.
* Odinstalowanie pyta, czy usunąć też dane (np. przed oddaniem komputera); domyślnie dane zostają.
* Repozytorium nie zawiera żadnych danych użytkownika – pilnuje tego test `tests/test_prywatnosc.py` przy każdym wysłaniu kodu.

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
python -m unittest tests.test_metrics tests.test_omi_parser tests.test_pricing tests.test_history tests.test_deposits tests.test_ponowny_import tests.test_aktualizacje tests.test_tryby tests.test_engine_and_brokers tests.test_app_skeleton tests.test_gotowka tests.test_diagnoza tests.test_porownanie tests.test_prywatnosc
```

Dokumentacja: [docs/ARCHITEKTURA.md](docs/ARCHITEKTURA.md), [docs/DODAWANIE_BROKERA.md](docs/DODAWANIE_BROKERA.md) (nowy broker, np. Revolut),
[docs/INSTALATOR.md](docs/INSTALATOR.md) (instalator, wydania, aktualizacje), [docs/ROZWOJ.md](docs/ROZWOJ.md) (praca nad zmianami).

## Zastrzeżenia

Aplikacja do prywatnego użytku, bez gwarancji. Nie jest doradztwem inwestycyjnym ani podatkowym (koszt w aplikacji = gotówka z księgi,
nie koszt podatkowy). Ceny pochodzą z nieoficjalnego API Yahoo Finance i mogą być opóźnione.
