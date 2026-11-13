# Architektura

```
uruchom.py                  start ze źródeł (to samo co: python -m invapp)
invapp/
  __main__.py               start: sprawdzenie bibliotek, wykrycie działającej kopii, wolny port, serwer WWW, przeglądarka, --stop
  config.py                 katalog danych użytkownika (%LOCALAPPDATA%\AplikacjaInwestycyjna), host 127.0.0.1, port
  updates.py                aktualizacje z wydań (Releases) repozytorium na GitHubie (sprawdzenie, pobranie, suma SHA-256, uruchomienie instalatora)
  db/
    engine.py               wbudowany PostgreSQL 16 (binarki z pakietu pgserver, serwerem zarządza pg_ctl): inicjalizacja, role, hasła, migracje
    migrate.py              migracje SQL (sumy SHA-256, numeracja ciągła)
    migrations/0001…0020    schemat: księga, partie, zakresy, widoki wyceny i wyniku, lokaty, indeksy, ustawienia, gotówka, PPK (baza)
    backup.py               kopie zapasowe (pg_dump z wbudowanego serwera)
  core/
    common.py               konwersje liczb, skróty wierszy, maskowanie danych osobowych
    derive.py               warstwa pochodna: partie i zamknięcia (XTB wg Position ID, FIFO dla brokerów bez numerów pozycji)
    ledger_import.py        WSPÓLNY SILNIK IMPORTU: partia, duplikaty/konflikty, surowe wiersze, kontrola G3; konta, ISIN → notowanie
    deposits.py             gotówka oprocentowana jako lokata (model odsetek, stopa z wypłat, kontrola)
    cash.py                 zakładka Gotówka i ustawienia (app_settings): konta bankowe, wolne środki, źródło cen i kursów
    removal.py              usunięcie pliku / portfela / wszystkiego (stan jak bez tych plików, razem z decyzjami o usuniętych danych)
  brokers/
    base.py                 WSPÓLNY INTERFEJS brokera (rozpoznanie pliku, walidacja, import, partie, kroki cenowe, pytania do użytkownika)
    __init__.py             LISTA BROKERÓW (BROKERS) – jedyne miejsce, które trzeba dopisać przy nowym brokerze
    xtb/                    parser .xlsx, silnik importu, konta z pliku, specyfikacja OMI, reguły walut (.PL), pytania o splity
    trade_republic/         parser .csv, silnik importu, FIFO, ISIN → symbol dostawcy ze sprawdzeniem ceny, pytanie o lokatę
  pricing/                  dostawcy (Yahoo, NBP), notowania na żywo (service), historia (history), wskaźniki i miary zwrotu (metrics),
                            indeksy porównawcze (benchmarks), profile/sektory (profiles), kontrola liczb (audit) + przyczyny i rozwiązania (diagnose)
  web/
    app.py                  FastAPI: strona, API importu, cen, historii, wskaźników, kontroli, pytań, aktualizacji, zamykania
    importing.py            import z interfejsu (wybór brokera po pliku, po kolei) i zadanie cen w tle
    dashboard.py            dane strony; zakładki = lista brokerów + WSZYSTKIE; widoki Portfel / Ekspozycja / Gotówka / Symulacje
    export_xlsx.py          eksport do Excela
    templates/, static/     strona (HTML) + wygląd i wykresy (CSS/JS bez zewnętrznych bibliotek, działa offline)
  tools/                    linia poleceń: nowy_broker (szkielet brokera), kopiuj_dane (dane do wersji testowej), potwierdzenie splitu
instalator/                 budowanie instalatora Windows (Python embeddable + Inno Setup), ikona
tests/                      testy publiczne (dane syntetyczne; wbudowany PostgreSQL tymczasowy), kontrakt brokerów, prywatność
docs/                       ZASADY (reguły i listy kontrolne), ARCHITEKTURA, DODAWANIE_BROKERA, DODAWANIE_MODULU, PPK, ROZWOJ, INSTALATOR
CLAUDE.md                   skrót zasad i poleceń dla pracy z kodem (także z asystentem AI)
```

## Zasady, których pilnuje kod

* **Wartość portfela = suma (ilość × aktualna cena z API × kurs)** + **lokaty** (gotówka oprocentowana, sekcja niżej), wszystko w PLN;
  wolne środki bez oprocentowania nie są częścią wartości ani widoków.
* Kurs walut do wyceny = kurs rynkowy z Yahoo (zapasowo NBP tabela A), bez potrącania prowizji przewalutowania brokera.
* Zysk z inwestycji (niezrealizowany + zrealizowany) osobno od dywidend, odsetek, nagród i opłat; razem = zysk całkowity.
  Koszt = gotówka z księgi (z opłatami), nie koszt podatkowy.
* Import atomowy i idempotentny: ponowny import tego samego albo nowszego eksportu dodaje tylko nowe operacje (klucze deduplikacji
  z identyfikatorów brokera), kolejność plików bez znaczenia; nieznane typy operacji nie trafiają do księgi (UNMAPPED).
