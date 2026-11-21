-- 0021: DOKLADNIEJSZA HISTORIA CEN NA WYKRESIE (decyzja uzytkownika 2026-11-21: "Tak, wdrazaj" po diagnozie 410 dni z oszacowanymi cenami).
--   1. Podzialy akcji wg dostawcy (provider_splits). Yahoo podaje historyczne zamkniecia PRZELICZONE o wszystkie pozniejsze splity. Ksiega stosuje
--      tylko splity potwierdzone (corporate_actions CONFIRMED) - wtedy partie sa w jednostkach po splicie i ceny Yahoo pasuja. Gdy pozycja byla
--      zamknieta PRZED splitem (np. SONY: sprzedana w 06.2024, split 5:1 w 10.2024), ksiega nie ma czego korygowac, a ceny Yahoo sa 5x nizsze
--      od faktycznych. Odtad przy wycenie dnia d cena dostawcy jest mnozona przez wspolczynnik splitow dostawcy po dniu d, ktorych nie ma
--      wsrod potwierdzonych korekt ksiegi (cofniecie przeliczenia) - takze przy weryfikacji powiazania symbolu.
--   2. Ceny zastepcze (zrodlo YAHOO_1D_PROXY w prices, powiazanie provider = 'YAHOO_1D_PROXY'): ta sama papier (ten sam ISIN) z innej gieldy,
--      uzywany TYLKO w dni, w ktorych wlasne notowanie nie ma ceny (np. VUAA.DE: Yahoo ma historie dopiero od 2024-12-30). Powiazanie zastepcze
--      jest weryfikowane (zgodnosc z wlasna historia w dniach wspolnych i z cenami zakupow) i jawnie oznaczone na wykresie.
-- WPLYW NA WYNIKI: zmienia sie TYLKO historia na wykresie (seria dzienna) i miary z niej liczone (TWR, obsuniecia, zmiennosc, mapa miesiecy,
--   XIRR/CAGR w czasie) w dniach, ktore dotad mialy cene OSZACOWANA (ostatnia cena transakcji). Biezaca wartosc, zysk, dochody, ksiega i partie
--   bez zmian (wycena biezaca uzywa notowan na zywo, nie tej tabeli).
CREATE TABLE provider_splits (
  listing_id   BIGINT NOT NULL REFERENCES listings,
  provider     TEXT NOT NULL,
  split_date   DATE NOT NULL,                       -- pierwszy dzien notowan po splicie (data gieldy)
  ratio_new    NUMERIC(18,8) NOT NULL CHECK (ratio_new > 0),
  ratio_old    NUMERIC(18,8) NOT NULL CHECK (ratio_old > 0),
  provider_symbol TEXT NOT NULL,
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (listing_id, provider, split_date)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON provider_splits TO investing_app;                      -- zapis robi zadanie cen w tle rola wlasciciela
  END IF;
END $$;
