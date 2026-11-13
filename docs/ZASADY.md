# Zasady projektu

Ten dokument zbiera reguły, które obowiązują przy **każdej** zmianie: nowy broker, nowy moduł (np. PPK), poprawka, optymalizacja.
Wynikają z decyzji właściciela projektu i są pilnowane testami. Jeśli zmiana wymaga złamania którejś z nich – najpierw opis i zgoda.

## 1. Liczby

1. **Liczby muszą się zgadzać co do grosza.** Wartość portfela = ilość × aktualna cena z API (Yahoo) × kurs, w PLN. Każda nowa liczba
   na stronie ma niezależną kontrolę w `pricing/audit.py` albo test, który ją przelicza inną drogą.
   *Rozszerzenie (planowane, do decyzji sposobu): waluta prezentacji w ustawieniach – USD, EUR, GBP, CHF i inne popularne. Księga, wycena
   i kontrole dalej liczone w PLN (waluta księgi); na stronie kwoty × kurs PLN → wybrana waluta (z tego samego źródła kursów co wycena),
   z podpisem waluty i kursu. Każda pokazana kwota musi dać się odtworzyć: kwota w PLN × pokazany kurs.*
2. **Zmiana logiki finansowej = opis przyczyny i wpływu przed zmianą.** W migracji: nagłówek `WPLYW NA WYNIKI:` (co się zmieni w liczbach
   i dlaczego); w kodzie: komentarz z datą decyzji. Ustawienia, które zmieniają liczby, są domyślnie wyłączone.
3. **Nie zgadujemy.** Waluta i giełda instrumentu nie wynikają z sufiksu symbolu (`.PL`, `.UK`); nieznany typ operacji = `UNMAPPED`;
   brak ceny = jawnie oznaczona pozycja, nie szacunek z zakupu. Wszystko, czego nie wiemy, jest **pytaniem do użytkownika** z przyciskiem
   (a opcja „Zdecyduj za mnie” zawsze mówi, co przyjmie i z jakiego źródła).
4. **Bez sztucznych korekt.** Rozwiązania z kontroli liczb używają tylko istniejących mechanizmów (odpowiedź na pytanie, ponowne pobranie cen,
   przeliczenie z eksportów, wczytanie pliku). Nigdy „dopisz brakującą kwotę”.
5. **Koszt = gotówka z księgi** (z opłatami), nie koszt podatkowy. Zysk z inwestycji osobno od dywidend, odsetek, nagród i opłat.

## 2. Dane

1. **Dane surowe są nienaruszalne.** Księga (`transactions`), surowe wiersze (`raw_rows`), migawki brokera – tylko dopisywanie; zmiany
   przez wyzwalacze ochronne. Warstwa pochodna (partie, zamknięcia) liczy się zawsze od zera z księgi.
2. **Import atomowy i idempotentny** (`core/ledger_import.py`): plik = jedna transakcja; ten sam lub nowszy eksport dodaje tylko nowe
   operacje; kolejność plików bez znaczenia; każdy wiersz pliku ma wynik w `raw_rows` (kontrola G3: suma wyników = liczba wierszy).
3. **Usunięcie = jak gdyby nie wczytano** (`core/removal.py`), razem z decyzjami dotyczącymi tylko usuniętych danych.
4. **Decyzje użytkownika są zapisane ze źródłem, datą i historią** (kto, kiedy, z czego) – np. `corporate_actions.evidence.history`,
   `cash_deposit_terms`, `app_settings`.
5. **Schemat bazy zmienia tylko nowa migracja** `invapp/db/migrations/00NN_opis.sql`. Zastosowanej migracji nie wolno edytować
   (suma SHA-256); wyjątek: poprawka samych komentarzy wpisana do `COMMENT_ONLY_EDITS` w `db/migrate.py`.

## 3. Prywatność i bezpieczeństwo

1. Dane zostają na komputerze użytkownika (`%LOCALAPPDATA%\AplikacjaInwestycyjna`). Do internetu idą tylko symbole/ISIN (ceny, sektory),
   zapytania o kursy (NBP), specyfikacja XTB i pytanie o nową wersję (GitHub). Test `tests/test_prywatnosc.py` pilnuje listy adresów.
