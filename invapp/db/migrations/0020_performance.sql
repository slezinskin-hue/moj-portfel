-- 0020: WYDAJNOSC (decyzja uzytkownika 2026-11-13: "optymalizacje, zeby dzialalo szybciej, takze przy dlugich eksportach").
--   1. Najnowsze notowanie / kurs bez przeszukiwania calej historii: notowania na zywo (market_quotes) i kursy (fx_quotes) sa dopisywane co minute
--      otwartej aplikacji i nigdy nie sa kasowane (audyt), wiec widoki z DISTINCT ON po calej tabeli zwalnialyby z kazdym miesiacem uzytkowania.
--      Nowe tabele market_quote_latest i fx_quote_latest trzymaja WSKAZANIE na najnowszy wiersz dla (notowanie|para walut, dostawca);
--      utrzymuje je baza (wyzwalacz po dopisaniu notowania), historia notowan zostaje bez zmian.
--   2. Indeksy na kolumnach uzywanych w zapytaniach po instrumencie, koncie, pliku i pozycji (klucze obce nie maja indeksow automatycznie).
-- WPLYW NA WYNIKI: brak - widoki zwracaja te same wiersze co dotad (ten sam wybor "najnowszego" po czasie notowania); zmienia sie tylko sposob ich wyszukania.

-- 1a. najnowsze notowanie rynkowe (wskazanie na wiersz market_quotes)
CREATE TABLE market_quote_latest (
  listing_id  BIGINT NOT NULL,
  provider    TEXT NOT NULL,
  quote_id    BIGINT NOT NULL REFERENCES market_quotes,
  quoted_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (listing_id, provider)
);
INSERT INTO market_quote_latest(listing_id, provider, quote_id, quoted_at)
SELECT DISTINCT ON (listing_id, provider) listing_id, provider, quote_id, quoted_at FROM market_quotes ORDER BY listing_id, provider, quoted_at DESC, quote_id DESC;

CREATE FUNCTION fn_market_quote_latest() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO market_quote_latest(listing_id, provider, quote_id, quoted_at) VALUES (NEW.listing_id, NEW.provider, NEW.quote_id, NEW.quoted_at)
  ON CONFLICT (listing_id, provider) DO UPDATE SET quote_id = EXCLUDED.quote_id, quoted_at = EXCLUDED.quoted_at
    WHERE EXCLUDED.quoted_at > market_quote_latest.quoted_at;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_market_quote_latest AFTER INSERT ON market_quotes FOR EACH ROW EXECUTE FUNCTION fn_market_quote_latest();

-- 1b. najnowszy kurs na zywo
CREATE TABLE fx_quote_latest (
  base       CHAR(3) NOT NULL,
  quote      CHAR(3) NOT NULL,
  provider   TEXT NOT NULL,
  rate       NUMERIC(24,10) NOT NULL CHECK (rate > 0),
  quoted_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (base, quote, provider)
);
INSERT INTO fx_quote_latest(base, quote, provider, rate, quoted_at)
SELECT DISTINCT ON (base, quote, provider) base, quote, provider, rate, quoted_at FROM fx_quotes ORDER BY base, quote, provider, quoted_at DESC;

CREATE FUNCTION fn_fx_quote_latest() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO fx_quote_latest(base, quote, provider, rate, quoted_at) VALUES (NEW.base, NEW.quote, NEW.provider, NEW.rate, NEW.quoted_at)
  ON CONFLICT (base, quote, provider) DO UPDATE SET rate = EXCLUDED.rate, quoted_at = EXCLUDED.quoted_at
    WHERE EXCLUDED.quoted_at > fx_quote_latest.quoted_at;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_fx_quote_latest AFTER INSERT ON fx_quotes FOR EACH ROW EXECUTE FUNCTION fn_fx_quote_latest();

-- 1c. widoki: te same kolumny i wiersze, odczyt z tabel "najnowszych"
CREATE OR REPLACE VIEW v_latest_quote AS
SELECT DISTINCT ON (q.listing_id) q.listing_id, q.provider, q.provider_symbol, q.price, q.currency, q.price_unit, q.quoted_at, q.fetched_at, q.market_state
FROM market_quote_latest lq
JOIN market_quotes q ON q.quote_id = lq.quote_id
JOIN listing_provider_symbols m ON m.listing_id = q.listing_id AND m.provider = q.provider AND m.status = 'VERIFIED'
ORDER BY q.listing_id, q.quoted_at DESC;

CREATE OR REPLACE VIEW v_fx_for_valuation AS
WITH live AS (SELECT DISTINCT ON (base, quote) base, quote, rate, provider AS source, quoted_at AS as_of FROM fx_quote_latest ORDER BY base, quote, quoted_at DESC),
     nbp  AS (SELECT DISTINCT ON (base, quote) base, quote, rate, source, rate_date AS rate_date FROM fx_rates WHERE source = 'NBP_A' ORDER BY base, quote, rate_date DESC)
