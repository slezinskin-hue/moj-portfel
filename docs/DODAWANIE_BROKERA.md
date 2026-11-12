# Dodawanie brokera (np. Revolut)

Cała reszta aplikacji (import z interfejsu, zakładki, wartość i zysk, wykres, wskaźniki, kontrola liczb) działa dla każdego brokera,
który spełnia wspólny interfejs `invapp/brokers/base.py`. Dodanie brokera to:

1. **Próbka eksportu** od użytkownika (oryginalny plik) – na niej powstaje analiza formatu: kolumny, typy operacji, waluty, identyfikatory,
   co jest w pliku, a czego brak (saldo, pozycje, numer konta). Bez próbki niczego nie zgadujemy.
2. **Folder** `invapp/brokers/<nazwa>/` z:
   * `parser.py` – czytanie pliku i zamiana wierszy na kandydatów transakcji (typy: BUY, SELL, DIVIDEND, TAX_WITHHOLDING, INTEREST, TAX_INTEREST,
     FEE, REWARD, CASH_DEPOSIT, CASH_WITHDRAWAL …); nieznany typ = UNMAPPED; dane osobowe tylko jako skrót/maska;
   * `importer.py` – zapis do księgi: partia importu, `raw_rows`, klucz deduplikacji (najlepiej identyfikator brokera), konflikty, konto;
   * `broker.py` – klasa dziedzicząca po `Broker`: `code`, `key`, `name`, `accept`, `file_help`, `matches()`, `validate_upload()`,
     `import_path()`, `rebuild_derived()` (zwykle `derive.rebuild_fifo(conn, KOD)`), `pricing_steps()` – lista `(tytuł, funkcja, faza)`, faza
     `"quick"` (przed pierwszym pobraniem notowań, np. powiązanie ISIN → symbol jak w Trade Republic), `"slow"` (np. specyfikacja do pobrania) albo
     `"history"`; opcjonalnie `pending_questions()` / `answer_question(conn, id, odpowiedź, pola)` – pytania do użytkownika z przyciskami
     i polami formularza (np. stopa lokaty, dzień splitu).
3. **Jedna linijka** w `invapp/brokers/__init__.py`: dopisanie do `BROKERS`. Wpis instytucji i zakres `BROKER:<KOD>` tworzą się same przy starcie,
   a zakładka pojawia się w interfejsie automatycznie.
4. **Testy** `tests/test_import_<nazwa>.py` na prawdziwym pliku (liczby z analizy), idempotencja, kolejność plików, prywatność, nieznane typy,
   oraz mutacje w `tests/mutation_check_import.py`.

Wzorce: `trade_republic/` (CSV z ISIN, bez numerów pozycji – FIFO, waluta z zewnętrznego źródła) i `xtb/` (Excel z numerami pozycji, konta z nazwy pliku,
specyfikacja instrumentów brokera).
