# Instalator, wydania i aktualizacje

## Dla użytkownika

1. Wejdź na stronę repozytorium na GitHubie → **Releases** i pobierz `MojPortfel-Setup-X.Y.Z.exe` z najnowszego wydania.
2. Uruchom plik. Instalacja nie wymaga uprawnień administratora: program trafia do `%LOCALAPPDATA%\Programs\MojPortfel`, skróty do menu Start
   (i opcjonalnie na pulpit).
   * Windows SmartScreen może pokazać „System Windows ochronił ten komputer” (instalator nie jest podpisany płatnym certyfikatem):
     **Więcej informacji → Uruchom mimo to**. Sumę SHA-256 pliku możesz porównać z tą w opisie wydania.
3. Aplikacja otwiera się **we własnym oknie** (jak zwykły program; pod spodem silnik Edge WebView2 wbudowany w Windows 10/11, adres `http://127.0.0.1:…`,
   tylko ten komputer). Gdyby okno nie mogło się otworzyć, aplikacja otworzy się w przeglądarce. Wczytaj eksporty z XTB (.xlsx) albo Trade Republic (.csv).
   Dane zostają w `%LOCALAPPDATA%\AplikacjaInwestycyjna`. Kolejne eksporty możesz wczytywać w każdej chwili – duplikaty są pomijane, dochodzą
   tylko nowe operacje.
   **Usunięcie pliku**: „＋ Wczytaj pliki” → lista „Wczytane pliki” → **Usuń**. Z portfela znika to, co pochodzi tylko z tego pliku (operacje
   obecne też w innym wczytanym pliku zostają, konto bez danych znika) – stan jak gdyby pliku nie wczytano. Przed usunięciem robi się kopia
   zapasowa bazy; plik można wczytać ponownie.
4. Zamknięcie: zamknij okno (albo przycisk **⏻**) – zatrzymuje serwer i bazę. Ponowne kliknięcie skrótu, gdy aplikacja działa, przenosi jej okno
   na wierzch (bez drugiej kopii).
5. **Aktualizacje**: aplikacja sprawdza najnowsze wydanie na GitHubie (przy uruchomieniu i co kilka godzin) i pyta „Dostępna nowa wersja –
   zaktualizować teraz?”. **Zaktualizuj teraz** pobiera instalator (tylko z wydania tego repozytorium, sprawdza sumę SHA-256), zamyka aplikację,
   instaluje nową wersję i ją uruchamia. **Nie teraz** zamyka pytanie; aktualizację można zrobić później przyciskiem **⬆ Aktualizacja**
   w nagłówku albo linkiem „Sprawdź aktualizacje” na dole strony (przy następnym uruchomieniu aplikacja zapyta ponownie).
   Dane zostają; przed zmianą schematu bazy robi się kopia zapasowa (`kopie\`). (Wersja 0.2.x pokazuje jeszcze pasek z przyciskiem
   „Zainstaluj aktualizację” – działa tak samo.)
6. Odinstalowanie: Ustawienia Windows → Aplikacje → Mój Portfel. Na końcu deinstalator pyta, czy usunąć też dane z
   `%LOCALAPPDATA%\AplikacjaInwestycyjna` (baza, kopie zapasowe, ustawienia) – wybierz „Tak” np. przed oddaniem komputera; domyślnie dane zostają.

## Jak powstaje instalator

`instalator\build.ps1`:
1. pobiera oficjalny **Python 3.12 embeddable** z python.org (suma SHA-256 sprawdzana),
2. instaluje do niego biblioteki z `requirements-lock.txt` (dokładne wersje wszystkich paczek, tylko gotowe paczki binarne Windows x64),
3. kopiuje kod `invapp\` i zapisuje `invapp\_build.py` (repozytorium GitHub, z którego aplikacja pobiera aktualizacje; ten plik oznacza też
   „wersję zainstalowaną” – z prawdziwymi danymi),
4. kompiluje pliki `.pyc` i buduje instalator **Inno Setup 6** (`instalator\moj-portfel.iss`) → `dist\MojPortfel-Setup-X.Y.Z.exe` (~24 MB).

Instalator przed instalacją na starej wersji zatrzymuje działającą aplikację (`python -m invapp --stop`), usuwa stare pliki programu (nie dane)
i po instalacji uruchamia aplikację ponownie. Aplikacja działa we własnym oknie (pywebview + WebView2), bez okna konsoli (`pythonw.exe`); dziennik: `%LOCALAPPDATA%\AplikacjaInwestycyjna\logi`.

## Wydania (GitHub Actions)

* `.github/workflows/testy.yml` – testy przy każdym wysłaniu na `main` i w pull requestach (Linux, dane syntetyczne).
* `.github/workflows/wydanie.yml` – po wysłaniu tagu `vX.Y.Z`: testy → instalator na Windows (sprawdza zgodność tagu z wersją w kodzie)
  → **GitHub Release** z instalatorem, plikiem `.sha256` i sumą SHA-256 w opisie. Od tej chwili zainstalowane aplikacje widzą aktualizację.

Dla publicznych repozytoriów GitHub Actions jest bezpłatne. Repozytorium musi być **publiczne**, bo aplikacje sprawdzają wydania i pobierają
instalator bez logowania. Jak przygotować i wypuścić wersję krok po kroku: [ROZWOJ.md](ROZWOJ.md).

## Bezpieczeństwo i licencje

* Aktualizacja pobiera instalator wyłącznie z adresu wydania tego repozytorium (`https://github.com/<repo>/releases/download/…`) i sprawdza
  sumę SHA-256. Chroń konto GitHub (uwierzytelnianie dwuskładnikowe) – kto może tworzyć wydania, może dostarczyć aktualizację.
* Instalator nie jest podpisany certyfikatem kodu (koszt ~200–400 EUR/rok); bez podpisu SmartScreen ostrzega przy pierwszych pobraniach.
* Dołączone składniki: Python (PSF), PostgreSQL (licencja PostgreSQL, przez pgserver – Apache 2.0), FastAPI/Starlette/uvicorn/pydantic (MIT/BSD),
  Jinja2/MarkupSafe (BSD), openpyxl (MIT), pypdf (BSD), psycopg2-binary (LGPL), psutil (BSD). Ceny z nieoficjalnego API Yahoo Finance
  (tylko do użytku prywatnego), kursy NBP (publiczne API).
