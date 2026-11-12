-- 0005: tozsamosc instrumentow (dokument 14 §14.3, decyzja D-INSTR-1)
--   * sufiks symbolu (np. .UK) NIE wyznacza waluty ani gieldy; niezweryfikowane = puste (NULL / NIEZNANY);
--   * GBP i GBp to rozne jednostki (price_unit); waluta bazowa funduszu osobno od waluty notowania;
--   * kazda wartosc ma zrodlo, date i status; historia zmian dopisywana automatycznie i niezmienna;
--   * ceny rynkowe mozna pobierac tylko dla notowan zweryfikowanych (fn_listing_price_ready).
-- WPLYW NA WYNIKI: brak (wszystkie wartosci XTB sa w PLN z eksportu; Faza 1 nie pobiera cen).

-- ---------------------------------------------------------------
-- 1. securities
-- ---------------------------------------------------------------
ALTER TABLE securities DROP CONSTRAINT securities_isin_status_check;
ALTER TABLE securities ADD CONSTRAINT securities_isin_status_check CHECK (isin_status IN
  ('NIEZNANY','WNIOSKOWANY','Z_EKSPORTU_BROKERA','ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA'));
ALTER TABLE securities ADD COLUMN fund_base_currency CHAR(3) CHECK (fund_base_currency ~ '^[A-Z]{3}$');
COMMENT ON COLUMN securities.fund_base_currency IS 'Waluta bazowa funduszu/klasy udzialowej. NIE jest waluta notowania zadnego notowania (listings.trading_currency).';

-- ---------------------------------------------------------------
-- 2. listings: waluta, gielda, jednostka ceny - kazde ze zrodlem, data i statusem
-- ---------------------------------------------------------------
ALTER TABLE listings DROP CONSTRAINT listings_mic_status_check;
ALTER TABLE listings DROP CONSTRAINT listings_currency_status_check;
ALTER TABLE listings ALTER COLUMN trading_currency DROP NOT NULL;
ALTER TABLE listings ALTER COLUMN currency_status SET DEFAULT 'NIEZNANY';
ALTER TABLE listings
  ADD COLUMN currency_source TEXT,
  ADD COLUMN currency_checked_at TIMESTAMPTZ,
  ADD COLUMN mic_source TEXT,
  ADD COLUMN mic_checked_at TIMESTAMPTZ,
  ADD COLUMN price_unit TEXT CHECK (price_unit IN ('MAJOR','MINOR_1_100')),   -- MAJOR: GBP; MINOR_1_100: GBp (pensy)
  ADD COLUMN price_unit_status TEXT NOT NULL DEFAULT 'NIEZNANY',
  ADD COLUMN price_unit_source TEXT,
  ADD COLUMN price_unit_checked_at TIMESTAMPTZ;

