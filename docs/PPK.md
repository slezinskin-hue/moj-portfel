# PPK – plan modułu „Oszczędności emerytalne”

Stan: **warstwa bazy gotowa** (schemat z migracji `0001_baseline.sql`, sprawdzony testami lokalnymi), **brak modułu w aplikacji**
(API, zakładka, wliczanie do wartości). Ten dokument opisuje, co jest i co dodać – według `docs/DODAWANIE_MODULU.md`.

## Co już jest w bazie

| obiekt | rola |
|---|---|
| `accounts` z `product_type = 'PPK'`, `account_class = 'RETIREMENT'`, `tracking_mode = 'BALANCE_ONLY'` | rachunek PPK (bez księgi operacji – tylko stany z datą) |
| `retirement_accounts` | rachunek PPK ↔ instytucja (np. PKO TFI) i fundusz zdefiniowanej daty; wyzwalacz pilnuje poprawnego typu konta |
| `retirement_funds`, `retirement_fund_strategies`, `…_target_allocations`, `…_glide_path`, `…_allocation_reports` | fundusz i jego strategia (alokacja docelowa, ścieżka „glide path”) – **tylko z oficjalnego źródła z datą** (wyzwalacze `fn_guard_official_source`) |
| `ppk_contribution_categories`, `ppk_category_aliases`, `fn_ppk_resolve_category` | kategorie wpłat (pracownik podstawowa/dodatkowa, pracodawca podstawowa/dodatkowa, wpłata powitalna, dopłata roczna…) i rozpoznawanie etykiet z serwisu instytucji |
| `ppk_snapshots`, `ppk_snapshot_lines` | **niezmienne migawki stanu**: data wyceny, wartość całkowita wg instytucji, wiersze kategorii (wpłaty narastająco, wartość); stany są narastające – używa się najnowszej, nie sumy |
| `fn_ppk_commit_snapshot(account, data, wartość, wiersze JSONB, zatwierdził, źródło, uwaga)` | zatwierdzenie migawki: `CREATED` / `DUPLICATE` (ta sama treść) / `CORRECTION` (ta sama data, inna treść – poprzednia zastąpiona, nic nie znika) |
| `ppk_import_drafts`, `fn_ppk_parse_amount` | szkic wklejonej tabeli z serwisu instytucji (np. tekst kopiowany z PKO) przed zatwierdzeniem |
| `v_ppk_account_current` | stan bieżący: wartość, wpłaty wg płatnika (pracownik / pracodawca / państwo), kontrola sumy kategorii, kompletność, nieświeżość (> 45 dni) |
| `v_ppk_history`, `v_ppk_snapshot_deltas`, `v_ppk_category_deltas`, `v_ppk_snapshot_warnings` | historia do wykresu, zmiany między stanami, ostrzeżenia |
| zakres `PPK` (kind `RETIREMENT`) i miejsce w `v_net_worth` | PPK liczone raz w majątku; w aktywach inwestycyjnych jako `RETIREMENT` |

Zasady z decyzji (D-PPK-1): wpłaty pracownika, pracodawcy i państwa wchodzą do kapitału; obok pokazywane osobno „łączne wpłaty”
i „wpłaty własne pracownika”. **Różnica wartość − wpłaty to zmiana orientacyjna, nie precyzyjny wynik inwestycyjny** (brak historii jednostek).
Brak danych ≠ zero; data wyceny jest obowiązkowa (nie zgadujemy).

## Co dodać (moduł w aplikacji)

1. **`invapp/core/ppk.py`**: `accounts(cur)`, `add_account(cur, nazwa, instytucja, fundusz)` (konto `PPK/RETIREMENT/BALANCE_ONLY` + `retirement_accounts`
   + zakresy `PPK`, `NET_WORTH`), `parse_table(text)` (wklejona tabela z serwisu → wiersze kategorii przez `fn_ppk_resolve_category` i `fn_ppk_parse_amount`;
   nierozpoznana etykieta = pytanie do użytkownika, nie zgadywanie), `commit(cur, konto, data, wartość, wiersze)` → `fn_ppk_commit_snapshot`,
   `current(cur)`, `history(cur, konto)`.
2. **API** (`owner_write`): `GET /api/ppk`, `POST /api/ppk/konto`, `POST /api/ppk/migawka/podglad` (szkic: rozpoznane kategorie, sumy, ostrzeżenia –
   bez zapisu), `POST /api/ppk/migawka` (zatwierdzenie).
3. **Zakładka „PPK”**: kafelki (wartość, wpłaty pracownik / pracodawca / państwo, zmiana orientacyjna, data wyceny i nieświeżość), wklejanie tabeli
   z podglądem przed zatwierdzeniem, historia (wykres wartość vs wpłaty – `Charts.TimeChart`), ostrzeżenia z `v_ppk_snapshot_warnings`.
4. **Wartość**: ustawienie „uwzględniaj PPK w wartości majątku” (domyślnie wyłączone) – jak gotówka: `total_value`, bez zmiany zysku i wskaźników
   portfela inwestycyjnego (wynik PPK jest orientacyjny).
5. **Kontrola liczb**: suma kategorii = wartość instytucji (`control_status`), kompletność kategorii, nieświeża wycena → przyczyna i rozwiązanie
   („wklej aktualną tabelę z serwisu instytucji”).
6. **Eksport**: arkusz „PPK” (migawki i kategorie). **Usuwanie**: dane wpisane ręcznie – nie znikają przy „Usuń wszystko” (jak Gotówka).
7. **Testy**: publiczny `tests/test_ppk.py` na tabeli syntetycznej (rozpoznanie etykiet, CREATED/DUPLICATE/CORRECTION, wartości bieżące,
   uprawnienia, 403 bez nagłówka) + mutacje; istniejące lokalne testy PPK w `tests/test_model_principles.py` zostają punktem odniesienia.

## Do decyzji użytkownika przed implementacją

* Czy PPK ma być doliczane do „Wartości portfela” w zakładce WSZYSTKIE (ustawienie) czy pokazywane tylko w osobnej zakładce i w majątku.
* Źródło danych: wklejanie tabeli z serwisu instytucji (zalecane – bez logowania) czy ręczne wpisywanie pól.
* Czy dane strategii funduszu (alokacja, glide path) wpisywać – tylko z oficjalnego dokumentu z datą.
