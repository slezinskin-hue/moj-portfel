-- 0016: INDEKSY DO POROWNANIA NA WYKRESIE "Zwrot %" (decyzja uzytkownika 2026-11-12: Nasdaq, S&P 500, MSCI World, WIG20 - do zaznaczenia).
--   Zamkniecia dzienne z Yahoo Finance (ten sam dostawca co ceny historyczne), przeliczane na PLN kursem dnia z fx_rates (YAHOO_1D).
--   Dane referencyjne, niezalezne od portfela; lista indeksow: invapp/pricing/benchmarks.py.
-- WPLYW NA WYNIKI: brak - nowa tabela tylko do wykresu porownawczego; ksiega, partie, widoki i wszystkie kwoty BEZ ZMIAN.
CREATE TABLE benchmark_prices (
  symbol       TEXT NOT NULL,
  price_date   DATE NOT NULL,
  close_price  NUMERIC(20,8) NOT NULL CHECK (close_price > 0),
  currency     CHAR(3) NOT NULL,
  source       TEXT NOT NULL,
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, price_date, source)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON benchmark_prices TO investing_app;
  END IF;
END $$;
