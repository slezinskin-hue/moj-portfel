# Mój Portfel – instrukcja pracy z kodem

Lokalna aplikacja (Windows, własne okno WebView2 albo przeglądarka) do śledzenia portfela: wczytuje eksporty brokerów (XTB .xlsx,
Trade Republic .csv), wycenia pozycje cenami z Yahoo w PLN, liczy zysk, dywidendy, wskaźniki i **kontroluje liczby**. Interfejs po polsku.
Publiczne repozytorium (GitHub), instalator Inno Setup, aktualizacje z wydań (GitHub Releases).

## Najpierw przeczytaj

* `docs/ZASADY.md` – reguły nienaruszalne (liczby co do grosza, nie zgadujemy, dane surowe nienaruszalne, prywatność, wydajność, testy)
  i listy kontrolne przed commitem / wydaniem.
* `docs/ARCHITEKTURA.md` – mapa kodu, zasady liczenia, lokaty, ustawienia, wydajność.
* Dodawanie: **brokera** → `docs/DODAWANIE_BROKERA.md` (generator `python -m invapp.tools.nowy_broker`), **modułu / zakładki** →
  `docs/DODAWANIE_MODULU.md`, **PPK** → `docs/PPK.md`. Praca nad zmianami i wydania → `docs/ROZWOJ.md`, instalator → `docs/INSTALATOR.md`.

## Polecenia (Windows, w folderze repozytorium)

```
python uruchom.py                                   # wersja ze źródeł, dane testowe %LOCALAPPDATA%\AplikacjaInwestycyjna-dev
python -m invapp --no-browser --port 8770           # sam serwer (np. do pomiarów); --data-dir D:\proba = inne dane
python -m invapp.tools.kopiuj_dane                  # kopia prawdziwych danych do wersji testowej (obie aplikacje zamknięte)
python -m unittest tests.test_metrics tests.test_omi_parser tests.test_pricing tests.test_history tests.test_deposits tests.test_ponowny_import tests.test_aktualizacje tests.test_tryby tests.test_engine_and_brokers tests.test_app_skeleton tests.test_gotowka tests.test_diagnoza tests.test_porownanie tests.test_prywatnosc tests.test_kontrakt_brokerow tests.test_pozycje tests.test_waluta
```

Testy lokalne na prawdziwych eksportach (pliki w `export/`, testy w `.gitignore`) i mutacje: `docs/ROZWOJ.md`.
U właściciela `python` może nie być w PATH terminala – pełna ścieżka: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.

## Twarde reguły (skrót – pełne w docs/ZASADY.md)

* Zmiana logiki finansowej: **najpierw opis przyczyny i wpływu na liczby, potem zmiana**; ustawienia zmieniające liczby domyślnie wyłączone.
* Nie zgadujemy (waluta z sufiksu symbolu, nieznany typ operacji, brak ceny) – pytanie do użytkownika z przyciskiem, z opcją „Zdecyduj za mnie”
  opisującą źródło decyzji.
* Schemat: tylko nowa migracja (`invapp/db/migrations/00NN_*.sql`, nagłówek `WPLYW NA WYNIKI`), nigdy edycja zastosowanej.
* Wspólny kod (`core/`, `pricing/`, `web/`) nie zna brokerów po nazwie – tylko interfejs `invapp/brokers/base.py` (test kontraktu).
* Żadnych danych użytkownika w repozytorium (test prywatności); dane osobowe tylko jako skrót/maska.
* Wersji ze źródeł **nie uruchamiamy na prawdziwych danych** (`AplikacjaInwestycyjna`) – tylko `-dev` albo kopia; podgląd (`.claude/launch.json`)
  wskazuje dane `-dev`. Migracja ze źródeł zablokowałaby zainstalowaną wersję (kod to blokuje: `db/engine.source_migration_blocked`).
* Wydajność: bez zapytań w pętli po wierszach pliku (prefetch, `execute_values`, pamięć podręczna), indeks dla każdej kolumny wyszukiwania,
  „najnowszy wiersz” z tabel wskazań (`market_quote_latest`, `fx_quote_latest`), pomiar przed i po.
* Każda reguła finansowa ma test i mutację wykrywaną przez testy (`tests/mutation_check*.py`).
* Komunikaty dla użytkownika po polsku z polskimi znakami; komentarze w kodzie po polsku bez polskich znaków.
* Wydanie (tag `vX.Y.Z`) i wysyłka na GitHuba tylko na wyraźną prośbę właściciela.