ALTER TABLE listings
  ADD CONSTRAINT listings_mic_status_check CHECK (mic_status IN
      ('NIEZNANY','WNIOSKOWANY','ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  ADD CONSTRAINT listings_currency_status_check CHECK (currency_status IN
      ('NIEZNANY','WNIOSKOWANY','ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  ADD CONSTRAINT listings_price_unit_status_check CHECK (price_unit_status IN
      ('NIEZNANY','WNIOSKOWANY','ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  ADD CONSTRAINT ck_listing_currency_format CHECK (trading_currency IS NULL OR trading_currency ~ '^[A-Z]{3}$'),
  -- wartosc <-> status: brak wartosci = NIEZNANY; wartosc nie moze miec statusu NIEZNANY
  ADD CONSTRAINT ck_listing_currency_value CHECK ((trading_currency IS NULL) = (currency_status = 'NIEZNANY')),
  ADD CONSTRAINT ck_listing_mic_value CHECK ((exchange_mic IS NULL) = (mic_status = 'NIEZNANY')),
  ADD CONSTRAINT ck_listing_unit_value CHECK ((price_unit IS NULL) = (price_unit_status = 'NIEZNANY')),
  -- status zweryfikowany wymaga zrodla i daty sprawdzenia
  ADD CONSTRAINT ck_listing_currency_verified CHECK (currency_status IN ('NIEZNANY','WNIOSKOWANY')
      OR (currency_source IS NOT NULL AND currency_checked_at IS NOT NULL)),
  ADD CONSTRAINT ck_listing_mic_verified CHECK (mic_status IN ('NIEZNANY','WNIOSKOWANY')
      OR (mic_source IS NOT NULL AND mic_checked_at IS NOT NULL)),
  ADD CONSTRAINT ck_listing_unit_verified CHECK (price_unit_status IN ('NIEZNANY','WNIOSKOWANY')
      OR (price_unit_source IS NOT NULL AND price_unit_checked_at IS NOT NULL));

-- Wartosc podana bez statusu jest traktowana jako WNIOSKOWANA (nigdy jako zweryfikowana); brak wartosci = NIEZNANY.
CREATE FUNCTION fn_listing_normalize_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.trading_currency IS NULL THEN NEW.currency_status := 'NIEZNANY';
  ELSIF NEW.currency_status = 'NIEZNANY' THEN NEW.currency_status := 'WNIOSKOWANY'; END IF;
  IF NEW.exchange_mic IS NULL THEN NEW.mic_status := 'NIEZNANY';
  ELSIF NEW.mic_status = 'NIEZNANY' THEN NEW.mic_status := 'WNIOSKOWANY'; END IF;
  IF NEW.price_unit IS NULL THEN NEW.price_unit_status := 'NIEZNANY';
  ELSIF NEW.price_unit_status = 'NIEZNANY' THEN NEW.price_unit_status := 'WNIOSKOWANY'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_listing_normalize_status BEFORE INSERT OR UPDATE ON listings
  FOR EACH ROW EXECUTE FUNCTION fn_listing_normalize_status();

-- ---------------------------------------------------------------
-- 3. Historia atrybutow (dopisywana automatycznie, niezmienna)
-- ---------------------------------------------------------------
CREATE TABLE listing_attribute_history (
  hist_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id        BIGINT REFERENCES listings,
  security_id       BIGINT REFERENCES securities,
  attribute         TEXT NOT NULL CHECK (attribute IN ('EXCHANGE_MIC','TRADING_CURRENCY','PRICE_UNIT','ISIN','FUND_BASE_CURRENCY')),
  value             TEXT,
  status            TEXT NOT NULL,
  source            TEXT,
  source_ref        TEXT,
  checked_at        TIMESTAMPTZ,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  recorded_by       TEXT NOT NULL DEFAULT current_user,
  CONSTRAINT ck_hist_target CHECK ((listing_id IS NOT NULL) <> (security_id IS NOT NULL))
);
CREATE INDEX ix_lah_listing ON listing_attribute_history (listing_id, attribute, hist_id);
CREATE INDEX ix_lah_security ON listing_attribute_history (security_id, attribute, hist_id);

CREATE FUNCTION fn_lah_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Historia atrybutow instrumentow jest niezmienna (tylko dopisywanie)'; END $$;
CREATE TRIGGER trg_lah_immutable BEFORE UPDATE OR DELETE ON listing_attribute_history
  FOR EACH ROW EXECUTE FUNCTION fn_lah_immutable();

CREATE FUNCTION fn_listing_record_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR (OLD.trading_currency, OLD.currency_status, OLD.currency_source) IS DISTINCT FROM (NEW.trading_currency, NEW.currency_status, NEW.currency_source) THEN
    INSERT INTO listing_attribute_history(listing_id, attribute, value, status, source, source_ref, checked_at)
    VALUES (NEW.listing_id, 'TRADING_CURRENCY', NEW.trading_currency, NEW.currency_status, NEW.currency_source, NEW.currency_evidence::text, NEW.currency_checked_at);
  END IF;
  IF TG_OP = 'INSERT' OR (OLD.exchange_mic, OLD.mic_status, OLD.mic_source) IS DISTINCT FROM (NEW.exchange_mic, NEW.mic_status, NEW.mic_source) THEN
    INSERT INTO listing_attribute_history(listing_id, attribute, value, status, source, checked_at)
    VALUES (NEW.listing_id, 'EXCHANGE_MIC', NEW.exchange_mic, NEW.mic_status, NEW.mic_source, NEW.mic_checked_at);
  END IF;
  IF TG_OP = 'INSERT' OR (OLD.price_unit, OLD.price_unit_status, OLD.price_unit_source) IS DISTINCT FROM (NEW.price_unit, NEW.price_unit_status, NEW.price_unit_source) THEN
    INSERT INTO listing_attribute_history(listing_id, attribute, value, status, source, checked_at)
    VALUES (NEW.listing_id, 'PRICE_UNIT', NEW.price_unit, NEW.price_unit_status, NEW.price_unit_source, NEW.price_unit_checked_at);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_listing_record_history AFTER INSERT OR UPDATE ON listings
  FOR EACH ROW EXECUTE FUNCTION fn_listing_record_history();

CREATE FUNCTION fn_security_record_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR (OLD.isin, OLD.isin_status) IS DISTINCT FROM (NEW.isin, NEW.isin_status) THEN
    INSERT INTO listing_attribute_history(security_id, attribute, value, status, source, source_ref)
    VALUES (NEW.security_id, 'ISIN', NEW.isin, NEW.isin_status, NEW.isin_evidence->>'source', NEW.isin_evidence::text);
  END IF;
  IF TG_OP = 'INSERT' OR OLD.fund_base_currency IS DISTINCT FROM NEW.fund_base_currency THEN
    IF NEW.fund_base_currency IS NOT NULL OR TG_OP = 'UPDATE' THEN
      INSERT INTO listing_attribute_history(security_id, attribute, value, status, source)
      VALUES (NEW.security_id, 'FUND_BASE_CURRENCY', NEW.fund_base_currency, 'NIEZNANY', NULL);
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_security_record_history AFTER INSERT OR UPDATE ON securities
  FOR EACH ROW EXECUTE FUNCTION fn_security_record_history();

-- ---------------------------------------------------------------
-- 4. Oficjalna specyfikacja brokera (np. XTB OMI) jako tabela referencyjna ze zrodlem
-- ---------------------------------------------------------------
CREATE TABLE broker_instrument_specs (
  spec_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id          SMALLINT NOT NULL REFERENCES financial_institutions,
  symbol             TEXT NOT NULL,
  instrument_name    TEXT NOT NULL,
  isin               TEXT,
  currency           CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  instrument_type    TEXT NOT NULL CHECK (instrument_type IN ('STOCK','ETF','ETN','ETC','OTHER')),
  source_doc_name    TEXT NOT NULL,
  source_doc_sha256  CHAR(64) NOT NULL,
  source_doc_url     TEXT,
  valid_from         DATE NOT NULL,
  imported_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (broker_id, symbol, source_doc_sha256)
);
CREATE INDEX ix_bis_symbol ON broker_instrument_specs (broker_id, symbol);
COMMENT ON TABLE broker_instrument_specs IS 'Specyfikacja instrumentow wg dokumentu brokera (zrodlo i data wazna). Nie zawiera giełdy ani jednostki ceny - tych kolumn dokument nie podaje.';

-- ---------------------------------------------------------------
-- 5. Gotowosc notowania do pobierania cen: tylko wartosci zweryfikowane
-- ---------------------------------------------------------------
CREATE FUNCTION fn_listing_price_ready(p_listing_id BIGINT) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
  SELECT COALESCE((SELECT l.trading_currency IS NOT NULL AND l.exchange_mic IS NOT NULL AND l.price_unit IS NOT NULL
                          AND l.currency_status   IN ('ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')
                          AND l.mic_status        IN ('ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')
                          AND l.price_unit_status IN ('ZGODNY_SPECYFIKACJA_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')
                   FROM listings l WHERE l.listing_id = p_listing_id), FALSE);
$$;
