-- 0012: ZAWSZE NAJNOWSZA DOSTEPNA CENA (decyzja uzytkownika 2026-11-11)
--   Dla kazdej pozycji wybierana jest cena o NAJNOWSZYM znaczniku czasu z dwoch zrodel: notowanie rynkowe (dostawca) albo cena z raportu XTB (migawka).
--   Kurs waluty: najnowszy dostepny - na zywo, a gdy go brak: dla ceny z raportu XTB kurs z raportu (wartosc co do grosza jak w raporcie XTB),
--   dla ceny rynkowej kurs NBP.
--   Zmiana wzgledem 0010: (1) cena z raportu XTB w walucie obcej jest przeliczana najnowszym kursem NA ZYWO (zamiast kursu z raportu), jesli taki istnieje -
--   jedna, spojna metoda dla calego portfela; (2) widok pokazuje wybrana cene, jej czas i zrodlo oraz uzyty kurs i jego zrodlo.
--   Bez zadnych notowan i kursow na zywo wartosc nadal jest identyczna co do grosza z raportem XTB.
-- WPLYW NA WYNIKI: wycena z migawki XTB w istniejacych widokach (v_scope_result itd.) BEZ ZMIAN; zmienia sie tylko wycena "na zywo" i tylko dla pozycji
--   w walucie obcej wycenianych cena z raportu XTB przy dostepnym kursie na zywo (dzis: zadna).
DROP VIEW v_scope_result_live;
DROP VIEW v_live_position_values;

CREATE VIEW v_live_position_values AS
WITH pos AS (
  SELECT dl.account_id, dl.listing_id, SUM(dl.qty_remaining) AS qty,
         SUM(dl.cost_cash_initial) - COALESCE(SUM(cl.closed), 0) AS cost_left
  FROM derived_lots dl
  LEFT JOIN (SELECT lot_id, SUM(cost_cash) AS closed FROM derived_lot_closures GROUP BY lot_id) cl ON cl.lot_id = dl.lot_id
  GROUP BY dl.account_id, dl.listing_id
  HAVING SUM(dl.qty_remaining) > 0),
latest AS (SELECT account_id, MAX(as_of) AS as_of FROM broker_position_snapshots WHERE row_level = 'INSTRUMENT' GROUP BY account_id),
snap AS (
  SELECT s.account_id, s.listing_id, s.value_cash, s.as_of,
         (SELECT MAX(p.current_price) FROM broker_position_snapshots p WHERE p.account_id = s.account_id AND p.listing_id = s.listing_id
            AND p.row_level = 'POSITION' AND p.as_of = s.as_of) AS snap_price,
         (SELECT AVG(p.implied_fx_rate) FROM broker_position_snapshots p WHERE p.account_id = s.account_id AND p.listing_id = s.listing_id
            AND p.row_level = 'POSITION' AND p.as_of = s.as_of) AS snap_fx
  FROM broker_position_snapshots s JOIN latest m ON m.account_id = s.account_id AND m.as_of = s.as_of
  WHERE s.row_level = 'INSTRUMENT'),
fxl AS (SELECT DISTINCT ON (base, quote) base, quote, rate, provider, quoted_at FROM fx_quotes ORDER BY base, quote, quoted_at DESC),
v AS (
  SELECT pos.account_id, pos.listing_id, pos.qty, pos.cost_left, a.base_currency,
         q.price, q.currency AS quote_currency, q.price_unit, q.quoted_at, q.provider, q.market_state,
         fx.rate AS fx_rate, fx.source AS fx_source, fx.as_of AS fx_as_of,
         CASE WHEN q.price IS NULL THEN NULL
              WHEN q.currency = a.base_currency THEN pos.qty * q.price / CASE WHEN q.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END
              WHEN fx.rate IS NOT NULL THEN pos.qty * q.price / CASE WHEN q.price_unit = 'MINOR_1_100' THEN 100 ELSE 1 END * fx.rate
         END AS market_value,
         snap.value_cash AS snapshot_value, snap.as_of AS snapshot_as_of,
         snap.snap_price, snap.snap_fx, l.trading_currency AS listing_currency, fxl.rate AS live_fx, fxl.provider AS live_fx_provider, fxl.quoted_at AS live_fx_at
  FROM pos
  JOIN accounts a ON a.account_id = pos.account_id
  JOIN listings l ON l.listing_id = pos.listing_id
  LEFT JOIN v_latest_quote q ON q.listing_id = pos.listing_id
  LEFT JOIN v_fx_for_valuation fx ON fx.base = q.currency AND fx.quote = a.base_currency
  LEFT JOIN snap ON snap.account_id = pos.account_id AND snap.listing_id = pos.listing_id
  LEFT JOIN fxl ON fxl.base = l.trading_currency AND fxl.quote = a.base_currency),
