-- 0019: PROFIL INSTRUMENTU DO ZAKLADKI "EKSPOZYCJA" (decyzja uzytkownika 2026-11-12: ekspozycja jako osobna zakladka).
--   Typ notowania (akcja/ETF), sektor i branza oraz gielda wg Yahoo Finance (wyszukiwarka symboli; wysylany jest tylko symbol).
--   Dane opisowe - nie wplywaja na zadne kwoty. Dla ETF Yahoo nie podaje sektora (sklad funduszu nie jest rozpisywany).
-- WPLYW NA WYNIKI: brak (nowa tabela tylko do podzialu wartosci w zakladce Ekspozycja).
CREATE TABLE listing_profile (
  listing_id   BIGINT PRIMARY KEY REFERENCES listings,
  provider     TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  quote_type   TEXT,
  sector       TEXT,
  industry     TEXT,
  exchange     TEXT,
  long_name    TEXT,
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON listing_profile TO investing_app;
  END IF;
END $$;
