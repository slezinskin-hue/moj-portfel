# Dodawanie modułu (nowa zakładka / nowy rodzaj aktywów)

Moduł = funkcja aplikacji, która nie jest brokerem: dane wpisywane ręcznie albo z innego źródła niż eksport brokera.
Wzorzec: **Gotówka** (wersja 0.3.0) – migracja `0017`, logika `invapp/core/cash.py`, API w `invapp/web/app.py`, zakładka w interfejsie.
Kolejny planowany moduł: **PPK** (`docs/PPK.md`).

## Miejsca do zmiany (w tej kolejności)

| # | plik | co dodać |
|---|---|---|
| 1 | `invapp/db/migrations/00NN_<modul>.sql` | tabele (historia zamiast nadpisywania, np. nowe saldo = nowy wiersz), ograniczenia `CHECK`, indeksy po kolumnach wyszukiwania, `GRANT SELECT … TO investing_app`; nagłówek z decyzją i **`WPLYW NA WYNIKI`** |
| 2 | `invapp/core/<modul>.py` | logika bez interfejsu: walidacja wejścia (błąd = klasa dziedzicząca po `ValueError` z komunikatem po polsku), odczyt, zapis, wyliczenia; zaokrąglenia `ROUND_HALF_UP` do grosza |
| 3 | `invapp/web/dashboard.py` | funkcja danych dla strony (`<modul>_data()`), połączenie tylko do odczytu (`_connect(readonly=True)`); stara baza bez tabel → wartości domyślne (`psycopg2.Error` → rollback) |
| 4 | `invapp/web/app.py` | `GET /api/<modul>` (odczyt) i `POST …` przez **`owner_write(request, lambda cur, body: …)`** – nagłówek ochronny, rola właściciela, jedna transakcja, `ValueError` → 422 |
| 5 | wartość portfela (jeśli moduł ją zmienia) | wzorzec `dashboard.add_cash`: `assets_value` (inwestycje – z nich liczą się wykres i wskaźniki) zostaje, dochodzi `total_value`; włączane **ustawieniem** (`core/cash.py: DEFAULTS/ALLOWED`, domyślnie wyłączone) |
| 6 | `invapp/web/templates/dashboard.html` | link zakładki `<a class="vtab" data-v="<modul>" href="#/<modul>">…</a>` i blok `<div class="view" data-view="<modul>" hidden>…</div>` |
| 7 | `invapp/web/static/app.js` | `<modul>` w liście `VIEWS`, wywołanie wczytania w `showView()`, funkcja rysowania (zapis przez `post(url, body)`) |
| 8 | kontrola liczb `invapp/pricing/audit.py` | niezależne przeliczenie nowej liczby (inna droga niż ta na stronie) + przyczyna/rozwiązanie w `pricing/diagnose.py`, jeśli może się nie zgadzać |
| 9 | eksport `invapp/web/export_xlsx.py` | arkusz z danymi modułu |
| 10 | usuwanie (`core/removal.py`) | świadoma decyzja: czy „Usuń wszystko” kasuje dane modułu (Gotówka: nie – dane wpisane ręcznie, nie z plików) |
| 11 | testy | `tests/test_<modul>.py` (logika, walidacja, uprawnienia roli aplikacji, `POST` bez nagłówka = 403), mutacje reguł w `tests/mutation_check*.py`, dopisanie do listy testów w `.github/workflows/testy.yml` i `docs/ROZWOJ.md` |
| 12 | dokumentacja | `docs/ARCHITEKTURA.md` (mapa i zasady), opis w `README.md`, sekcja „Jak liczone są liczby” w stronie |

## Zasady modułów

* Dane wpisywane ręcznie mają **datę stanu** i **historię** (nowy wpis zamiast nadpisania; „usuń” = archiwizacja).
* Moduł, który nie jest inwestycją (gotówka) albo ma wynik tylko orientacyjny (PPK), **nie zmienia** zysku, wpłat, wykresów i wskaźników
  portfela inwestycyjnego – chyba że to świadoma, opisana decyzja.
* Waluty obce: ten sam kurs co dla papierów (`v_fx_for_valuation`, ustawienie `fx_source`); brak kursu = brak wartości (jawnie), nie zgadywanie.
* Kod modułu nie zna brokerów po nazwie (jeśli potrzebuje ich danych – przez `invapp/brokers` i atrybuty interfejsu, np. `free_funds`).
