-- 0006: ochrona danych surowych i model uprawnien roli aplikacji (dokument 17 §17.2, §17.3)
--   * dane surowe i ksiega nie sa nadpisywane: tabele tylko do dopisywania, rdzen transakcji niezmienny
--     (dozwolone zmiany: wzbogacenia - powiazania, grupy, status, notatki, znacznik ostatniego widzenia);
--   * rola aplikacji (investing_app) ma WYLACZNIE odczyt (tabele i widoki) z wyjatkiem danych surowych i konfliktow importu;
--     zapis przez aplikacje bedzie mozliwy tylko przez wskazane funkcje dodawane w kolejnych migracjach (np. PPK, potwierdzenia).
-- WPLYW NA WYNIKI: brak. Uprawnienia sa warunkowe (rola istnieje tylko na serwerze lokalnym i w testach).

-- ---------------------------------------------------------------
-- 1. Tabele tylko do dopisywania
-- ---------------------------------------------------------------
CREATE FUNCTION fn_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Tabela % jest tylko do dopisywania (surowe dane nie sa modyfikowane ani usuwane)', TG_TABLE_NAME;
END $$;

CREATE TRIGGER trg_raw_rows_append_only BEFORE UPDATE OR DELETE ON raw_rows
  FOR EACH ROW EXECUTE FUNCTION fn_append_only();
CREATE TRIGGER trg_snapshots_append_only BEFORE UPDATE OR DELETE ON broker_position_snapshots
  FOR EACH ROW EXECUTE FUNCTION fn_append_only();

-- Zamkniecia brokera: jedyna dozwolona zmiana to powiazanie z korekta (corporate_action_id) z NULL na wartosc
CREATE FUNCTION fn_guard_closed_positions() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Zamkniecia brokera (broker_closed_positions) nie sa usuwane'; END IF;
  IF (to_jsonb(NEW) - 'corporate_action_id') IS DISTINCT FROM (to_jsonb(OLD) - 'corporate_action_id')
     OR (OLD.corporate_action_id IS NOT NULL AND NEW.corporate_action_id IS DISTINCT FROM OLD.corporate_action_id) THEN
    RAISE EXCEPTION 'Zamkniecia brokera sa niezmienne (mozna tylko powiazac z korekta)';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_closed_positions_guard BEFORE UPDATE OR DELETE ON broker_closed_positions
  FOR EACH ROW EXECUTE FUNCTION fn_guard_closed_positions();

-- Ksiega: rdzen transakcji niezmienny; wzbogacenia i status dozwolone; brak usuwania (uniewaznienie przez status)
CREATE FUNCTION fn_guard_transactions() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_mutable TEXT[] := ARRAY['status','notes','updated_at','last_seen_batch_id','related_txn_id','transfer_group_id','order_ref','details','txn_subtype'];
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Transakcji nie usuwa sie (uniewaznienie: status = VOIDED)'; END IF;
  IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
    RAISE EXCEPTION 'Rdzen transakcji % jest niezmienny (zmiana tylko wzbogacen i statusu)', OLD.txn_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_transactions_guard BEFORE UPDATE OR DELETE ON transactions
  FOR EACH ROW EXECUTE FUNCTION fn_guard_transactions();

-- ---------------------------------------------------------------
-- 2. Uprawnienia roli aplikacji: tylko odczyt
-- ---------------------------------------------------------------
DO $$
DECLARE r RECORD;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM investing_app;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM investing_app;
    FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r','v')
               AND c.relname NOT IN ('raw_rows','import_conflicts')       -- surowe wiersze i konflikty tylko dla importu / wlasciciela
    LOOP
      EXECUTE format('GRANT SELECT ON public.%I TO investing_app', r.relname);
    END LOOP;
  END IF;
END $$;