SELECT COALESCE(l.base, n.base) AS base, COALESCE(l.quote, n.quote) AS quote,
       COALESCE(l.rate, n.rate) AS rate,
       CASE WHEN l.base IS NOT NULL THEN l.source ELSE n.source END AS source,
       COALESCE(l.as_of, n.rate_date::timestamptz) AS as_of,
       n.rate AS nbp_rate, n.rate_date AS nbp_date
FROM live l FULL JOIN nbp n ON n.base = l.base AND n.quote = l.quote;

-- 1d. wycena na zywo: kurs na zywo z fx_quote_latest (reszta definicji jak w 0018)
CREATE OR REPLACE VIEW v_live_position_values AS
WITH cfg AS (
  SELECT COALESCE((SELECT value #>> '{}' FROM app_settings WHERE key = 'price_source'), 'YAHOO') AS price_source,
         COALESCE((SELECT value #>> '{}' FROM app_settings WHERE key = 'fx_source'), 'YAHOO') AS fx_source),
pos AS (
  SELECT dl.account_id, dl.listing_id, SUM(dl.qty_remaining) AS qty,
         SUM(dl.cost_cash_initial) - COALESCE(SUM(cl.closed), 0) AS cost_left
  FROM derived_lots dl
  LEFT JOIN (SELECT lot_id, SUM(cost_cash) AS closed FROM derived_lot_closures GROUP BY lot_id) cl ON cl.lot_id = dl.lot_id
  GROUP BY dl.account_id, dl.listing_id
  HAVING SUM(dl.qty_remaining) > 0),
latest AS (SELECT account_id, MAX(as_of) AS as_of FROM broker_position_snapshots WHERE row_level = 'INSTRUMENT' GROUP BY account_id),
snap AS (
  SELECT s.account_id, s.listing_id, s.value_cash, s.as_of, s.volume AS snap_volume,
         (SELECT MAX(p.current_price) FROM broker_position_snapshots p WHERE p.account_id = s.account_id AND p.listing_id = s.listing_id
            AND p.row_level = 'POSITION' AND p.as_of = s.as_of) AS snap_price,
         (SELECT AVG(p.implied_fx_rate) FROM broker_position_snapshots p WHERE p.account_id = s.account_id AND p.listing_id = s.listing_id
            AND p.row_level = 'POSITION' AND p.as_of = s.as_of) AS snap_fx
  FROM broker_position_snapshots s JOIN latest m ON m.account_id = s.account_id AND m.as_of = s.as_of
  WHERE s.row_level = 'INSTRUMENT'),
fxl AS (SELECT DISTINCT ON (base, quote) base, quote, rate, provider, quoted_at FROM fx_quote_latest ORDER BY base, quote, quoted_at DESC),
v AS (
  SELECT pos.account_id, pos.listing_id, pos.qty, pos.cost_left, a.base_currency,
         q.price, q.currency AS quote_currency, q.price_unit, q.quoted_at, q.provider, q.market_state,
         CASE WHEN cfg.fx_source = 'NBP' AND fx.nbp_rate IS NOT NULL THEN fx.nbp_rate ELSE fx.rate END AS fx_rate,
         CASE WHEN cfg.fx_source = 'NBP' AND fx.nbp_rate IS NOT NULL THEN 'NBP_A' ELSE fx.source END AS fx_source,
         CASE WHEN cfg.fx_source = 'NBP' AND fx.nbp_rate IS NOT NULL THEN fx.nbp_date::timestamptz ELSE fx.as_of END AS fx_as_of,
         snap.value_cash AS snapshot_value, snap.as_of AS snapshot_as_of, snap.snap_volume,
         snap.snap_price, snap.snap_fx, l.trading_currency AS listing_currency,
         CASE WHEN cfg.fx_source = 'NBP' AND fxn.nbp_rate IS NOT NULL THEN fxn.nbp_rate ELSE fxl.rate END AS live_fx,
         cfg.price_source AS cfg_price_source
  FROM pos
  CROSS JOIN cfg
  JOIN accounts a ON a.account_id = pos.account_id
  JOIN listings l ON l.listing_id = pos.listing_id
  LEFT JOIN v_latest_quote q ON q.listing_id = pos.listing_id
  LEFT JOIN v_fx_for_valuation fx ON fx.base = q.currency AND fx.quote = a.base_currency
  LEFT JOIN snap ON snap.account_id = pos.account_id AND snap.listing_id = pos.listing_id
  LEFT JOIN fxl ON fxl.base = l.trading_currency AND fxl.quote = a.base_currency
  LEFT JOIN v_fx_for_valuation fxn ON fxn.base = l.trading_currency AND fxn.quote = a.base_currency),
mv AS (
  SELECT v.*,
         CASE WHEN v.price IS NULL THEN NULL
              WHEN v.quote_currency = v.base_currency THEN v.qty * v.price / CASE WHEN v.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END
              WHEN v.fx_rate IS NOT NULL THEN v.qty * v.price / CASE WHEN v.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END * v.fx_rate
         END AS market_value
  FROM v),
w AS (
  SELECT mv.*,
         -- ustawienie "ceny z eksportu XTB": wartosc z raportu, jesli ilosc w raporcie = ilosc w ksiedze
         (mv.cfg_price_source = 'XTB_EXPORT' AND mv.snapshot_value IS NOT NULL AND mv.snap_volume IS NOT NULL
            AND abs(mv.snap_volume - mv.qty) <= 0.00000001) AS use_broker,
         (mv.market_value IS NOT NULL) AS has_market
  FROM mv),
x AS (
  SELECT w.*, (NOT w.use_broker AND w.has_market) AS use_market FROM w)
SELECT x.account_id, x.listing_id, x.qty, x.cost_left, x.base_currency,
       x.price, x.quote_currency, x.price_unit, x.quoted_at, x.provider, x.market_state,
       x.fx_rate, x.fx_source, x.fx_as_of, x.market_value, x.snapshot_value, x.snapshot_as_of,
       CASE WHEN x.use_broker THEN 'BROKER' WHEN x.use_market THEN 'LIVE' WHEN x.snapshot_value IS NOT NULL THEN 'SNAPSHOT' ELSE 'MISSING' END AS value_source,
       CASE WHEN x.use_broker THEN x.snapshot_value
            WHEN x.use_market THEN x.market_value
            WHEN x.snapshot_value IS NOT NULL THEN
              CASE WHEN x.listing_currency IS NOT NULL AND x.listing_currency <> x.base_currency AND x.live_fx IS NOT NULL AND x.snap_price IS NOT NULL
                   THEN x.qty * x.snap_price * x.live_fx          -- awaryjnie: cena z raportu XTB, najnowszy kurs (wg ustawienia)
                   ELSE x.snapshot_value END
            ELSE NULL END AS value_used,
       CASE WHEN x.use_market THEN x.price ELSE x.snap_price END AS price_used,
       CASE WHEN x.use_market THEN x.quoted_at ELSE x.snapshot_as_of END AS price_time_used,
       CASE WHEN x.use_broker THEN 'XTB_EXPORT' WHEN x.use_market THEN 'MARKET' WHEN x.snapshot_value IS NOT NULL THEN 'XTB_REPORT' ELSE 'NONE' END AS price_source,
       CASE WHEN x.use_broker THEN x.snap_fx
            WHEN x.use_market THEN x.fx_rate
            WHEN x.snapshot_value IS NOT NULL AND x.listing_currency IS NOT NULL AND x.listing_currency <> x.base_currency AND x.live_fx IS NOT NULL THEN x.live_fx
            WHEN x.snapshot_value IS NOT NULL THEN x.snap_fx END AS fx_used
FROM x;

-- 2. indeksy (zapytania po instrumencie, koncie, pliku, pozycji; kursy wg czasu)
CREATE INDEX IF NOT EXISTS ix_txn_listing ON transactions (listing_id) WHERE listing_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_txn_related ON transactions (related_txn_id) WHERE related_txn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_txn_first_batch ON transactions (first_batch_id);
CREATE INDEX IF NOT EXISTS ix_txn_position_ref ON transactions (account_id, broker_position_ref) WHERE broker_position_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_raw_rows_txn ON raw_rows (txn_id) WHERE txn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_raw_rows_key ON raw_rows (natural_key) WHERE natural_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_lots_listing ON derived_lots (listing_id);
CREATE INDEX IF NOT EXISTS ix_lots_account ON derived_lots (account_id);
CREATE INDEX IF NOT EXISTS ix_lots_open_txn ON derived_lots (open_txn_id);
CREATE INDEX IF NOT EXISTS ix_closures_lot ON derived_lot_closures (lot_id);
CREATE INDEX IF NOT EXISTS ix_bcp_listing ON broker_closed_positions (listing_id);
CREATE INDEX IF NOT EXISTS ix_snap_listing ON broker_position_snapshots (listing_id);
CREATE INDEX IF NOT EXISTS ix_ca_listing ON corporate_actions (listing_id);
CREATE INDEX IF NOT EXISTS ix_dq_entity ON data_quality_issues (entity_ref);
CREATE INDEX IF NOT EXISTS ix_dq_batch ON data_quality_issues (batch_id);
CREATE INDEX IF NOT EXISTS ix_fxq_latest ON fx_quotes (base, quote, quoted_at DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON market_quote_latest, fx_quote_latest TO investing_app;
  END IF;
END $$;
