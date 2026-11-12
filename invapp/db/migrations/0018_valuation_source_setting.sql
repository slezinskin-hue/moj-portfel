-- 0018: WYBOR ZRODLA WYCENY W USTAWIENIACH (decyzja uzytkownika 2026-11-12: "opcja w ustawieniach rozne kursy, np. Yahoo Finance, XTB",
--   po zauwazeniu, ze XTB pokazuje ok. 0,2% mniej niz aplikacja). Ustawienia w app_settings (migracja 0017):
--   * price_source = 'YAHOO' (domyslnie, jak dotad: cena rynkowa z API na zywo; raport XTB tylko awaryjnie)
--                  | 'XTB_EXPORT' (pozycje XTB wyceniane DOKLADNIE wartoscia z ostatniego eksportu XTB - ceny i kursy XTB z chwili eksportu;
--                    tylko gdy ilosc w raporcie = ilosc w ksiedze, inaczej (zakup/sprzedaz po eksporcie) cena rynkowa z API).
--                    XTB nie udostepnia publicznego API z cenami (xAPI wylaczone w 2025), wiec "ceny XTB" = ceny z eksportu.
--   * fx_source    = 'YAHOO' (domyslnie, jak dotad: kurs na zywo, a gdy brak - NBP) | 'NBP' (sredni kurs NBP, tabela A; gdy brak - na zywo).
-- WPLYW NA WYNIKI: przy ustawieniach domyslnych BEZ ZMIAN (ta sama definicja co 0013). Po zmianie ustawienia zmienia sie tylko wycena "na zywo"
--   (wartosc papierow, zysk niezrealizowany i pochodne); ksiega, partie, koszty, wykres historyczny bez zmian. Pozycja wyceniona z eksportu
--   ma value_source = 'BROKER' i price_source = 'XTB_EXPORT' (nie jest liczona jako "brak ceny rynkowej").
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
fxl AS (SELECT DISTINCT ON (base, quote) base, quote, rate, provider, quoted_at FROM fx_quotes ORDER BY base, quote, quoted_at DESC),
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
