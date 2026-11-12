-- 0007: rozbicie wyniku inwestycyjnego zakresu na ZYSK Z INWESTYCJI (zmiana wartosci aktywow) oraz DOCHODY DODATKOWE (dywidendy, odsetki, oplaty)
-- Powod: uzytkownik chce, aby glowny widok pokazywal zysk z inwestycji (wzrost wartosci aktywow), a dywidendy i inne wplywy byly zapisane
--        i widoczne obok (2026-11-11).
-- WPLYW NA WYNIKI: BRAK zmiany wartosci, kapitalu ani wyniku calkowitego (v_scope_result bez zmian). Widok tylko DZIELI wynik na skladniki;
--        suma skladnikow + pozycja 'residual' (reszta niewyjasniona) = wynik calkowity; dla danych XTB reszta wynosi 0,00.
--   zysk z inwestycji   = niezrealizowany (wartosc papierow - koszt z gotowki pozostalych partii) + zrealizowany (sprzedaze: przychod - koszt z gotowki)
--   dochody dodatkowe   = dywidendy brutto, podatek u zrodla, odsetki, podatek od odsetek, nagrody, oplaty (wszystko z ksiegi, ze znakiem)
--   PPK                 = orientacyjna zmiana PPK wzgledem wplat (nie jest zyskiem z inwestycji precyzyjnym; osobna pozycja)
-- Koszt = gotowka z ksiegi (D-KOSZT-1); to NIE jest koszt podatkowy.
CREATE VIEW v_scope_result_breakdown AS
WITH sc AS (SELECT s.code AS scope_code, sa.account_id FROM scopes s JOIN scope_accounts sa ON sa.scope_id = s.scope_id),
cost_left AS (
  SELECT sc.scope_code, SUM(dl.cost_cash_initial) - COALESCE(SUM(cl.closed), 0) AS cost_left
  FROM sc JOIN derived_lots dl ON dl.account_id = sc.account_id
  LEFT JOIN (SELECT lot_id, SUM(cost_cash) AS closed FROM derived_lot_closures GROUP BY lot_id) cl ON cl.lot_id = dl.lot_id
  GROUP BY sc.scope_code),
realized AS (
  SELECT sc.scope_code, SUM(c.realized_pnl) AS realized
  FROM sc JOIN derived_lots dl ON dl.account_id = sc.account_id JOIN derived_lot_closures c ON c.lot_id = dl.lot_id
  GROUP BY sc.scope_code),
inc AS (
  SELECT sc.scope_code, t.cash_currency AS currency,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'DIVIDEND')        AS dividends_gross,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'TAX_WITHHOLDING') AS withholding_tax,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'INTEREST')        AS interest,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'TAX_INTEREST')    AS interest_tax,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'REWARD')          AS rewards,
         SUM(t.cash_amount) FILTER (WHERE t.txn_type = 'FEE')             AS fees
  FROM sc JOIN transactions t ON t.account_id = sc.account_id AND t.status = 'ACTIVE'
  GROUP BY sc.scope_code, t.cash_currency)
SELECT r.scope_code, r.currency, r.portfolio_value, r.net_contributions, r.investment_result,
       r.securities_value,
       COALESCE(cl.cost_left, 0)                            AS securities_cost,                 -- koszt z gotowki pozostalych pozycji
       r.securities_value - COALESCE(cl.cost_left, 0)                    AS unrealized_gain,
       COALESCE(re.realized, 0)                             AS realized_gain,
       (r.securities_value - COALESCE(cl.cost_left, 0)) + COALESCE(re.realized, 0) AS investment_gain,        -- ZYSK Z INWESTYCJI
       COALESCE(i.dividends_gross, 0)  AS dividends_gross,
       COALESCE(i.withholding_tax, 0)  AS withholding_tax,
       COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) AS dividends_net,
       COALESCE(i.interest, 0)         AS interest,
       COALESCE(i.interest_tax, 0)     AS interest_tax,
       COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0) AS interest_net,
       COALESCE(i.rewards, 0)          AS rewards,
       COALESCE(i.fees, 0)             AS fees,
       COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) + COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0)
         + COALESCE(i.rewards, 0) + COALESCE(i.fees, 0)     AS additional_income,                -- DOCHODY DODATKOWE (netto, po pobranych podatkach)
       r.retirement_indicative_change,
       r.investment_result - ( (r.securities_value - COALESCE(cl.cost_left, 0)) + COALESCE(re.realized, 0)
         + COALESCE(i.dividends_gross, 0) + COALESCE(i.withholding_tax, 0) + COALESCE(i.interest, 0) + COALESCE(i.interest_tax, 0)
         + COALESCE(i.rewards, 0) + COALESCE(i.fees, 0) + COALESCE(r.retirement_indicative_change, 0) ) AS residual   -- reszta niewyjasniona (powinna byc 0,00)
FROM v_scope_result r
LEFT JOIN cost_left cl ON cl.scope_code = r.scope_code
LEFT JOIN realized re ON re.scope_code = r.scope_code
LEFT JOIN inc i ON i.scope_code = r.scope_code AND i.currency = r.currency;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON v_scope_result_breakdown TO investing_app;
  END IF;
END $$;
