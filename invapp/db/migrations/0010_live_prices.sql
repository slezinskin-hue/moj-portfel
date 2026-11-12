-- 0010: ceny na zywo (etap C, dokumenty 19 i 20). ADDYTYWNIE: dotychczasowa wycena (v_account_valuation, v_scope_result, v_scope_result_breakdown)
--        z migawki XTB zostaje BEZ ZMIAN. Nowe widoki *_live licza wartosc z najnowszych cen rynkowych, gdy sa nowsze niz migawka XTB.
--   * listing_provider_symbols: powiazanie notowania z symbolem u dostawcy cen (np. Yahoo), ZWERYFIKOWANE: waluta dostawcy = waluta notowania w bazie
--     oraz cena w rozsadnym przedziale wokol ceny z migawki XTB; tylko VERIFIED jest uzywane do wyceny. To jest "linia referencyjna cen",
--     NIE twierdzenie, na jakiej gieldzie XTB wykonuje zlecenia (dlatego listings.exchange_mic nie jest zmieniane).
--   * market_quotes, fx_quotes: notowania i kursy (tylko dopisywanie); fx_rates: kursy NBP (tabela A).
--   * wartosc pozycji na zywo = ilosc (partie po korektach CONFIRMED) x cena [/100 dla pensow] x kurs (na zywo; brak -> NBP);
--     jesli cena jest starsza niz migawka XTB albo jej brak -> wartosc z migawki XTB (zrodlo jawnie oznaczone).
-- WPLYW NA WYNIKI: brak zmian w istniejacych widokach; nowe widoki pokazuja dodatkowo wartosc i wynik "na zywo".

CREATE TABLE listing_provider_symbols (
  mapping_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id         BIGINT NOT NULL REFERENCES listings,
  provider           TEXT NOT NULL CHECK (provider ~ '^[A-Z0-9_]+$'),
  provider_symbol    TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('VERIFIED','PENDING','REJECTED')),
  quote_currency     CHAR(3),
  price_unit         TEXT CHECK (price_unit IN ('MAJOR','MINOR_1_100')),
  reference_exchange TEXT,                                  -- gielda linii referencyjnej wg dostawcy (np. 'Warsaw', 'XETRA')
  checked_at         TIMESTAMPTZ NOT NULL,
  evidence           JSONB NOT NULL,                        -- waluty, cena dostawcy vs cena XTB, powod odrzucenia
  UNIQUE (listing_id, provider),
  CONSTRAINT ck_lps_verified CHECK (status <> 'VERIFIED' OR (quote_currency IS NOT NULL AND price_unit IS NOT NULL))
);

CREATE TABLE market_quotes (
  quote_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id       BIGINT NOT NULL REFERENCES listings,
  provider         TEXT NOT NULL,
  provider_symbol  TEXT NOT NULL,
  price            NUMERIC(24,8) NOT NULL CHECK (price > 0),
  currency         CHAR(3) NOT NULL,
  price_unit       TEXT NOT NULL CHECK (price_unit IN ('MAJOR','MINOR_1_100')),
  quoted_at        TIMESTAMPTZ NOT NULL,                    -- czas notowania wg dostawcy
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  market_state     TEXT,
  UNIQUE (listing_id, provider, quoted_at)
);
CREATE INDEX ix_quotes_latest ON market_quotes (listing_id, quoted_at DESC);

CREATE TABLE fx_quotes (
  base        CHAR(3) NOT NULL,
  quote       CHAR(3) NOT NULL,
  rate        NUMERIC(24,10) NOT NULL CHECK (rate > 0),
  provider    TEXT NOT NULL,
  quoted_at   TIMESTAMPTZ NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (base, quote, provider, quoted_at)
);

CREATE TRIGGER trg_quotes_append_only BEFORE UPDATE OR DELETE ON market_quotes FOR EACH ROW EXECUTE FUNCTION fn_append_only();
CREATE TRIGGER trg_fx_quotes_append_only BEFORE UPDATE OR DELETE ON fx_quotes FOR EACH ROW EXECUTE FUNCTION fn_append_only();

