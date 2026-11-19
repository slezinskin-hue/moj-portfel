# PPK – moduł „Oszczędności emerytalne”

Stan: **gotowy od wersji 0.5.0** (decyzja właściciela 2026-11-19: dane wpisywane ręcznie, opcjonalnie wklejany tekst ze strony instytucji;
PKO TFI nie udostępnia pliku eksportu). Baza – schemat z migracji `0001_baseline.sql` (sekcja 9a); logika – `invapp/core/ppk.py`.

## Jak to działa dla użytkownika

1. Zakładka **PPK** → „Dodaj rachunek”: nazwa (domyślnie „PPK”), instytucja (np. PKO TFI), fundusz (np. „PKO Emerytura 2055”), rok docelowy
   (opcjonalnie), data przystąpienia.
2. „Wpisz stan”: data wyceny, wartość całkowita i dla każdej składki (podstawowa / dodatkowa pracownika i pracodawcy, powitalna, dopłata roczna)
   **wpłaty narastająco** i **wartość** – tak jak w tabeli na stronie instytucji. Sumy i zgodność z wartością całkowitą liczą się na bieżąco.
   Opcjonalnie: wklejenie tekstu ze strony („uzupełnij pola za mnie”) – aplikacja tylko wypełnia pola; tekst nie jest zapisywany.
3. Kolejne stany (np. raz w miesiącu, albo starsze z wyciągów – data z przeszłości) budują historię i wykres „wartość vs wpłaty”.

## Liczby

* **Wartość** = wartość całkowita wg instytucji (nadrzędna); suma wartości składek jest tylko kontrolą (tolerancja 1 gr na wiersz).
* **Łączne wpłaty** = suma wpłat wszystkich składek (pracownik + pracodawca + państwo) – tylko gdy każda wpisana składka ma wpłaty; inaczej „brak danych”.
* **Zmiana wartości** = wartość − łączne wpłaty; % od łącznych wpłat. **Orientacyjna**: instytucja podaje stany, nie daty wpłat, więc nie ma
  stopy zwrotu (TWR/XIRR) – zgodnie z decyzją D-PPK-1.
* **Ponad Twoje wpłaty** = wartość − wpłaty pracownika (podstawowe + dodatkowe); % od wpłat pracownika.
* Każda składka: zmiana = wartość − wpłaty, % od wpłat (od zera – brak).
* Obowiązuje stan z **najpóźniejszą datą wyceny** (stany są narastające – nigdy się ich nie sumuje); starszy stan wpisany później go nie zastępuje.
* Kwoty zawsze w PLN (przy innej walucie prezentacji wartość doliczona do portfela jest przeliczana kursem teraz).

## PPK w portfelu (ustawienie „Uwzględniaj PPK”, decyzja właściciela 2026-11-19)

* Domyślnie **wyłączone** – wtedy rachunek PPK (zakresy `PPK` i `NET_WORTH`, **nie** `INVESTMENT_ALL`) nie zmienia żadnej liczby portfela
  (test `TestPortfolioUnchanged`).
* Włączone (tylko portfel WSZYSTKIE; XTB i Trade Republic bez zmian):
  * **wartość** (i aktywa) + wartość PPK,
  * **wpłaty** + wszystkie składki (pracownika, pracodawcy i państwa – jako kapitał, D-PPK-1),
  * **zysk całkowity** + jedna linia „PPK” = wartość − łączne wpłaty (bez rozbicia na dopłaty),
  * **wykres i wskaźniki** (TWR, XIRR, obsunięcia, mapa miesięcy, rok do roku) – z serii dziennej odtworzonej modelem (niżej); seria zaczyna się
    od przystąpienia do PPK, jeśli jest wcześniejsze niż pierwsza operacja u brokera.
* Rachunek, którego najnowszy stan nie ma wpłat wszystkich składek, jest liczony tylko w wartości (bez zysku i wykresu) – ostrzeżenie w kontroli liczb.
* Waluta prezentacji inna niż PLN: wartość po kursie teraz, każda odtworzona wpłata po kursie ze swojego dnia (jak zakupy).

## Model w czasie (`ppk.model`, `ppk.account_model`)

Instytucja podaje tylko stany, więc dni wpłat i wartość dzień po dniu są **odtworzone** (każda kwota sumuje się co do grosza do wpisanych stanów):

* **składki pracownika i pracodawcy** – co miesiąc 15. dnia (pracodawca przekazuje składki do 15. dnia następnego miesiąca), pierwsza w miesiącu
  po przystąpieniu; przyrost wpłat między stanami rozłożony równo (reszta groszy w ostatniej),
* **wpłata powitalna** – po 3 pełnych miesiącach kalendarzowych uczestnictwa, zaksięgowana 30 dni po końcu kwartału, w którym minęły
  (przystąpienie 10.05.2023 → 30.10.2023),
* **dopłata roczna** – 15 kwietnia roku następnego (ustawa o PPK); przyrost n × 240 zł → po 240 zł na n najnowszych terminów,
* kwota bez pasującego terminu albo spadek wpłat (wypłata) – w dniu stanu, w którym się pojawiła,
* **wartość** – między kolejnymi stanami jedna stała stopa dzienna `g` dobrana bisekcją tak, by `V(d) = V(d−1) × g + wpłata(d)` dawało
  dokładnie wartość następnego stanu; w dniu stanu wartość = wartość wg instytucji; po ostatnim stanie – bez zmian (brak informacji).
  To szacunek: nie pokazuje wahań rynku (do tego potrzebne byłyby ceny jednostek funduszu).

## Prywatność

Zapisywane: nazwa rachunku, instytucja, fundusz, daty, kwoty. **Nie** zapisywane: numer rejestru / rachunku, nazwa pracodawcy, wklejony tekst
(`ppk_import_drafts` nie jest używane).

## Kontrola liczb i testy

* `audit._ppk_checks` (portfel WSZYSTKIE): suma składek = wartość; liczby zakładki = niezależne przeliczenie z `ppk_snapshots` /
  `ppk_snapshot_lines` (bez widoków); stan nie starszy niż 45 dni (ostrzeżenie, z przyczyną i rozwiązaniem w `diagnose.py`);
  przy włączonym „Uwzględniaj PPK” – model trafia w każdy wpisany stan (odtworzone wpłaty do dnia stanu = wpłaty stanu z SQL, wartość = wartość stanu).
* `tests/test_ppk.py` (dane syntetyczne) + mutacje „PPK 0.5” w `tests/mutation_check_import.py`; reguły bazy – mutacje „PPK:” w `tests/mutation_check.py`.

## API

`GET /api/ppk` · `POST /api/ppk/konto` · `POST /api/ppk/konto/{id}/stan` · `POST /api/ppk/konto/{id}/usun` · `POST /api/ppk/odczytaj`
(wklejony tekst → propozycja pól, połączenie tylko do odczytu). Zapisy przez `owner_write` (nagłówek ochronny, 403 bez niego).

## Na później (nie zrobione)

* Strategia funduszu (alokacja, glide path) – tabele są, wymagają oficjalnego dokumentu z datą.
* Liczba i cena jednostek (`units_total`, `unit_price`) – pozwoliłyby aktualizować wartość automatycznie, gdy znajdzie się źródło cen jednostek.
