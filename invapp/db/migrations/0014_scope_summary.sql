-- 0014: PODSUMOWANIE ZAKRESU BEZ WOLNYCH SRODKOW (decyzja uzytkownika 2026-11-11): wartosc = tylko papiery wartosciowe z cen API;
--   wolne srodki, "wartosc portfela z gotowka" i "wplacony kapital netto" nie wystepuja. Dzialaja dla kazdego zakresu (BROKER:XTB, BROKER:TR, INVESTMENT_ALL)
--   i dla kazdego brokera (partie z derived_lots: XTB wg Position ID, Trade Republic metoda FIFO), nie zaleza od migawek pozycji XTB.
--   Zysk z inwestycji = wartosc papierow - (zakupy - sprzedaze) = niezrealizowany + zrealizowany; dochody dodatkowe (dywidendy, odsetki, nagrody, oplaty)
--   osobno; wynik calkowity = zysk z inwestycji + dochody dodatkowe.
-- WPLYW NA WYNIKI: dodatek (nowy widok); istniejace widoki (v_scope_result*, v_live_position_values, v_scope_result_live) i ksiega BEZ ZMIAN.
CREATE VIEW v_scope_summary AS
WITH sc AS (SELECT s.code AS scope_code, sa.account_id FROM scopes s JOIN scope_accounts sa ON sa.scope_id = s.scope_id
            WHERE s.kind IN ('BROKER', 'INVESTMENT_ALL')),
pos AS (
  SELECT sc.scope_code, COUNT(*) AS positions, SUM(p.value_used) AS securities_value, SUM(p.cost_left) AS cost_left,
         COUNT(*) FILTER (WHERE p.value_source = 'LIVE') AS live_positions,
         COUNT(*) FILTER (WHERE p.value_source = 'SNAPSHOT') AS snapshot_positions,
         COUNT(*) FILTER (WHERE p.value_source = 'MISSING') AS missing_positions,
         MAX(p.price_time_used) AS newest_quote, MIN(p.price_time_used) AS oldest_quote
  FROM sc JOIN v_live_position_values p ON p.account_id = sc.account_id GROUP BY sc.scope_code),
buys AS (SELECT sc.scope_code, SUM(dl.cost_cash_initial) AS purchases FROM sc JOIN derived_lots dl ON dl.account_id = sc.account_id GROUP BY sc.scope_code),
sells AS (
  SELECT sc.scope_code, SUM(c.proceeds_cash) AS sales, SUM(c.realized_pnl) AS realized
  FROM sc JOIN derived_lots dl ON dl.account_id = sc.account_id JOIN derived_lot_closures c ON c.lot_id = dl.lot_id GROUP BY sc.scope_code),
inc AS (
  SELECT sc.scope_code,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'DIVIDEND')        AS dividends_gross,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'TAX_WITHHOLDING') AS withholding_tax,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'INTEREST')        AS interest,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'TAX_INTEREST')    AS interest_tax,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'REWARD')          AS rewards,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'FEE')             AS fees
  FROM sc JOIN transactions t ON t.account_id = sc.account_id AND t.status = 'ACTIVE' AND t.cash_currency = 'PLN' GROUP BY sc.scope_code),
base AS (SELECT DISTINCT scope_code FROM sc)
SELECT b.scope_code, 'PLN'::char(3) AS currency,
       COALESCE(p.securities_value, 0) AS securities_value,
       COALESCE(p.cost_left, 0) AS securities_cost,
       COALESCE(p.securities_value, 0) - COALESCE(p.cost_left, 0) AS unrealized_gain,
       COALESCE(s.realized, 0) AS realized_gain,
       COALESCE(p.securities_value, 0) - COALESCE(p.cost_left, 0) + COALESCE(s.realized, 0) AS investment_gain,
       COALESCE(bu.purchases, 0) AS total_purchases, COALESCE(s.sales, 0) AS total_sales,
       COALESCE(bu.purchases, 0) - COALESCE(s.sales, 0) AS net_invested,
       COALESCE(i.dividends_gross, 0) AS dividends_gross, COALESCE(i.withholding_tax, 0) AS withholding_tax,
       COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) AS dividends_net,
       COALESCE(i.interest, 0) AS interest, COALESCE(i.interest_tax, 0) AS interest_tax, COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0) AS interest_net,
       COALESCE(i.rewards, 0) AS rewards, COALESCE(i.fees, 0) AS fees,
       COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) + COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0) + COALESCE(i.rewards, 0) + COALESCE(i.fees, 0) AS additional_income,
       COALESCE(p.securities_value, 0) - COALESCE(p.cost_left, 0) + COALESCE(s.realized, 0)
         + COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) + COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0) + COALESCE(i.rewards, 0) + COALESCE(i.fees, 0) AS total_result,
       COALESCE(p.positions, 0) AS positions, COALESCE(p.live_positions, 0) AS live_positions, COALESCE(p.snapshot_positions, 0) AS snapshot_positions,
       COALESCE(p.missing_positions, 0) AS missing_positions, p.newest_quote, p.oldest_quote,
       EXISTS (SELECT 1 FROM transactions t JOIN sc x ON x.account_id = t.account_id WHERE x.scope_code = b.scope_code) AS has_data
FROM base b LEFT JOIN pos p ON p.scope_code = b.scope_code LEFT JOIN buys bu ON bu.scope_code = b.scope_code
            LEFT JOIN sells s ON s.scope_code = b.scope_code LEFT JOIN inc i ON i.scope_code = b.scope_code;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON v_scope_summary TO investing_app;
  END IF;
END $$;
