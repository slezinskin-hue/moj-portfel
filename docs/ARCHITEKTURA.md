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
    migrations/0001…        schemat: księga, partie, zakresy, widoki wyceny i wyniku, lokaty
    backup.py               kopie zapasowe (pg_dump z wbudowanego serwera)
  core/
    common.py               konwersje liczb, skróty wierszy, maskowanie danych osobowych
    derive.py               warstwa pochodna: partie i zamknięcia (XTB wg Position ID, FIFO dla brokerów bez numerów pozycji)
    deposits.py             gotówka oprocentowana jako lokata (model odsetek, stopa z wypłat, kontrola)
    removal.py              usunięcie wczytanego pliku (stan jak bez tego pliku)
  brokers/
    base.py                 WSPÓLNY INTERFEJS brokera (rozpoznanie pliku, walidacja, import, partie, kroki cenowe, pytania do użytkownika)
    __init__.py             LISTA BROKERÓW (BROKERS) – jedyne miejsce, które trzeba dopisać przy nowym brokerze
    xtb/                    parser .xlsx, silnik importu, konta z pliku, specyfikacja OMI, reguły walut (.PL), pytania o splity
    trade_republic/         parser .csv, silnik importu, FIFO, ISIN → symbol dostawcy ze sprawdzeniem ceny, pytanie o lokatę
  pricing/                  dostawcy (Yahoo, NBP), notowania na żywo, historia, wskaźniki (metrics), kontrola liczb (audit)
  web/
    app.py                  FastAPI: strona, API importu, cen, historii, wskaźników, kontroli, pytań, aktualizacji, zamykania
    importing.py            import z interfejsu (wybór brokera po pliku, po kolei) i zadanie cen w tle
    dashboard.py            dane strony; zakładki = lista brokerów + WSZYSTKIE
    templates/, static/     strona (HTML) + wygląd i wykresy (CSS/JS bez zewnętrznych bibliotek, działa offline)
  tools/                    narzędzia z linii poleceń (np. potwierdzenie splitu)
instalator/                 budowanie instalatora Windows (Python embeddable + Inno Setup), ikona
tests/                      testy publiczne (dane syntetyczne; wbudowany PostgreSQL tymczasowy)
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

## Wydajność

* Specyfikacja instrumentów XTB (PDF ~35 s) jest pobierana w tle, nie w trakcie importu – import pliku XTB trwa 1–3 s.
* Importy wgrywane jednocześnie są wykonywane po kolei.
* Ceny: zapytania do Yahoo równolegle (6 naraz), zweryfikowane symbole nie są sprawdzane ponownie przez 7 dni, historia cen dopełniana
  przyrostowo. Kolejność: najpierw notowania (wartości widoczne po kilku sekundach), potem specyfikacje i historia.
* Baza: serwerem zarządza aplikacja (`db/engine.py`), dziennik serwera poza katalogiem danych – po nieczystym zamknięciu start nie zawiesza się.
* Strona: dane wczytywane równolegle, najpierw zapisane ceny, potem świeże; po zakończeniu kroku w tle strona odświeża dane bez przeładowania.

## Baza danych

PostgreSQL 16 **wbudowany** w program (binarki z pakietu `pgserver`, ok. 40 MB). Serwer startuje i zatrzymuje się z programem, słucha tylko
na 127.0.0.1 (wolny port), połączenia wymagają haseł generowanych przy pierwszym uruchomieniu (scram-sha-256).
Aktualizacja programu z nowymi migracjami: przy starcie automatyczna kopia zapasowa, potem migracje. Zmieniony plik zastosowanej migracji jest
błędem; wyjątek to poprawki wyłącznie komentarzy wpisane w `COMMENT_ONLY_EDITS` (`db/migrate.py`).

## Instalator i aktualizacje

Opis: [INSTALATOR.md](INSTALATOR.md).