* Usunięcie wczytanego pliku (`core/removal.py`) = stan jak gdyby pliku nigdy nie wczytano: usuwane jest tylko to, co pochodzi wyłącznie
  z tego pliku, powiązania liczone z całej księgi (przelewy, podatek → dywidenda, splity wykryte z danych) są liczone od nowa, konto bez
  danych znika, decyzje użytkownika zostają. Lokalny test sprawdza to dla każdego prawdziwego pliku.
* Czysta instalacja nie zawiera żadnych danych: instalator ma tylko program, baza powstaje pusta przy pierwszym uruchomieniu.
* Waluta i giełda instrumentu nie wynikają z sufiksu symbolu: tylko specyfikacja brokera, jawna decyzja użytkownika (pytanie w interfejsie)
  albo źródło zewnętrzne ze sprawdzeniem ceny – zawsze ze źródłem, datą i statusem.
* Dane osobowe (IBAN, imię i nazwisko z opisów przelewów) nie są zapisywane – tylko skrót i maska.
* Strona i odczyt używają roli `investing_app` (tylko odczyt), import – roli `investing_owner`; zapisy przez API wymagają nagłówka ochronnego
  i adresu lokalnego.

## Lokaty: gotówka oprocentowana

* **Model** (`invapp/core/deposits.py`): odsetki dzienne = saldo na koniec dnia × stopa / 365; odsetki za miesiąc wypłacane 1. dnia następnego
  miesiąca, od wypłaty podatek 19% (tak liczy Trade Republic – model odtwarza wypłaty z eksportu co do grosza, co sprawdza kontrola liczb).
* **Wartość lokaty** = saldo gotówki z księgi (stan na dzień eksportu) – **tylko kwoty zaksięgowane i wypłacone**. Odsetki naliczone od ostatniej
  wypłaty są pokazywane informacyjnie („naliczone, jeszcze niewypłacone – nie wliczone”); nie wchodzą do wartości, zysku ani wykresu.
* **Decyzja per konto** (`cash_deposit_terms`, migracja 0015): aplikacja pyta („Gotówka w Trade Republic jako lokata”), stopa wyznaczona z ostatniej
  wypłaty odsetek; zmiana stopy w kolejnych eksportach = pytanie o aktualizację; odmowa też jest zapisywana.
* **Wykres i wskaźniki**: dla konta z lokatą wpłata = przelew z banku, wypłata = przelew do banku, zakup/sprzedaż to przesunięcie wewnątrz aktywów,
  a odsetki są zwrotem. **Nagrody brokera** (np. darmowa akcja na start w Trade Republic) to **czysty zysk, nie wpłata**: na koncie z lokatą
  nagroda nie jest przepływem, a na koncie bez lokaty zakup opłacony nagrodą nie jest liczony jako wpłata własnych pieniędzy.
* **Kontrole**: saldo z widoku SQL = saldo w modelu; model odtwarza wypłaty; papiery + lokata − wpłaty netto z banku = zysk z inwestycji + dochody
  (tożsamość konta); WSZYSTKIE = suma brokerów.

## Wersja 0.3.0: ustawienia, gotówka, porównania, diagnoza

* **Ustawienia** (`app_settings`, migracja 0017; `core/cash.py`). Domyślnie wszystko jak w 0.2 – żadna liczba się nie zmienia, dopóki
  użytkownik czegoś nie włączy:
  * `price_source`: `YAHOO` (cena rynkowa na żywo) albo `XTB_EXPORT` – pozycje XTB wyceniane **dokładnie** wartością z ostatniego eksportu XTB
    (ceny i kursy XTB z chwili eksportu), o ile ilość w raporcie = ilość w księdze; inaczej cena z Yahoo. XTB nie ma publicznego API cen,
    dlatego „ceny XTB” pochodzą z eksportu. Różnica wobec Yahoo to zwykle 0,1–0,5% (XTB wycenia po bid i swoim kursie walut).
  * `fx_source`: `YAHOO` (kurs na żywo, zapas NBP) albo `NBP` (średni kurs tabeli A, zapas na żywo).
  * Widok `v_live_position_values` (migracja 0018) czyta ustawienia; kontrola liczb (`pricing/audit.py`) liczy niezależnie według tych samych.
    Zmienia się tylko bieżąca wycena; historia na wykresie zawsze z cen zamknięcia Yahoo.
  * `include_cash`, `include_xtb_free`: gotówka z kont bankowych (wpisywana ręcznie, tylko zakładka WSZYSTKIE) i wolne środki XTB (saldo
    gotówki z księgi) **doliczane tylko do bieżącej wartości portfela** (kafelek, alokacja). Zysk, wpłaty, wykresy i wskaźniki bez zmian
    (`assets_value` = inwestycje; `total_value` = inwestycje + włączona gotówka).
* **Zwrot %** (`pricing/metrics.return_series`): TWR i TWR z dywidendami (indeksy, przeliczane od początku widocznego okresu), zysk / wpłaty
  netto, zysk / suma wpłat, XIRR od początku do dnia (co tydzień), CAGR. Ostatni punkt każdej miary = wartość z kafelków (test).
