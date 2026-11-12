-- 0017: ZAKLADKA "GOTOWKA" I USTAWIENIA (decyzja uzytkownika 2026-11-12):
--   * konta bankowe wpisywane recznie (nazwa, waluta, saldo na dzien) - historia sald (nowy wpis = nowe saldo; obowiazuje najnowsze),
--     usuniecie = archiwizacja (archived_at), bez kasowania historii. Bez numerow kont (IBAN nie jest potrzebny ani zapisywany).
--   * ustawienia aplikacji (klucz -> wartosc JSON): m.in. "uwzgledniaj gotowke" i "uwzgledniaj wolne srodki XTB" w wartosci portfela.
-- WPLYW NA WYNIKI: nowe tabele; ksiega, partie, widoki BEZ ZMIAN. Domyslnie ustawienia sa WYLACZONE, wiec zadna liczba sie nie zmienia,
--   dopoki uzytkownik ich nie wlaczy; po wlaczeniu rosnie tylko "wartosc portfela" (zysk i wskazniki bez zmian) - opis: docs/ARCHITEKTURA.md.
CREATE TABLE manual_cash_accounts (
  cash_account_id  BIGSERIAL PRIMARY KEY,
  name             TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  currency         CHAR(3) NOT NULL DEFAULT 'PLN' CHECK (currency ~ '^[A-Z]{3}$'),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  archived_at      TIMESTAMPTZ
);

CREATE TABLE manual_cash_balances (
  balance_id       BIGSERIAL PRIMARY KEY,
  cash_account_id  BIGINT NOT NULL REFERENCES manual_cash_accounts(cash_account_id),
  balance          NUMERIC(18,2) NOT NULL,
  as_of            DATE NOT NULL,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX manual_cash_balances_account ON manual_cash_balances(cash_account_id, as_of DESC, recorded_at DESC);

CREATE TABLE app_settings (
  key         TEXT PRIMARY KEY CHECK (key ~ '^[a-z_]{1,40}$'),
  value       JSONB NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN
    GRANT SELECT ON manual_cash_accounts, manual_cash_balances, app_settings TO investing_app;
  END IF;
END $$;