-- najnowsze notowanie z ZWERYFIKOWANEGO powiazania
CREATE VIEW v_latest_quote AS
SELECT DISTINCT ON (q.listing_id) q.listing_id, q.provider, q.provider_symbol, q.price, q.currency, q.price_unit, q.quoted_at, q.fetched_at, q.market_state
FROM market_quotes q
JOIN listing_provider_symbols m ON m.listing_id = q.listing_id AND m.provider = q.provider AND m.status = 'VERIFIED'
ORDER BY q.listing_id, q.quoted_at DESC;

-- kurs do wyceny: najnowszy kurs na zywo; gdy brak -> najnowszy NBP (tabela A); oba widoczne
CREATE VIEW v_fx_for_valuation AS
WITH live AS (SELECT DISTINCT ON (base, quote) base, quote, rate, provider AS source, quoted_at AS as_of FROM fx_quotes ORDER BY base, quote, quoted_at DESC),
     nbp  AS (SELECT DISTINCT ON (base, quote) base, quote, rate, source, rate_date AS rate_date FROM fx_rates WHERE source = 'NBP_A' ORDER BY base, quote, rate_date DESC)
SELECT COALESCE(l.base, n.base) AS base, COALESCE(l.quote, n.quote) AS quote,
       COALESCE(l.rate, n.rate) AS rate,
       CASE WHEN l.base IS NOT NULL THEN l.source ELSE n.source END AS source,
       COALESCE(l.as_of, n.rate_date::timestamptz) AS as_of,
       n.rate AS nbp_rate, n.rate_date AS nbp_date
FROM live l FULL JOIN nbp n ON n.base = l.base AND n.quote = l.quote;

CREATE VIEW v_live_position_values AS
WITH pos AS (
  SELECT dl.account_id, dl.listing_id, SUM(dl.qty_remaining) AS qty,
         SUM(dl.cost_cash_initial) - COALESCE(SUM(cl.closed), 0) AS cost_left
  FROM derived_lots dl
  LEFT JOIN (SELECT lot_id, SUM(cost_cash) AS closed FROM derived_lot_closures GROUP BY lot_id) cl ON cl.lot_id = dl.lot_id
  GROUP BY dl.account_id, dl.listing_id
  HAVING SUM(dl.qty_remaining) > 0),
snap AS (
  SELECT s.account_id, s.listing_id, s.value_cash, s.as_of
  FROM broker_position_snapshots s
  JOIN (SELECT account_id, MAX(as_of) AS as_of FROM broker_position_snapshots WHERE row_level = 'INSTRUMENT' GROUP BY account_id) m
       ON m.account_id = s.account_id AND m.as_of = s.as_of
  WHERE s.row_level = 'INSTRUMENT'),
v AS (
  SELECT pos.account_id, pos.listing_id, pos.qty, pos.cost_left, a.base_currency,
         q.price, q.currency AS quote_currency, q.price_unit, q.quoted_at, q.provider, q.market_state,
         fx.rate AS fx_rate, fx.source AS fx_source, fx.as_of AS fx_as_of,
         CASE WHEN q.price IS NULL THEN NULL
              WHEN q.currency = a.base_currency THEN pos.qty * q.price / CASE WHEN q.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END
              WHEN fx.rate IS NOT NULL THEN pos.qty * q.price / CASE WHEN q.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END * fx.rate
         END AS market_value,
         snap.value_cash AS snapshot_value, snap.as_of AS snapshot_as_of
  FROM pos
  JOIN accounts a ON a.account_id = pos.account_id
  LEFT JOIN v_latest_quote q ON q.listing_id = pos.listing_id
  LEFT JOIN v_fx_for_valuation fx ON fx.base = q.currency AND fx.quote = a.base_currency
  LEFT JOIN snap ON snap.account_id = pos.account_id AND snap.listing_id = pos.listing_id)