w AS (
  SELECT v.*,
         -- cena o najnowszym znaczniku czasu: rynkowa albo z raportu XTB
         (v.market_value IS NOT NULL AND (v.snapshot_as_of IS NULL OR v.quoted_at > v.snapshot_as_of)) AS use_market
  FROM v)
SELECT w.account_id, w.listing_id, w.qty, w.cost_left, w.base_currency,
       w.price, w.quote_currency, w.price_unit, w.quoted_at, w.provider, w.market_state,
       w.fx_rate, w.fx_source, w.fx_as_of, w.market_value, w.snapshot_value, w.snapshot_as_of,
       CASE WHEN w.use_market THEN 'LIVE' WHEN w.snapshot_value IS NOT NULL THEN 'SNAPSHOT' WHEN w.market_value IS NOT NULL THEN 'LIVE' ELSE 'MISSING' END AS value_source,
       CASE WHEN w.use_market THEN w.market_value
            WHEN w.snapshot_value IS NOT NULL THEN
              CASE WHEN w.listing_currency IS NOT NULL AND w.listing_currency <> w.base_currency AND w.live_fx IS NOT NULL AND w.snap_price IS NOT NULL
                   THEN w.qty * w.snap_price * w.live_fx          -- cena z raportu XTB, najnowszy kurs na zywo (jedna spojna metoda)
                   ELSE w.snapshot_value END                      -- bez kursu na zywo (albo waluta = waluta konta): dokladnie wartosc z raportu XTB
            ELSE w.market_value END AS value_used,
       -- wybrana cena, jej czas, zrodlo i uzyty kurs
       CASE WHEN w.use_market OR (w.snapshot_value IS NULL AND w.market_value IS NOT NULL) THEN w.price ELSE w.snap_price END AS price_used,
       CASE WHEN w.use_market OR (w.snapshot_value IS NULL AND w.market_value IS NOT NULL) THEN w.quoted_at ELSE w.snapshot_as_of END AS price_time_used,
       CASE WHEN w.use_market OR (w.snapshot_value IS NULL AND w.market_value IS NOT NULL) THEN 'MARKET'
            WHEN w.snapshot_value IS NOT NULL THEN 'XTB_REPORT' ELSE 'NONE' END AS price_source,
       CASE WHEN w.use_market THEN w.fx_rate
            WHEN w.snapshot_value IS NOT NULL AND w.listing_currency IS NOT NULL AND w.listing_currency <> w.base_currency AND w.live_fx IS NOT NULL THEN w.live_fx
            WHEN w.snapshot_value IS NOT NULL THEN w.snap_fx END AS fx_used
FROM w;

CREATE VIEW v_scope_result_live AS
WITH lv AS (
  SELECT s.code AS scope_code, SUM(p.value_used) AS securities_value_live, SUM(p.cost_left) AS cost_left,
         COUNT(*) AS positions, COUNT(*) FILTER (WHERE p.value_source = 'LIVE') AS live_positions,
         COUNT(*) FILTER (WHERE p.value_source = 'SNAPSHOT') AS snapshot_positions, COUNT(*) FILTER (WHERE p.value_source = 'MISSING') AS missing_positions,
         MAX(p.price_time_used) AS newest_quote, MIN(p.price_time_used) AS oldest_quote
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
    GRANT SELECT ON v_live_position_values, v_scope_result_live TO investing_app;
  END IF;
END $$;
