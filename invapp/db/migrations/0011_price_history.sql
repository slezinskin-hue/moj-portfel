-- 0011: historia cen (wykres portfela w czasie)
--   * ceny dzienne (zamkniecia) i kursy dzienne zapisywane w istniejacych tabelach prices i fx_rates (zrodlo 'YAHOO_1D'); dostawca koryguje historyczne ceny
--     o pozniejsze splity, wiec wiersze moga byc POPRAWIANE (upsert) - w przeciwienstwie do notowan na zywo (market_quotes), ktore sa tylko do dopisywania;
--   * powiazanie symbolu historii (provider 'YAHOO_1D') weryfikowane wzgledem WSZYSTKICH transakcji z ksiegi (waluta + cena zakupu/sprzedazy vs zamkniecie z dnia);
--   * rola aplikacji moze zapisywac wylacznie te dane referencyjne (ceny, kursy), nigdy ksiege.
-- WPLYW NA WYNIKI: brak (nowe dane referencyjne; istniejace widoki bez zmian).
ALTER TABLE prices ADD CONSTRAINT ck_prices_positive CHECK (close_price > 0);
CREATE INDEX IF NOT EXISTS ix_prices_listing_date ON prices (listing_id, price_date DESC);
CREATE INDEX IF NOT EXISTS ix_fx_rates_pair_date ON fx_rates (base, quote, rate_date DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT INSERT, UPDATE ON prices, fx_rates TO investing_app;
  END IF;
END $$;