SELECT v.*,
       CASE WHEN v.market_value IS NOT NULL AND (v.snapshot_as_of IS NULL OR v.quoted_at > v.snapshot_as_of) THEN 'LIVE'
            WHEN v.snapshot_value IS NOT NULL THEN 'SNAPSHOT'
            WHEN v.market_value IS NOT NULL THEN 'LIVE'
            ELSE 'MISSING' END AS value_source,
       CASE WHEN v.market_value IS NOT NULL AND (v.snapshot_as_of IS NULL OR v.quoted_at > v.snapshot_as_of) THEN v.market_value
            WHEN v.snapshot_value IS NOT NULL THEN v.snapshot_value
            ELSE v.market_value END AS value_used
FROM v;

-- wynik zakresu "na zywo": te same skladniki co v_scope_result_breakdown, ale wartosc papierow z v_live_position_values
CREATE VIEW v_scope_result_live AS
WITH lv AS (
  SELECT s.code AS scope_code, SUM(p.value_used) AS securities_value_live, SUM(p.cost_left) AS cost_left,
         COUNT(*) AS positions, COUNT(*) FILTER (WHERE p.value_source = 'LIVE') AS live_positions,
         COUNT(*) FILTER (WHERE p.value_source = 'SNAPSHOT') AS snapshot_positions, COUNT(*) FILTER (WHERE p.value_source = 'MISSING') AS missing_positions,
         MAX(p.quoted_at) FILTER (WHERE p.value_source = 'LIVE') AS newest_quote, MIN(p.quoted_at) FILTER (WHERE p.value_source = 'LIVE') AS oldest_quote
  FROM scopes s JOIN scope_accounts sa ON sa.scope_id = s.scope_id JOIN v_live_position_values p ON p.account_id = sa.account_id
  GROUP BY s.code)
SELECT b.scope_code, b.currency, b.net_contributions,
       b.portfolio_value - b.securities_value AS free_funds,
       COALESCE(lv.securities_value_live, 0) AS securities_value_live,
       (b.portfolio_value - b.securities_value) + COALESCE(lv.securities_value_live, 0) AS portfolio_value_live,
       (b.portfolio_value - b.securities_value) + COALESCE(lv.securities_value_live, 0) - b.net_contributions AS investment_result_live,
       COALESCE(lv.securities_value_live, 0) - COALESCE(lv.cost_left, 0) AS unrealized_gain_live,
       b.realized_gain,
       COALESCE(lv.securities_value_live, 0) - COALESCE(lv.cost_left, 0) + b.realized_gain AS investment_gain_live,
       b.additional_income,
       (b.portfolio_value - b.securities_value) + COALESCE(lv.securities_value_live, 0) - b.net_contributions
         - (COALESCE(lv.securities_value_live, 0) - COALESCE(lv.cost_left, 0) + b.realized_gain + b.additional_income
            + COALESCE(b.retirement_indicative_change, 0)) AS residual_live,
       b.portfolio_value AS portfolio_value_broker, b.investment_result AS investment_result_broker,
       COALESCE(lv.positions, 0) AS positions, COALESCE(lv.live_positions, 0) AS live_positions,
       COALESCE(lv.snapshot_positions, 0) AS snapshot_positions, COALESCE(lv.missing_positions, 0) AS missing_positions,
       lv.newest_quote, lv.oldest_quote
FROM v_scope_result_breakdown b LEFT JOIN lv ON lv.scope_code = b.scope_code
WHERE b.securities_value IS NOT NULL;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON listing_provider_symbols, market_quotes, fx_quotes, v_latest_quote, v_fx_for_valuation, v_live_position_values, v_scope_result_live TO investing_app;
    GRANT INSERT ON market_quotes, fx_quotes, fx_rates TO investing_app;          -- odswiezanie cen z aplikacji (tylko dopisywanie danych referencyjnych)
  END IF;
END $$;
