# Waluta prezentacji (wersja 0.4.0, decyzja właściciela 2026-11-13: opcja 2 – pełne liczenie w wybranej walucie)

Ustawienie ⚙ „Waluta”: **PLN (domyślnie)**, USD, EUR, GBP, CHF. Przy PLN aplikacja liczy **dokładnie tak jak dotąd** – ta ścieżka nie jest
ruszana. Przy innej walucie (W) każda kwota jest liczona tak, jak liczyłby inwestor rozliczający się w W: każdy przepływ pieniędzy po kursie
z **dnia, w którym nastąpił**, a bieżąca wartość po kursie z teraz.

## Kursy

* **kurs(d)** = ile złotych kosztuje 1 W w dniu d: kurs dzienny z Yahoo (zamknięcie, ten sam typ kursu co wykres portfela), dla dnia bez notowań
  ostatni wcześniejszy (najwyżej 7 dni – jak ceny na wykresie). Brak kursu = kwota jawnie oznaczona jako brak (nigdy szacunek).
* **kurs teraz** = kurs bieżący wg ustawienia kursów (Yahoo na żywo / NBP) – ten sam, którym wyceniane są papiery.
* Księga, partie i kontrole w PLN zostają bez zmian (waluta księgi). Kwoty w W są **liczone z kwot PLN z księgi**, nie z cen w walutach notowania,
  więc zgadzają się z tym, co faktycznie zapłaciłeś i dostałeś.

## Definicje (W ≠ PLN)

| liczba | w W |
|---|---|
| wartość pozycji, lokaty, gotówki dziś | wartość w PLN / kurs teraz |
| koszt otwartej partii | koszt PLN partii / kurs(dzień zakupu) |
| zysk niezrealizowany | wartość W − koszt W |
| zysk zrealizowany (każde zamknięcie partii) | przychód PLN / kurs(dzień sprzedaży) − koszt PLN / kurs(dzień zakupu) |
| dywidendy, podatek, odsetki, nagrody, opłaty | kwota PLN / kurs(dzień operacji) |
| zysk całkowity | niezrealizowany + zrealizowany + dochody (jak w PLN) |
| wykres: wartość dnia d | wartość PLN dnia d / kurs(d) |
| wykres: wpłaty netto | suma przepływów dnia (zakupy, sprzedaże; dla kont z lokatą przelewy) / kurs(dzień przepływu) |
| TWR, XIRR, obsunięcia, mapa miesięcy | te same wzory na serii w W |
| Dziś (pozycja) | ilość × (cena teraz w W − cena zamknięcia z wczoraj / kurs(wczoraj)) |
| indeksy porównawcze | poziom indeksu w PLN / kurs(d) |

**Skutek dla liczb:** wynik w W różni się od wyniku w PLN o zmianę kursu złotego. Przykład: akcja za 400 zł przy 4,00 zł/$ (100 $), dziś warta
396 zł przy 3,60 zł/$ (110 $): w PLN −4 zł (−1%), w USD +10 $ (+10%). Procenty w W i w PLN są różne – to zamierzone.

## Kontrole (kontrola liczb, tylko gdy W ≠ PLN)

* niezależne przeliczenie partia po partii (koszt W, zrealizowany W) i porównanie z liczbami na stronie;
* tożsamość: zysk z inwestycji W = wartość W − (zakupy W − sprzedaże W);
* zysk całkowity W = zysk z inwestycji W + dochody W;
* ostatni punkt wykresu W = wartość W z kafelka.

Eksport do Excela zostaje w PLN (waluta księgi), z adnotacją.
