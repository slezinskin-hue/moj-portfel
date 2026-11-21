# Wbudowana baza PostgreSQL – stan i plan wymiany źródła binarek

Rozpoznanie z 2026-11-21 (bez zmian w kodzie). Decyzja o wymianie – osobno, przed którymś wydaniem.

## Stan

* Aplikacja bierze z pakietu `pgserver` **tylko pliki programu PostgreSQL** (`pg_ctl`, `initdb`, `postgres`, `pg_dump`…) – jedyne miejsce
  to `db/engine.bin_path()`. Serwerem zarządza nasz kod (`db/engine.py`), nie `pgserver`.
* `pgserver` 0.1.4 to **ostatnie wydanie (2024-06-08)**; zawiera PostgreSQL **16.2** (luty 2024). Od tego czasu wyszło kilkanaście
  wydań poprawkowych 16.x (także poprawki bezpieczeństwa). Główna wersja 16 ma wsparcie twórców PostgreSQL do listopada 2028.
* Ryzyko dziś jest **małe**: serwer słucha tylko na 127.0.0.1, losowe hasła (scram), dostęp ma tylko ten komputer. Ryzyko na przyszłość:
  pakiet nieutrzymywany – gdyby przestał się instalować z nowym Pythonem albo Windowsem, nie ma kto go poprawić.

## Alternatywy

| źródło | stan | ocena |
|---|---|---|
| `pgserver` (obecnie) | 0.1.4, PG 16.2, bez wydań od 2,5 roku | działa, ale stare poprawki i brak utrzymania |
| `postgresql-wheel` | 14.1, tylko Linux, Python < 3.10 | odpada |
| `py-pglite` (PGlite w WASM przez Node) | wymaga Node.js | odpada (nowa duża zależność) |
| **oficjalne binarki EDB** `postgresql-16.x-1-windows-x64-binaries.zip` (get.enterprisedb.com) | dostępne np. **16.13**, także 17.x i 18.x | **zalecane** – to samo źródło co oficjalny instalator PostgreSQL dla Windows |

## Zalecany plan (gdy zapadnie decyzja)

1. **Ta sama główna wersja (16.2 → 16.13)** – zmiana wersji poprawkowej **nie wymaga migracji danych** (format katalogu danych ten sam);
   wystarczy podmienić pliki programu. Żadna liczba w aplikacji się nie zmienia.
2. `instalator/build.ps1`: pobranie zipa EDB z **sprawdzeniem SHA-256** (tak jak Python embeddable), wypakowanie tylko `bin`, `lib`, `share`
   (bez pgAdmina i dokumentacji – zip ma ~325 MB, potrzebne pliki ok. 40–60 MB) do `invapp\pgsql\`.
3. `db/engine.bin_path()`: najpierw `invapp\pgsql\bin`, zapasowo `pgserver` (wersja ze źródeł i testy na Linuksie w CI – tam można użyć
   pakietu systemowego PostgreSQL 16 z apt).
4. Test: `tests/test_engine_and_brokers` + pełny zestaw lokalny + test instalatora na czystym Windowsie (CI) + aktualizacja na istniejącej
   instalacji (dane z 16.2 otwierane przez 16.13).
5. Przejście na 17/18 (nowa główna wersja) – dopiero osobno i tylko z potrzeby: wymaga `pg_dump` → nowy katalog danych → przywrócenie
   (mechanizm kopii już jest w `db/backup.py`).
