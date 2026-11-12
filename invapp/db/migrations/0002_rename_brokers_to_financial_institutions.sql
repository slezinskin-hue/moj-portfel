-- 0002: zmiana nazwy rejestru instytucji  brokers -> financial_institutions
-- Powod: tabela przechowuje nie tylko brokerow (XTB, Trade Republic), ale tez TFI, banki itd.
--        (kolumna institution_kind). Decyzja uzytkownika z 2026-11-11 (Z-35).
-- Zakres: WYLACZNIE nazwa tabeli i jej ograniczen/sekwencji. Kolumny (w tym klucz glowny broker_id oraz kolumny
--         broker_id w tabelach zalezych) pozostaja bez zmian - zmiana nazw kolumn dotknelaby ok. 28 miejsc w schemacie
--         i testach i nie wnosi nic do logiki; ewentualna zmiana wymaga osobnej decyzji.
-- Wplyw na obliczenia: BRAK (widoki i klucze obce wskazuja tabele przez identyfikator obiektu, nie przez nazwe;
--         w funkcjach plpgsql nie ma odwolan do nazwy 'brokers').
ALTER TABLE brokers RENAME TO financial_institutions;
ALTER TABLE financial_institutions RENAME CONSTRAINT brokers_pkey TO financial_institutions_pkey;
ALTER TABLE financial_institutions RENAME CONSTRAINT brokers_code_key TO financial_institutions_code_key;
ALTER SEQUENCE IF EXISTS brokers_broker_id_seq RENAME TO financial_institutions_broker_id_seq;
COMMENT ON TABLE financial_institutions IS 'Rejestr instytucji finansowych: brokerzy, TFI, banki (institution_kind). Dawniej: brokers.';
