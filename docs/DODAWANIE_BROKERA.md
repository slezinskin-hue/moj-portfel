# Dodawanie brokera (np. Revolut, mBank eMakler, Bossa)

Reszta aplikacji (import z interfejsu, zakładki, wartość i zysk, wykresy, wskaźniki, kontrola liczb z rozwiązaniami, usuwanie plików,
eksport) działa dla każdego brokera, który spełnia wspólny interfejs `invapp/brokers/base.py`. Wspólny kod **nie zna brokerów po nazwie**.

## Szybki start (generator)

```
python -m invapp.tools.nowy_broker revolut RVL "Revolut"
python -m unittest tests.test_import_revolut tests.test_kontrakt_brokerow
```

Generator tworzy działający od razu adapter dla prostego eksportu CSV i dopisuje brokera do listy `BROKERS`:

| plik | co zawiera | co dopasować |
|---|---|---|
| `invapp/brokers/revolut/parser.py` | `COLUMNS` (nagłówek), `TYPES` (typ z pliku → typ w księdze), `normalize()` → kandydat operacji | **wszystko** do prawdziwej próbki eksportu |
| `invapp/brokers/revolut/importer.py` | konto, instrumenty po ISIN, kolumny księgi; reszta to wspólny silnik `core/ledger_import.py` | numer konta (jeśli jest w pliku), powiązania (np. podatek → dywidenda) |
| `invapp/brokers/revolut/broker.py` | klasa brokera: rozpoznanie pliku, walidacja, import, partie FIFO, kroki cen | `matches()`, opis plików dla użytkownika, pytania (jeśli trzeba) |
| `tests/test_import_revolut.py` | import, ponowny import bez duplikatów, konflikt, nieznany typ, usunięcie = stan jak bez pliku | dane z prawdziwej próbki (zanonimizowane) |

## Krok po kroku

1. **Próbka eksportu** od użytkownika (oryginalny plik). Analiza: kolumny, typy operacji, waluty, identyfikator operacji, czy jest numer
   konta, saldo, stan pozycji, numery pozycji. Bez próbki niczego nie zgadujemy. Próbka zostaje lokalnie (folder `export/` jest w `.gitignore`).
2. **Generator** (wyżej) i dopasowanie `parser.py`:
   * `TYPES`: typy w księdze – `BUY`, `SELL`, `DIVIDEND`, `TAX_WITHHOLDING`, `INTEREST`, `TAX_INTEREST`, `FEE`, `REWARD`,
     `CASH_DEPOSIT`, `CASH_WITHDRAWAL` (znaki kwot: `ck_cash_sign` w migracji 0001). Typ spoza mapy = `UNMAPPED` (nie trafia do księgi,
     użytkownik widzi ostrzeżenie);
   * **klucz deduplikacji** = identyfikator operacji od brokera (`dedup_key = "<klucz>:<id>"`); gdy go brak – stabilny skrót pól
     znaczących (data, typ, kwota, instrument, licznik w ramach dnia), opisany w kodzie;
   * **skrót treści** (`row_hash`) z pól znaczących – ta sama operacja z inną treścią = `CONFLICT`, nie nadpisanie;
   * waluta inna niż PLN: szablon ją odrzuca (`UNMAPPED`); obsługa walut = osobna decyzja (kurs z pliku brokera, opis wpływu);
   * dane osobowe: opisy przelewów z nazwiskami / IBAN – nie do `payload`, tylko `core/common.mask_identifier` lub skrót.
3. **Partie**: eksport bez numerów pozycji → FIFO (`derive.rebuild_fifo`, jak Trade Republic); z numerami pozycji → jak XTB (`derive.rebuild`).
4. **Ceny**: instrumenty po ISIN dostają symbol Yahoo ze sprawdzeniem ceny (`pricing.resolve_isin_listings`, krok `"quick"`), jak w szablonie.
   Broker z własną specyfikacją instrumentów (jak XTB/OMI) – własny krok `"slow"`.
5. **Pytania do użytkownika** (opcjonalnie), np. waluta, lokata, split: `pending_questions()` zwraca słowniki
   `{id, kategoria, tytul, tresc, odpowiedzi: [{wartosc, etykieta, opis?, bez_pol?, potwierdz?}], pola?: [{nazwa, typ, etykieta, max?}]}`,
   `answer_question()` zapisuje decyzję z historią. `kategoria` ∈ `waluta | split | lokata | inne` – po niej kontrola liczb dobiera
   przycisk rozwiązania. Pierwsza odpowiedź = przycisk główny (zalecana), np. „Zdecyduj za mnie”.
6. **Wolne środki**: `free_funds = True`, jeśli saldo gotówki konta to zwykłe wolne środki (jak XTB); `False`, gdy gotówka jest inaczej
   liczona (np. lokata Trade Republic).
7. **Po usunięciu pliku**: `relink(conn, report)` uruchamia te same powiązania co import (podatek → dywidenda, przelewy, klasyfikacja wpłat).
8. **Testy**: wygenerowany test + test na prawdziwym pliku **lokalnie** (liczby z analizy; plik w `.gitignore`, np. dopisany do
   `tests/test_lokalne.py`) + mutacje reguł w `tests/mutation_check_import.py`. `tests/test_kontrakt_brokerow.py` sprawdza każdego brokera
   automatycznie (identyfikator, rozpoznawanie cudzych plików, pytania, kroki cen, brak kodu brokera poza jego folderem).

## Interfejs (`invapp/brokers/base.py`)

| element | obowiązkowy | opis |
|---|---|---|
| `code`, `key`, `name`, `accept`, `file_help` | tak | kod w bazie (`RVL`), klucz w adresie (`revolut`), nazwa, rozszerzenia plików, opis dla użytkownika |
| `matches(filename, head)` | tak | czy plik należy do brokera (nazwa + początek zawartości); bez efektów ubocznych |
| `validate_upload(filename, data)` | tak | rozmiar/format przed zapisem na dysk; błąd = `ImportFailed` po polsku |
| `import_path(conn, path, display_name)` | tak | import + przeliczenie partii, bez commitu; raport z licznikami |
| `rebuild_derived(conn)` | tak | partie od zera z księgi |
| `relink(conn, report)` | jeśli broker łączy operacje | powiązania od nowa po usunięciu pliku |
| `pricing_steps(conn, provider)` | nie | kroki zadania w tle `(tytuł, funkcja, faza)`; faza `quick` / `slow` / `history` |
| `pending_questions`, `answer_question` | nie | pytania do użytkownika (format wyżej) |
| `negative_state_fix(cur, listing_id)` | nie | propozycja dla kontroli liczb przy ujemnym stanie papierów (np. split z danych) |
| `free_funds` | nie | czy saldo gotówki to wolne środki (zakładka Gotówka) |

## Wspólny silnik importu (`invapp/core/ledger_import.py`)

`ImportBatch` robi to, co jest takie samo dla każdego brokera: partia, blokada importów brokera, wykrywanie duplikatów i konfliktów
(jedno zapytanie na plik), surowe wiersze, zbiorcze „widziano w tym pliku”, kontrola G3, raport. Do tego `ensure_account` (konto + zakresy)
i `IsinResolver` (ISIN → notowanie, waluta nieznana do czasu sprawdzenia). Broker pisze tylko: odczyt pliku → kandydaci → kolumny księgi.

Wzorce: `trade_republic/` (CSV z ISIN, FIFO, lokata, nagrody – na wspólnym silniku) i `xtb/` (zaawansowany: Excel z trzema arkuszami,
numery pozycji, konta z nazwy pliku, specyfikacja instrumentów, splity).
