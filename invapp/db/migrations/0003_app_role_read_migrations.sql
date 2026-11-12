-- 0003: rola aplikacji (investing_app) moze odczytac tabele schema_migrations (kontrola stanu w /zdrowie).
-- Zakres: wylacznie SELECT na jednej tabeli technicznej. Uprawnienia do tabel danych (wlasciwy model uprawnien
-- aplikacji) beda zaprojektowane osobno przy imporcie (krok 1) i wymagaja akceptacji.
-- Warunkowe: na tymczasowej bazie testowej (pgserver) rola investing_app nie istnieje - wtedy nic nie robimy.
-- Wplyw na obliczenia: BRAK.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app')
     AND to_regclass('public.schema_migrations') IS NOT NULL THEN
    GRANT SELECT ON TABLE schema_migrations TO investing_app;
  END IF;
END $$;
