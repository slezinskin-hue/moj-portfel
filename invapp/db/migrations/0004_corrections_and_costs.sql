-- 0004: korekty (splity) i koszty: jawne, ze zrodlem, statusem i odtwarzalnoscia
-- Podstawa: dokument 14 §14.2 (decyzja D-KOSZT-1), dokument 17 §17.3.
--   * korekta ma zrodlo, status, dowody (evidence) i status zrodla OFICJALNEGO;
--   * korekta ze statusem HYPOTHESIS NIE jest stosowana w partiach (derived_lots);
--   * partia z korekta musi wskazywac rekordy korekt, a liczby musza sie zgadzac (zadnych ukrytych korekt);
--   * podstawa kosztu partii = gotowka z ksiegi (cost_basis = 'CASH'); to NIE jest koszt podatkowy;
--   * kazda roznica kosztu ma wiersz w v_cost_reconciliation z kodem przyczyny.
-- WPLYW NA WYNIKI: brak zmiany definicji wartosci, kapitalu ani wyniku. Dodane sa ograniczenia, widoki i funkcja wyjasniajaca.

-- ---------------------------------------------------------------
-- 1. corporate_actions: status HYPOTHESIS, zrodlo oficjalne, status daty
-- ---------------------------------------------------------------
ALTER TABLE corporate_actions DROP CONSTRAINT corporate_actions_status_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_status_check
  CHECK (status IN ('CONFIRMED','HYPOTHESIS','NEEDS_REVIEW','REJECTED'));

ALTER TABLE corporate_actions
  ADD COLUMN official_source_status TEXT NOT NULL DEFAULT 'NOT_VERIFIED'
        CHECK (official_source_status IN ('NOT_VERIFIED','VERIFIED')),
  ADD COLUMN official_source_ref TEXT,
  ADD COLUMN verified_at TIMESTAMPTZ,
  ADD COLUMN effective_date_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (effective_date_status IN ('UNKNOWN','HYPOTHESIS','FROM_BROKER_DATA','FROM_OFFICIAL_SOURCE')),
  ADD COLUMN applies_to_lots_opened_before TIMESTAMPTZ,   -- granica oparta na dowodach, gdy effective_date jest nieznana
  ADD COLUMN is_applied BOOLEAN GENERATED ALWAYS AS (status = 'CONFIRMED') STORED;

ALTER TABLE corporate_actions
  ADD CONSTRAINT ck_ca_ratio_positive CHECK (ratio_new > 0 AND ratio_old > 0),
  ADD CONSTRAINT ck_ca_official_source CHECK (official_source_status <> 'VERIFIED'
        OR (official_source_ref IS NOT NULL AND verified_at IS NOT NULL)),
  ADD CONSTRAINT ck_ca_evidence CHECK (status NOT IN ('CONFIRMED','HYPOTHESIS')
        OR (evidence IS NOT NULL AND jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb)),
  ADD CONSTRAINT ck_ca_effective_date CHECK ((effective_date IS NULL) = (effective_date_status = 'UNKNOWN')),
  ADD CONSTRAINT ck_ca_effective_official CHECK (effective_date_status <> 'FROM_OFFICIAL_SOURCE' OR official_source_status = 'VERIFIED'),
  ADD CONSTRAINT ck_ca_hypothesis_cutoff CHECK (status <> 'HYPOTHESIS' OR effective_date IS NOT NULL OR applies_to_lots_opened_before IS NOT NULL);

-- ---------------------------------------------------------------
-- 2. derived_lots: podstawa kosztu, rekordy korekt, dopasowanie po Position ID
-- ---------------------------------------------------------------
ALTER TABLE derived_lots DROP CONSTRAINT derived_lots_method_check;
ALTER TABLE derived_lots ADD CONSTRAINT derived_lots_method_check
  CHECK (method IN ('POSITION_ID','BROKER_POSITION','FIFO'));   -- BROKER_POSITION: nazwa dawna, rownowazna POSITION_ID (zachowana dla zgodnosci testow Fazy 0)
ALTER TABLE derived_lots
  ADD COLUMN adjustment_action_ids BIGINT[] NOT NULL DEFAULT '{}',
  ADD COLUMN cost_basis TEXT NOT NULL DEFAULT 'CASH' CHECK (cost_basis = 'CASH');
COMMENT ON COLUMN derived_lots.cost_cash_initial IS 'Koszt z gotowki z ksiegi (PLN), NIE koszt podatkowy. Split go nie zmienia.';

