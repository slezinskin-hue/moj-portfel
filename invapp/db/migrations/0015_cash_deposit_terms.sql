-- 0015: GOTOWKA OPROCENTOWANA JAKO LOKATA (decyzja uzytkownika 2026-11-12: gotowka w Trade Republic = lokata 6% rocznie,
--   odsetki co miesiac (kapitalizacja), podatek Belki 19%; "wartosc aktywow to tez lokaty").
--   Decyzja jest zapisywana per konto (nowy wiersz = nowa decyzja; obowiazuje najnowsza), ze zrodlem stopy (np. wyznaczona z wyplaconych odsetek)
--   i z tym, kto zdecydowal. Odmowa (treat_as_deposit = false) tez jest zapisywana, zeby pytanie nie wracalo.
--   Sama wartosc lokaty (saldo gotowki z ksiegi - tylko kwoty zaksiegowane) jest liczona w invapp/core/deposits.py.
-- WPLYW NA WYNIKI: tabela nowa; ksiega, partie i widoki BEZ ZMIAN. Wartosc aktywow w aplikacji rosnie o wartosc lokat (zysk calkowity bez zmian)
--   (opis i liczby: docs/ARCHITEKTURA.md, sekcja o lokatach).
CREATE TABLE cash_deposit_terms (
  terms_id          BIGSERIAL PRIMARY KEY,
  account_id        BIGINT NOT NULL REFERENCES accounts(account_id),
  treat_as_deposit  BOOLEAN NOT NULL,
  annual_rate       NUMERIC(9,6) CHECK (annual_rate IS NULL OR (annual_rate >= 0 AND annual_rate < 1)),
  tax_rate          NUMERIC(5,4) CHECK (tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate < 1)),
  day_count         INTEGER CHECK (day_count IS NULL OR day_count IN (360, 365)),
  capitalization    TEXT CHECK (capitalization IS NULL OR capitalization = 'MONTHLY'),
  rate_source       TEXT,
  decided_by        TEXT NOT NULL,
  decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  note              TEXT,
  CHECK (NOT treat_as_deposit OR (annual_rate IS NOT NULL AND tax_rate IS NOT NULL AND day_count IS NOT NULL AND capitalization IS NOT NULL AND rate_source IS NOT NULL))
);
CREATE INDEX cash_deposit_terms_account ON cash_deposit_terms(account_id, decided_at DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON cash_deposit_terms TO investing_app;
  END IF;
END $$;
