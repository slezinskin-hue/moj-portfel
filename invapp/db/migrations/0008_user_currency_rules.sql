-- 0008: jawna regula UZYTKOWNIKA dla waluty notowania wg sufiksu symbolu brokera + kontrolowane uzupelnienie ceny w ksiedze
-- Powod: uzytkownik potwierdzil (2026-11-11), ze instrumenty z sufiksem .PL (GPW) sa notowane w PLN. To NIE jest zgadywanie z sufiksu przez system
--        (decyzja D-INSTR-1 pozostaje): regula jest zapisana przez uzytkownika, ma zrodlo, date i status POTWIERDZONY_PRZEZ_UZYTKOWNIKA;
--        specyfikacja brokera (OMI) ma pierwszenstwo; sufiksy bez reguly (np. .UK) nadal NIE dostaja waluty.
-- Ksiega: cena w transakcji BUY/SELL z walutą nieznana w chwili importu ma price_currency = 'XXX' (ISO: brak waluty). Po potwierdzeniu waluty wolno
--        JEDNORAZOWO uzupelnic price_currency, fx_rate i fx_rate_kind tylko dla wierszy z 'XXX'; pozostale pola rdzenia (kwota, czas, ilosc, cena...) nadal niezmienne.
-- WPLYW NA WYNIKI: BRAK (wartosc, kapital, wynik, koszt z gotowki bez zmian; kwoty sa w PLN z eksportu).
CREATE TABLE broker_symbol_suffix_rules (
  rule_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id      SMALLINT NOT NULL REFERENCES financial_institutions,
  symbol_suffix  TEXT NOT NULL CHECK (symbol_suffix ~ '^[A-Z0-9]{1,6}$'),
  attribute      TEXT NOT NULL DEFAULT 'TRADING_CURRENCY' CHECK (attribute = 'TRADING_CURRENCY'),
  value          TEXT NOT NULL CHECK (value ~ '^[A-Z]{3}$'),
  status         TEXT NOT NULL DEFAULT 'POTWIERDZONY_PRZEZ_UZYTKOWNIKA' CHECK (status = 'POTWIERDZONY_PRZEZ_UZYTKOWNIKA'),
  source         TEXT NOT NULL,
  confirmed_at   TIMESTAMPTZ NOT NULL,
  UNIQUE (broker_id, symbol_suffix, attribute)
);
COMMENT ON TABLE broker_symbol_suffix_rules IS 'Reguly ustawione JAWNIE przez uzytkownika (np. .PL = PLN). Nie sa zgadywaniem systemu; specyfikacja brokera ma pierwszenstwo.';

CREATE OR REPLACE FUNCTION fn_guard_transactions() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_mutable TEXT[] := ARRAY['status','notes','updated_at','last_seen_batch_id','related_txn_id','transfer_group_id','order_ref','details','txn_subtype'];
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Transakcji nie usuwa sie (uniewaznienie: status = VOIDED)'; END IF;
  -- jednorazowe uzupelnienie waluty ceny po potwierdzeniu przez uzytkownika (tylko z 'XXX' na prawdziwy kod)
  IF OLD.price_currency = 'XXX' AND NEW.price_currency <> 'XXX' THEN
    v_mutable := v_mutable || ARRAY['price_currency','fx_rate','fx_rate_kind'];
  END IF;
  IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
    RAISE EXCEPTION 'Rdzen transakcji % jest niezmienny (zmiana tylko wzbogacen i statusu)', OLD.txn_id;
  END IF;
  RETURN NEW;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON broker_symbol_suffix_rules TO investing_app;
  END IF;
END $$;