CREATE FUNCTION fn_guard_lot_adjustment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_id BIGINT; a corporate_actions%ROWTYPE; v_prod NUMERIC := 1; v_cut TIMESTAMPTZ;
BEGIN
  IF NEW.split_factor = 1 THEN
    IF cardinality(NEW.adjustment_action_ids) > 0 THEN
      RAISE EXCEPTION 'Partia % ma wskazane korekty, ale wspolczynnik wynosi 1', NEW.lot_id;
    END IF;
  ELSE
    IF cardinality(NEW.adjustment_action_ids) = 0 THEN
      RAISE EXCEPTION 'Ukryta korekta: wspolczynnik % bez rekordu korekty (corporate_actions)', NEW.split_factor;
    END IF;
    FOREACH v_id IN ARRAY NEW.adjustment_action_ids LOOP
      SELECT * INTO a FROM corporate_actions WHERE action_id = v_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Korekta % nie istnieje', v_id; END IF;
      IF a.status <> 'CONFIRMED' THEN
        RAISE EXCEPTION 'Korekta % ma status % - nie jest stosowana w obliczeniach (tylko CONFIRMED)', v_id, a.status;
      END IF;
      IF a.listing_id <> NEW.listing_id THEN RAISE EXCEPTION 'Korekta % dotyczy innego notowania', v_id; END IF;
      v_cut := COALESCE(a.applies_to_lots_opened_before, a.effective_date::timestamptz);
      IF v_cut IS NULL OR NEW.opened_at >= v_cut THEN
        RAISE EXCEPTION 'Korekta % nie obejmuje partii otwartej %', v_id, NEW.opened_at;
      END IF;
      v_prod := v_prod * (a.ratio_new / a.ratio_old);
    END LOOP;
    IF abs(v_prod - NEW.split_factor) > 0.0000001 THEN
      RAISE EXCEPTION 'Wspolczynnik partii % nie zgadza sie z iloczynem korekt %', NEW.split_factor, v_prod;
    END IF;
  END IF;
  IF abs(NEW.qty_as_reported * NEW.split_factor - NEW.qty_initial) > 0.00000001 THEN
    RAISE EXCEPTION 'Ilosc po korekcie (%) nie rowna sie ilosc_surowa x wspolczynnik (%)', NEW.qty_initial, NEW.qty_as_reported * NEW.split_factor;
  END IF;
  IF abs(NEW.price_as_reported / NEW.split_factor - NEW.price_adjusted) > 0.00000001 + abs(NEW.price_adjusted) * 0.000001 THEN
    RAISE EXCEPTION 'Cena po korekcie (%) nie rowna sie cena_surowa / wspolczynnik (%)', NEW.price_adjusted, NEW.price_as_reported / NEW.split_factor;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_guard_lot_adjustment BEFORE INSERT OR UPDATE ON derived_lots
  FOR EACH ROW EXECUTE FUNCTION fn_guard_lot_adjustment();

-- ---------------------------------------------------------------
-- 3. Mapa korekt brokera: kod przyczyny kazdej roznicy kosztu
-- ---------------------------------------------------------------
ALTER TABLE corporate_action_position_map
  ADD COLUMN reason_code TEXT CHECK (reason_code IN ('BROKER_FX_REVALUATION_AT_SPLIT','OTHER_EXPLAINED','UNEXPLAINED')),
  ADD CONSTRAINT ck_map_diff_consistent CHECK (cost_diff_cash = new_cost_cash_broker - old_cost_cash),
  ADD CONSTRAINT ck_map_reason_required CHECK (cost_diff_cash = 0 OR reason_code IS NOT NULL);

-- ---------------------------------------------------------------
-- 4. Widoki
-- ---------------------------------------------------------------
-- Korekty uzyte (lub nieuzyte) w obliczeniach, z jawna etykieta: STOSOWANA / NIESTOSOWANA
CREATE VIEW v_active_adjustments AS
SELECT a.action_id, a.listing_id, l.display_name, a.action_type, a.ratio_new, a.ratio_old,
       a.ratio_new / a.ratio_old AS factor,
       a.effective_date, a.effective_date_status, a.applies_to_lots_opened_before,
       a.source, a.status, a.is_applied,
       CASE WHEN a.is_applied THEN 'STOSOWANA'
            WHEN a.status = 'HYPOTHESIS' THEN 'NIESTOSOWANA (hipoteza)'
            ELSE 'NIESTOSOWANA' END AS apply_label,
       a.official_source_status, a.official_source_ref, a.verified_at, a.evidence,
       (SELECT count(*) FROM derived_lots dl WHERE a.action_id = ANY (dl.adjustment_action_ids)) AS lots_adjusted
FROM corporate_actions a JOIN listings l USING (listing_id)
WHERE a.status <> 'REJECTED';

-- Widok HIPOTETYCZNY: ilosc po zastosowaniu korekt HYPOTHESIS (NIE jest podstawa zadnego wyniku)
CREATE VIEW v_hypothetical_lot_quantities AS
SELECT dl.lot_id, dl.account_id, dl.listing_id, dl.opened_at,
       dl.qty_as_reported, dl.split_factor AS applied_factor, h.hypothesis_factor,
       dl.qty_as_reported * dl.split_factor * COALESCE(h.hypothesis_factor, 1) AS qty_hypothetical_initial,
       h.action_ids AS hypothesis_action_ids,
       'HIPOTETYCZNE (korekta niepotwierdzona)'::text AS label