* **Indeksy** (`pricing/benchmarks.py`, migracja 0016): Nasdaq-100 (^NDX), S&P 500 (^GSPC), MSCI World (^990100-USD-STRD) – indeksy cenowe;
  WIG20 – ETF Beta WIG20TR (Yahoo nie podaje historii indeksu WIG20). Waluta indeksu z konfiguracji, sprawdzana z Yahoo; w PLN kursem dnia.
* **Ekspozycja** (`pricing/profiles.py`, migracja 0019): typ, sektor i giełda wg wyszukiwarki Yahoo (tylko symbol), odświeżane co 30 dni.
* **Diagnoza kontroli liczb** (`pricing/diagnose.py`): przy każdej uwadze prawdopodobna przyczyna i rozwiązania z istniejących mechanizmów –
  odpowiedź na pytanie brokera (np. split „Zdecyduj za mnie”, waluta .PL), ponowne pobranie cen, przeliczenie partii od nowa z eksportów
  (`POST /api/przelicz`), wczytanie brakującego pliku. Żadna kwota nie jest korygowana ręcznie.
* **Usuwanie** (`core/removal.py`): plik, cały portfel (konto) albo wszystko – zawsze „jak gdyby nie wczytano”. Razem z danymi znikają decyzje,
  które dotyczyły już tylko usuniętych danych: splity instrumentów bez żadnych danych (przyjęte i odrzucone), reguły walut brokera bez żadnego
  pliku (np. .PL = PLN – waluta instrumentów ustalona tą regułą wraca do „nieznanej”), lokata usuniętego konta. Po ponownym wczytaniu aplikacja
  pyta o nie od nowa. Zostają: konta bankowe z „Gotówki”, ustawienia, dane referencyjne (ceny, kursy, specyfikacje).
* **Split „Zdecyduj za mnie”** (`brokers/xtb/broker._auto_split`): dzień podziału z historii splitów Yahoo, jeśli współczynnik się zgadza
  i dzień leży między ostatnim zakupem a pierwszą sprzedażą po podziale; inaczej granica z transakcji. Zawsze oznaczony jako niesprawdzony
  w źródle oficjalnym (`official_source_status = NOT_VERIFIED`).

## Wydajność

Pomiar 2026-11-13 (prawdziwe dane, ~1200 operacji, 3 lata historii): strony i API 40–250 ms, eksport Excela 0,5 s, zadanie cen w tle ~5 s.

* **Import**: istniejące operacje sprawdzane jednym zapytaniem na plik (prefetch kluczy), nowe operacje XTB zapisywane wsadowo,
  „widziano w tym pliku” zbiorczo, konta i waluty w pamięci podręcznej importu, arkusz Excela czytany raz. Wynik: ok. 5× mniej zapytań;
  ponowny import pliku z 1118 operacjami ~2× szybszy, pierwszy ~1,3× (pozostały czas: odczyt Excela i łączenie operacji).
  Specyfikacja instrumentów XTB (PDF ~35 s) jest pobierana w tle, nie w trakcie importu. Importy wgrywane jednocześnie – po kolei.
* **Notowania i kursy** są dopisywane co minutę otwartej aplikacji i nigdy kasowane (audyt). Wycena czyta „najnowsze” z tabel wskazań
  `market_quote_latest` / `fx_quote_latest` (utrzymuje je baza wyzwalaczem, migracja 0020) – czas nie rośnie z historią notowań.
* **Indeksy** na kolumnach wyszukiwania (instrument, konto, plik, pozycja, wiersz surowy) – migracja 0020; nowa kolumna wyszukiwania = indeks w tej samej migracji.
* Ceny: zapytania do Yahoo równolegle (6 naraz), zweryfikowane symbole nie są sprawdzane ponownie przez 7 dni, historia cen dopełniana
  przyrostowo; najpierw notowania (wartości po kilku sekundach), potem specyfikacje i historia.
* Baza: serwerem zarządza aplikacja (`db/engine.py`), dziennik serwera poza katalogiem danych – po nieczystym zamknięciu start nie zawiesza się;
  zamknięcie serwera czeka najwyżej 10 s na otwarte połączenia przeglądarki.
* Strona: dane wczytywane równolegle, najpierw zapisane ceny, potem świeże; po zakończeniu kroku w tle strona odświeża dane bez przeładowania.

## Baza danych

PostgreSQL 16 **wbudowany** w program (binarki z pakietu `pgserver`, ok. 40 MB). Serwer startuje i zatrzymuje się z programem, słucha tylko
na 127.0.0.1 (wolny port), połączenia wymagają haseł generowanych przy pierwszym uruchomieniu (scram-sha-256).
Aktualizacja programu z nowymi migracjami: przy starcie automatyczna kopia zapasowa, potem migracje. Zmieniony plik zastosowanej migracji jest
błędem; wyjątek to poprawki wyłącznie komentarzy wpisane w `COMMENT_ONLY_EDITS` (`db/migrate.py`).

## Instalator i aktualizacje

Opis: [INSTALATOR.md](INSTALATOR.md).