2. Dane osobowe (IBAN, imiona i nazwiska z opisów przelewów) nigdy nie trafiają do księgi ani surowych wierszy – tylko skrót i maska.
3. **Repozytorium jest publiczne**: żadnych eksportów, liczb użytkownika, numerów kont. Testy na prawdziwych plikach są lokalne
   (w `.gitignore`); w repozytorium tylko dane syntetyczne.
4. Serwer odpowiada tylko na `127.0.0.1` / `localhost` (nagłówki Host i Origin), zapisy wymagają nagłówka `X-Requested-With: invapp`,
   strona nie ładuje niczego z internetu (CSP). Rola bazy `investing_app` ma tylko odczyt; zapisy robi `investing_owner`.

## 4. Kod

1. **Python 3.12, bez nowych zależności bez potrzeby** (każda biblioteka trafia do instalatora; `requirements-lock.txt` = dokładne wersje).
   Interfejs bez zewnętrznych bibliotek JS/CSS (działa offline).
2. **Teksty dla użytkownika po polsku** (z polskimi znakami). Komentarze w kodzie po polsku bez polskich znaków (zgodnie z resztą kodu).
3. **Wspólny kod nie zna brokerów po nazwie.** Wszystko, co zależy od brokera, jest w `invapp/brokers/<broker>/` i idzie przez interfejs
   `invapp/brokers/base.py` (test `tests/test_kontrakt_brokerow.py` to sprawdza).
4. **Wydajność od początku**:
   * w pętli po wierszach pliku żadnych pojedynczych zapytań, jeśli da się inaczej: istniejące klucze pobierać raz (`prefetch`),
     zapisy wsadowo (`execute_values`), słowniki/pamięć podręczna na czas importu (konta, waluty, instrumenty);
   * nowa kolumna, po której się szuka (klucz obcy, filtr) = indeks w tej samej migracji;
   * tabele, które rosną z czasem (notowania, kursy), czytane przez „najnowszy wiersz” – wskaźnik/tabela „latest” (migracja 0020), nie `DISTINCT ON` po całej historii;
   * przed i po optymalizacji pomiar (czas + liczba zapytań) i dowód, że liczby są identyczne (testy).

## 5. Testy

1. Każda zmiana: test publiczny (dane syntetyczne, `tests/`) + – jeśli dotyczy prawdziwych plików – test lokalny (`tests/test_lokalne.py`).
2. **Testy mutacyjne** (`tests/mutation_check.py` dla schematu, `tests/mutation_check_import.py` dla kodu): każda reguła finansowa ma mutację,
   którą testy muszą wykryć. Nowa reguła = nowa mutacja. Mutacja celuje w tekst ostatniej definicji (nowa migracja zastępująca widok =
   przepięcie mutacji na nowy tekst).
3. Przed wydaniem: wszystkie testy publiczne i lokalne, obie serie mutacji, test instalatora na czystym Windowsie (GitHub Actions).

## 6. Listy kontrolne

**Przed zatwierdzeniem zmiany (commit):**
- [ ] testy publiczne: `python -m unittest tests.test_metrics … tests.test_kontrakt_brokerow` (pełna lista: `docs/ROZWOJ.md`)
- [ ] testy lokalne, jeśli zmiana dotyka importu, wyceny lub usuwania
- [ ] mutacje dla nowej reguły dopisane i wykryte
- [ ] `git status` bez plików z danymi; `tests/test_prywatnosc.py` zielony
- [ ] dokumentacja: `docs/ARCHITEKTURA.md` (mapa / zasady), poradnik, jeśli zmienia się sposób dodawania czegoś

**Przed wydaniem:** numer w `invapp/__init__.py`, opis zmian w tagu, wszystko powyżej + obie serie mutacji; po wydaniu sprawdzić
wydanie na GitHubie i ofertę aktualizacji dla poprzedniej wersji.