FROM derived_lots dl
JOIN LATERAL (
  SELECT round(exp(sum(ln(a.ratio_new / a.ratio_old))), 8) AS hypothesis_factor, array_agg(a.action_id ORDER BY a.action_id) AS action_ids
  FROM corporate_actions a
  WHERE a.listing_id = dl.listing_id AND a.status = 'HYPOTHESIS'
    AND dl.opened_at < COALESCE(a.applies_to_lots_opened_before, a.effective_date::timestamptz)
) h ON h.hypothesis_factor IS NOT NULL;

-- Uzgodnienie kosztow: koszt z gotowki (ksiega) vs koszt wg brokera, z kodem przyczyny
CREATE VIEW v_cost_reconciliation AS
SELECT m.map_id, m.action_id, a.listing_id, m.account_id, m.old_position_ref, m.new_position_ref,
       m.old_cost_cash AS cost_cash_ledger, m.new_cost_cash_broker AS cost_cash_broker, m.cost_diff_cash,
       CASE WHEN m.cost_diff_cash = 0 THEN 'NONE' ELSE COALESCE(m.reason_code, 'UNEXPLAINED') END AS reason_code,
       (m.cost_diff_cash <> 0 AND COALESCE(m.reason_code, 'UNEXPLAINED') = 'UNEXPLAINED') AS needs_attention,
       a.status AS action_status, a.official_source_status, 'SPLIT_MAP'::text AS source
FROM corporate_action_position_map m JOIN corporate_actions a ON a.action_id = m.action_id;

CREATE VIEW v_cost_reconciliation_totals AS
SELECT account_id, listing_id, reason_code, count(*) AS positions,
       sum(cost_cash_ledger) AS cost_cash_ledger, sum(cost_cash_broker) AS cost_cash_broker, sum(cost_diff_cash) AS cost_diff_cash
FROM v_cost_reconciliation GROUP BY account_id, listing_id, reason_code;

-- ---------------------------------------------------------------
-- 5. Odtworzenie obliczenia partii krok po kroku
-- ---------------------------------------------------------------
CREATE FUNCTION fn_explain_lot(p_lot_id BIGINT) RETURNS TABLE (step INT, label TEXT, value TEXT)
LANGUAGE plpgsql STABLE AS $$
DECLARE l derived_lots%ROWTYPE; t transactions%ROWTYPE; a corporate_actions%ROWTYPE; v_id BIGINT; n INT := 2;
BEGIN
  SELECT * INTO l FROM derived_lots WHERE lot_id = p_lot_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Partia % nie istnieje', p_lot_id; END IF;
  SELECT * INTO t FROM transactions WHERE txn_id = l.open_txn_id;
  step := 1; label := 'Zakup w ksiedze (dane brokera, bez korekt)';
  value := format('%s szt. x %s %s; gotowka %s %s; dedup_key %s', t.quantity, t.price, t.price_currency, t.cash_amount, t.cash_currency, t.dedup_key);
  RETURN NEXT;
  step := 2; label := 'Ilosc i cena zapisane w partii jak w ksiedze'; value := format('%s szt. x %s', l.qty_as_reported, l.price_as_reported);
  RETURN NEXT;
  IF cardinality(l.adjustment_action_ids) = 0 THEN
    n := n + 1; step := n; label := 'Korekty'; value := 'brak (wspolczynnik 1)'; RETURN NEXT;
  ELSE
    FOREACH v_id IN ARRAY l.adjustment_action_ids LOOP
      SELECT * INTO a FROM corporate_actions WHERE action_id = v_id;
      n := n + 1; step := n; label := format('Korekta #%s', v_id);
      value := format('%s %s:%s, status %s, zrodlo %s, zrodlo oficjalne %s, data %s (%s)', a.action_type, a.ratio_new, a.ratio_old, a.status,
                      a.source, a.official_source_status, COALESCE(a.effective_date::text, 'nieznana'), a.effective_date_status);
      RETURN NEXT;
    END LOOP;
  END IF;
  n := n + 1; step := n; label := 'Wspolczynnik laczny'; value := l.split_factor::text; RETURN NEXT;
  n := n + 1; step := n; label := 'Ilosc po korekcie = ilosc surowa x wspolczynnik';
  value := format('%s x %s = %s (zapisano %s; %s)', l.qty_as_reported, l.split_factor, l.qty_as_reported * l.split_factor, l.qty_initial,
                  CASE WHEN abs(l.qty_as_reported * l.split_factor - l.qty_initial) < 0.00000001 THEN 'zgodne' ELSE 'NIEZGODNE' END);
  RETURN NEXT;
  n := n + 1; step := n; label := 'Cena po korekcie = cena surowa / wspolczynnik';
  value := format('%s / %s = %s (zapisano %s)', l.price_as_reported, l.split_factor, round(l.price_as_reported / l.split_factor, 8), l.price_adjusted);
  RETURN NEXT;
  n := n + 1; step := n; label := 'Koszt z gotowki (PLN) - NIE koszt podatkowy; korekta go nie zmienia';
  value := format('%s (gotowka z zakupu: %s; %s)', l.cost_cash_initial, -t.cash_amount,
                  CASE WHEN l.cost_cash_initial = -t.cash_amount THEN 'zgodne' ELSE 'ROZNE' END);
  RETURN NEXT;
END $$;
