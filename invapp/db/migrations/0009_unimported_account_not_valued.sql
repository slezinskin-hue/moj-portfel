-- 0009: rachunek z ksiega (tracking_mode = FULL), do ktorego NIGDY nie bylo importu ani operacji, jest "bez wyceny" (a nie wyceniony na 0 zl)
-- Powod: konto Trade Republic bylo zarejestrowane, ale nie zaimportowane; widok liczyl je jako wycenione na 0 zl, wiec majatek calkowity
--        nie byl oznaczany jako niekompletny mimo braku ok. 41 tys. zl (2026-11-11, uzgodnione z uzytkownikiem).
-- Rachunek "ma dane", gdy ma jakakolwiek transakcje, zatwierdzona partie importu (takze pusty plik, np. IKZE) albo migawke pozycji brokera.
-- WPLYW NA WYNIKI: wartosc, kapital i wynik zakresow z zaimportowanymi danymi BEZ ZMIAN. Zmienia sie tylko: liczba rachunkow bez wyceny,
--        flaga kompletnosci oraz wartosc rachunku bez importu (z 0 na brak wyceny / NULL).
CREATE OR REPLACE VIEW v_account_has_ledger_data AS
SELECT a.account_id,
       (EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.account_id)
        OR EXISTS (SELECT 1 FROM import_batches b WHERE b.account_id = a.account_id AND b.status = 'COMMITTED')
        OR EXISTS (SELECT 1 FROM broker_position_snapshots s WHERE s.account_id = a.account_id)) AS has_data
FROM accounts a;

CREATE OR REPLACE VIEW v_account_is_valued AS
SELECT a.account_id,
       ((a.tracking_mode = 'FULL' AND hd.has_data)
        OR EXISTS (SELECT 1 FROM v_latest_valuation_snapshot s WHERE s.account_id = a.account_id)) AS is_valued
FROM accounts a JOIN v_account_has_ledger_data hd ON hd.account_id = a.account_id;

CREATE OR REPLACE VIEW v_account_valuation AS
SELECT a.account_id, a.account_class, a.tracking_mode, a.base_currency AS currency,
       CASE WHEN a.tracking_mode = 'FULL' AND hd.has_data THEN COALESCE(ff.free_funds, 0) END AS free_funds,
       CASE WHEN a.tracking_mode = 'FULL' AND hd.has_data THEN sv.securities_value END AS securities_value,
       CASE WHEN a.tracking_mode = 'FULL' AND hd.has_data THEN COALESCE(ff.free_funds, 0) + sv.securities_value
            WHEN a.tracking_mode = 'FULL' THEN NULL
            ELSE ls.balance END AS value,
       CASE WHEN a.tracking_mode = 'FULL' AND hd.has_data THEN 'LEDGER:' || sv.value_source
            WHEN a.tracking_mode = 'FULL' THEN 'NO_IMPORT'
            WHEN ls.account_id IS NOT NULL THEN 'SNAPSHOT_MANUAL' ELSE 'NONE' END AS value_source,
       (CASE WHEN a.tracking_mode = 'FULL' AND hd.has_data THEN COALESCE(ff.free_funds, 0) + sv.securities_value
             WHEN a.tracking_mode = 'FULL' THEN NULL
             ELSE ls.balance END) IS NOT NULL AS is_valued,
       EXISTS (SELECT 1 FROM v_free_funds f2 WHERE f2.account_id = a.account_id AND f2.cash_currency <> a.base_currency) AS has_foreign_currency_cash
FROM accounts a
JOIN v_account_has_ledger_data hd ON hd.account_id = a.account_id
LEFT JOIN v_free_funds ff ON ff.account_id = a.account_id AND ff.cash_currency = a.base_currency
LEFT JOIN v_account_securities_value sv ON sv.account_id = a.account_id
LEFT JOIN v_latest_valuation_snapshot ls ON ls.account_id = a.account_id AND ls.currency = a.base_currency;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON v_account_has_ledger_data TO investing_app;
  END IF;
END $$;
