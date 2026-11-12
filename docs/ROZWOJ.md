# Praca nad aktualizacjami: zainstalowana aplikacja + wersja rozwojowa

Na jednym komputerze działają obok siebie dwie rzeczy i **nie mieszają danych**:

| | Zainstalowana aplikacja | Wersja rozwojowa (folder z kodem) |
|---|---|---|
| Uruchamianie | skrót „Mój Portfel” | `python uruchom.py` |
| Dane | `%LOCALAPPDATA%\AplikacjaInwestycyjna` (prawdziwe) | `%LOCALAPPDATA%\AplikacjaInwestycyjna-dev` (testowe) |
| Znaczek na stronie | – | „wersja deweloperska” |
| Aktualizacje | z GitHub Releases (pasek na stronie) | to, co właśnie zmieniasz |

Obie mogą działać jednocześnie (każda na swoim porcie). Nowe migracje bazy z wersji rozwojowej działają tylko na danych testowych,
więc zainstalowana aplikacja nie zobaczy „bazy nowszej niż program”.

## Cykl pracy

1. **Zmiany w kodzie** w folderze z kodem, start: `python uruchom.py`.
2. **Test na prawdziwych liczbach** (opcjonalnie): zamknij obie aplikacje (⏻) i skopiuj prawdziwe dane do wersji rozwojowej:
   ```
   python -m invapp.tools.kopiuj_dane
   ```
   Kopia zastępuje dane testowe; dane zainstalowanej aplikacji nie są zmieniane.
3. **Testy automatyczne** (te same co na GitHubie):
   ```
   python -m unittest tests.test_metrics tests.test_omi_parser tests.test_pricing tests.test_history tests.test_deposits tests.test_ponowny_import tests.test_aktualizacje tests.test_tryby tests.test_engine_and_brokers tests.test_app_skeleton
   ```
   Pełny zestaw na Twoich prawdziwych eksportach (pliki w `export\`, testy lokalne – nie trafiają do repozytorium):
   ```
   python -m unittest tests.test_model_principles tests.test_step1_migrations tests.test_import_xtb tests.test_import_tr tests.test_app_import tests.test_lokalne
   python tests\mutation_check.py
   python tests\mutation_check_import.py
   ```
4. **Test instalatora** (opcjonalnie, przed wydaniem): zbuduj go lokalnie i zainstaluj na swojej zainstalowanej wersji – tak samo jak zrobi
   to aktualizacja u innych:
   ```
   powershell -ExecutionPolicy Bypass -File instalator\build.ps1 -UpdateRepo "<twoj-login>/moj-portfel"
   ```
   Wynik: `dist\MojPortfel-Setup-X.Y.Z.exe`.
5. **Wydanie**: zmień numer w `invapp/__init__.py` (np. `__version__ = "0.3.0"`), zapisz zmiany i wyślij tag z opisem zmian:
   ```
   git add -A
   git commit -m "Wersja 0.3.0: ..."
   git tag -a v0.3.0 -m "Co nowego: ..."
   git push origin main --follow-tags
   ```
   GitHub Actions zbuduje instalator i utworzy wydanie (Releases → v0.3.0). Twoja zainstalowana aplikacja i aplikacje innych pokażą
   „Dostępna nowa wersja 0.3.0”.

## Zasady

* **Nigdy nie dodawaj do repozytorium danych osobistych**: folder `export\`, testy na prawdziwych plikach i `docs\historia\` są w `.gitignore`.
  Przed `git add` sprawdź `git status` – nie powinno być tam plików z Twoimi eksportami ani liczbami.
* Zmiana schematu bazy = **nowa migracja** (`invapp/db/migrations/00NN_opis.sql`), nigdy edycja zastosowanej.
* Numer wersji w tagu musi być taki sam jak w `invapp/__init__.py` (budowanie instalatora to sprawdza).
