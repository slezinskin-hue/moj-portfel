-- =====================================================================
-- Aplikacja inwestycyjna – PROPOZYCJA schematu bazy (Faza 0, do akceptacji)
-- Dialekt: PostgreSQL 15+. Adaptacja do SQLite opisana w 03-model-danych.md §3.9.
-- Zasady: kwoty/ilości = NUMERIC (nigdy float); czas = UTC (TIMESTAMPTZ);
--         księga (transactions) jest źródłem prawdy, tabele "derived_*" można
--         w każdej chwili skasować i przeliczyć.
-- =====================================================================

-- ---------------------------------------------------------------
-- 1. SŁOWNIKI: brokerzy, konta, instrumenty
-- ---------------------------------------------------------------
CREATE TABLE brokers (
  broker_id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE,                 -- 'XTB'
  name        TEXT NOT NULL,
  institution_kind TEXT NOT NULL DEFAULT 'BROKER' CHECK (institution_kind IN ('BROKER','TFI','BANK','OTHER'))
                    -- tabela to rejestr INSTYTUCJI FINANSOWYCH (broker, TFI prowadzące PPK, bank); nazwa historyczna
);

CREATE TABLE accounts (
  account_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id           SMALLINT NOT NULL REFERENCES brokers,
  external_no         TEXT NOT NULL,                -- nr konta u brokera ('12345678'); gdy plik go nie zawiera (Trade Republic) – etykieta nadana przez użytkownika
  external_no_source  TEXT NOT NULL DEFAULT 'FILE' CHECK (external_no_source IN ('FILE','USER','BROKER_API')),
  display_name        TEXT NOT NULL,                -- 'IKE (XTB)'
  product_type        TEXT NOT NULL CHECK (product_type IN
                        ('STANDARD','STANDARD_SUB','IKE','IKZE','PAYMENT','BANK','TERM_DEPOSIT','PPK','RETIREMENT_OTHER','SAVINGS','OTHER')),
  ownership           TEXT NOT NULL DEFAULT 'OWN' CHECK (ownership IN ('OWN')),   -- rejestr zawiera tylko rachunki własne
  account_class       TEXT NOT NULL DEFAULT 'INVESTMENT' CHECK (account_class IN ('INVESTMENT','PAYMENT','BANK','TERM_DEPOSIT','RETIREMENT','CASH','OTHER')),
  tracking_mode       TEXT NOT NULL DEFAULT 'FULL' CHECK (tracking_mode IN ('FULL','TRANSFERS_ONLY','BALANCE_ONLY')),
                      -- FULL: pełna księga (import); TRANSFERS_ONLY: znamy tylko transfery, saldo nieznane;
                      -- BALANCE_ONLY: tylko ręczne wyceny z datą (lokata, PPK, rachunek bankowy)
  broker_product_label TEXT,                        -- surowa etykieta: 'My Trades', 'Investment Plans', 'IKE'
  tax_regime          TEXT NOT NULL CHECK (tax_regime IN ('PIT38','IKE','IKZE','PPK','NONE')),
  base_currency       CHAR(3) NOT NULL DEFAULT 'PLN',
  parent_account_id   BIGINT REFERENCES accounts,   -- podkonto -> konto główne (Plany -> My Trades)
  opened_at           TIMESTAMPTZ,
  closed_at           TIMESTAMPTZ,
  UNIQUE (broker_id, external_no)
);

-- Rozpoznawanie konta z pliku: etykieta produktu / nazwa pliku -> konto (potwierdzane raz, przy 1. imporcie)
CREATE TABLE account_aliases (
  broker_id    SMALLINT NOT NULL REFERENCES brokers,
  alias_kind   TEXT NOT NULL CHECK (alias_kind IN ('FILE_ACCOUNT_NO','PRODUCT_LABEL')),
  alias_value  TEXT NOT NULL,                        -- '12345678' | 'Investment Plan' | 'Investment Plans'
  context_account_no TEXT NOT NULL DEFAULT '',       -- dla PRODUCT_LABEL: nr konta z nazwy pliku ('12345678')
  account_id   BIGINT NOT NULL REFERENCES accounts,
  PRIMARY KEY (broker_id, alias_kind, alias_value, context_account_no)
);

-- Papier wartościowy (fundusz/spółka) – niezależnie od giełdy
CREATE TABLE securities (
  security_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  isin          TEXT UNIQUE,                         -- NULL dopóki nie uzupełnimy (eksport XTB go nie zawiera); identyfikuje KLASĘ UDZIAŁOWĄ, nie linię handlową
  isin_status   TEXT NOT NULL DEFAULT 'NIEZNANY' CHECK (isin_status IN
                  ('NIEZNANY','WNIOSKOWANY','Z_EKSPORTU_BROKERA','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  isin_evidence JSONB,                               -- skąd, kiedy, link
  name          TEXT NOT NULL,
  asset_class   TEXT NOT NULL CHECK (asset_class IN ('STOCK','ETF','BOND','CRYPTO','OTHER')),
  distribution  TEXT CHECK (distribution IN ('ACCUMULATING','DISTRIBUTING')),
  country_of_issuer CHAR(2)
);

-- Notowanie = linia handlowa: (papier, MIC giełdy, waluta notowania).
-- VUAA.DE i VUAA.UK to 2 notowania 1 papieru; ten sam ISIN może mieć kilka linii na jednej giełdzie w różnych walutach.
-- Gdy ISIN jest nieznany, notowanie wskazuje „papier zastępczy” (securities.isin IS NULL, isin_status = 'NIEZNANY').
CREATE TABLE listings (
  listing_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  security_id       BIGINT NOT NULL REFERENCES securities,
  display_name      TEXT NOT NULL,                   -- 'FTSE All-World'
  exchange_mic      TEXT,                            -- 'XETR' itp.; NULL = nieznana
  mic_status        TEXT NOT NULL DEFAULT 'NIEZNANY' CHECK (mic_status IN
                      ('NIEZNANY','WNIOSKOWANY','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  trading_currency  CHAR(3) NOT NULL,                -- wartość robocza dopuszczalna, ale ze statusem poniżej
  currency_status   TEXT NOT NULL DEFAULT 'WNIOSKOWANY' CHECK (currency_status IN
                      ('NIEZNANY','WNIOSKOWANY','ZGODNY_ZRODLO_ZEWN','POTWIERDZONY_PRZEZ_UZYTKOWNIKA')),
  currency_evidence JSONB,                           -- np. {"regula":"sufiks .UK -> USD","kurs_migawki":3.9337,"zrodla":[...]}
  CONSTRAINT uq_listing_line UNIQUE NULLS NOT DISTINCT (security_id, exchange_mic, trading_currency)
);

-- Alias brokera: (broker, symbol) -> notowanie. Zmiana przypisania jest historią, nie nadpisaniem.
CREATE TABLE listing_aliases (
  alias_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id     SMALLINT NOT NULL REFERENCES brokers,
  broker_symbol TEXT NOT NULL,                       -- 'VUAA.UK'
  listing_id    BIGINT NOT NULL REFERENCES listings,
  market_suffix TEXT,                                -- 'UK' (wyprowadzone z symbolu)
  valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to      TIMESTAMPTZ,
  verified_by   TEXT,                                -- 'system' | 'user' | 'external:<źródło>'
  UNIQUE (broker_id, broker_symbol, valid_from)
);
CREATE UNIQUE INDEX uq_alias_active ON listing_aliases (broker_id, broker_symbol) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------
-- 2. IMPORT: partie, surowe wiersze, konflikty, jakość
-- ---------------------------------------------------------------
CREATE TABLE import_batches (
  batch_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id           SMALLINT NOT NULL REFERENCES brokers,
  source_file_name    TEXT NOT NULL,
  source_file_sha256  CHAR(64) NOT NULL,
  parser_name         TEXT NOT NULL,                 -- 'xtb_xlsx'
  parser_version      TEXT NOT NULL,
  period_from         TIMESTAMPTZ,
  period_to           TIMESTAMPTZ,
  report_generated_at TIMESTAMPTZ,
  account_id          BIGINT REFERENCES accounts,
  status              TEXT NOT NULL CHECK (status IN ('RUNNING','COMMITTED','FAILED','ROLLED_BACK')),
  rows_read           INT NOT NULL DEFAULT 0,
  rows_inserted       INT NOT NULL DEFAULT 0,
  rows_duplicate      INT NOT NULL DEFAULT 0,        -- ten sam dedup_key, ten sam row_hash
  rows_conflict       INT NOT NULL DEFAULT 0,        -- ten sam dedup_key, INNY row_hash
  rows_unmapped       INT NOT NULL DEFAULT 0,
  rows_ignored        INT NOT NULL DEFAULT 0,        -- stopki, pola kontrolne
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at         TIMESTAMPTZ,
  report              JSONB                          -- pełny raport importu (kontrole, ostrzeżenia)
);
CREATE INDEX ix_batches_hash ON import_batches (source_file_sha256);

-- Surowe wiersze (audyt/odtwarzalność). Nie służą do raportów.
CREATE TABLE raw_rows (
  raw_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id      BIGINT NOT NULL REFERENCES import_batches ON DELETE CASCADE,
  sheet_name    TEXT NOT NULL,
  row_number    INT  NOT NULL,
  row_kind      TEXT NOT NULL CHECK (row_kind IN ('DATA','FOOTER','SUMMARY','META')),
  natural_key   TEXT,
  row_hash      CHAR(64),
  payload       JSONB NOT NULL,
  outcome       TEXT NOT NULL CHECK (outcome IN ('INSERTED','DUPLICATE','CONFLICT','UNMAPPED','IGNORED')),
  txn_id        BIGINT,                              -- FK dodany niżej
  UNIQUE (batch_id, sheet_name, row_number)
);

CREATE TABLE import_conflicts (
  conflict_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id      BIGINT NOT NULL REFERENCES import_batches,
  dedup_key     TEXT NOT NULL,
  existing_hash CHAR(64) NOT NULL,
  incoming_hash CHAR(64) NOT NULL,
  incoming_payload JSONB NOT NULL,
  resolution    TEXT CHECK (resolution IN ('KEEP_EXISTING','REPLACE','PENDING')) DEFAULT 'PENDING',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE data_quality_issues (
  issue_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id     BIGINT REFERENCES import_batches,
  code         TEXT NOT NULL,                        -- 'DQ-05_SPLIT_ARTIFACT', 'DQ-06_IMPLICIT_SPLIT', ...
  severity     TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR')),
  entity_ref   TEXT,                                 -- np. 'position:1439598280'
  message      TEXT NOT NULL,
  details      JSONB,
  status       TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at  TIMESTAMPTZ
);

-- ---------------------------------------------------------------
-- 3. KSIĘGA TRANSAKCJI (źródło prawdy)
-- ---------------------------------------------------------------
CREATE TABLE transactions (
  txn_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id          BIGINT NOT NULL REFERENCES accounts,
  broker_id           SMALLINT NOT NULL REFERENCES brokers,   -- denormalizacja (z konta) dla UNIQUE na identyfikatorze brokera
  txn_type            TEXT NOT NULL CHECK (txn_type IN (
                        'CASH_DEPOSIT','CASH_WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT',
                        'BUY','SELL','DIVIDEND','TAX_WITHHOLDING','INTEREST','TAX_INTEREST',
                        'FEE','REWARD','FX_CONVERSION','TRANSFER_IN_KIND','TRANSFER_OUT_KIND','OTHER')),
  txn_subtype         TEXT,                          -- 'sec_fee','blik','card','correction', ...
  occurred_at         TIMESTAMPTZ NOT NULL,          -- UTC, z milisekundami
  listing_id          BIGINT REFERENCES listings,
  quantity            NUMERIC(24,8) CHECK (quantity IS NULL OR quantity >= 0),  -- zawsze >= 0; kierunek z txn_type
  price               NUMERIC(24,8),                 -- w price_currency
  price_currency      CHAR(3),
  gross_amount        NUMERIC(24,8),                 -- quantity*price (BUY/SELL) lub brutto dywidendy w jej walucie
  cash_amount         NUMERIC(24,8) NOT NULL,        -- PEŁNY wpływ na gotówkę ZE ZNAKIEM w cash_currency, WRAZ z prowizją wbudowaną w wiersz
                                                     -- (BUY: -(gross + commission); SELL: +(gross - commission))
  cash_currency       CHAR(3) NOT NULL,
  fx_rate             NUMERIC(24,10),                -- cash_currency za 1 price_currency
  fx_rate_kind        TEXT CHECK (fx_rate_kind IN ('EFFECTIVE_IMPLIED','BROKER_REPORTED','EXTERNAL','MANUAL')),
  commission          NUMERIC(24,8) NOT NULL DEFAULT 0,   -- część informacyjna prowizji (>=0) zawarta w cash_amount; XTB: 0; Trade Republic: 4,00 za wykonanie
  broker_position_ref TEXT,                          -- XTB 'Position ID'
  order_ref           TEXT,                          -- syntetyczna grupa wypełnień jednego zlecenia
  related_txn_id      BIGINT REFERENCES transactions,-- WHT -> dywidenda, korekta -> operacja korygowana
  transfer_group_id   TEXT,                          -- spina nogi przelewu / konwersji walut
  external_id         TEXT,
  external_id_scheme  TEXT,                          -- 'XTB_CASH_ID'
  dedup_key           TEXT NOT NULL UNIQUE,          -- patrz 07-silnik-importu.md §7.3
  row_hash            CHAR(64),
  source_kind         TEXT NOT NULL CHECK (source_kind IN ('BROKER_IMPORT','MANUAL','DERIVED')),
  first_batch_id      BIGINT REFERENCES import_batches,
  last_seen_batch_id  BIGINT REFERENCES import_batches,
  raw_comment         TEXT,
  details             JSONB,                         -- sparsowane pola: channel, wht_rate, dividend_per_share, order_volume, ...
  status              TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','VOIDED','SUPERSEDED')),
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- spójność wg typu
  CONSTRAINT ck_trade_fields CHECK (txn_type NOT IN ('BUY','SELL') OR
        (listing_id IS NOT NULL AND quantity IS NOT NULL AND price IS NOT NULL AND price_currency IS NOT NULL)),
  CONSTRAINT ck_income_fields CHECK (txn_type NOT IN ('DIVIDEND','TAX_WITHHOLDING') OR listing_id IS NOT NULL),
  CONSTRAINT ck_cash_sign CHECK (
        (txn_type IN ('CASH_DEPOSIT','TRANSFER_IN','INTEREST','REWARD') AND cash_amount >= 0) OR
        (txn_type IN ('CASH_WITHDRAWAL','TRANSFER_OUT','BUY','TAX_WITHHOLDING','TAX_INTEREST') AND cash_amount <= 0) OR
        txn_type IN ('SELL','DIVIDEND','FEE','FX_CONVERSION','TRANSFER_IN_KIND','TRANSFER_OUT_KIND','OTHER'))
        -- SELL: wpływ netto może być ujemny, gdy prowizja przewyższa przychód (Trade Republic: sprzedaż za 1,99 zł z opłatą 4,00 zł = −2,01 zł);
        -- korekty mogą mieć dowolny znak
  ,
  CONSTRAINT ck_commission_nonneg CHECK (commission >= 0),
  -- transfer papierów w naturze: ruch gotówki = 0, a WARTOŚĆ papierów w dniu transferu w gross_amount (wchodzi do przepływów przez granicę zakresu)
  CONSTRAINT ck_inkind_fields CHECK (txn_type NOT IN ('TRANSFER_IN_KIND','TRANSFER_OUT_KIND') OR
        (cash_amount = 0 AND gross_amount IS NOT NULL AND gross_amount >= 0 AND listing_id IS NOT NULL AND quantity IS NOT NULL))
);
CREATE UNIQUE INDEX uq_txn_broker_external ON transactions (broker_id, external_id_scheme, external_id)
  WHERE external_id IS NOT NULL;    -- identyfikator brokera jest unikalny W OBRĘBIE brokera i schematu (XTB_CASH_ID, TR_TXN_UUID, ...); wiersze potomne mają external_id = '<id>:tax'
CREATE INDEX ix_txn_account_time ON transactions (account_id, occurred_at);
CREATE INDEX ix_txn_listing      ON transactions (listing_id, occurred_at);
CREATE INDEX ix_txn_position     ON transactions (broker_position_ref);
CREATE INDEX ix_txn_group        ON transactions (transfer_group_id);
CREATE INDEX ix_txn_related      ON transactions (related_txn_id);
ALTER TABLE raw_rows ADD FOREIGN KEY (txn_id) REFERENCES transactions;

-- ---------------------------------------------------------------
-- 4. DANE BROKERA UŻYWANE DO WZBOGACANIA I KONTROLI (nie są księgą)
-- ---------------------------------------------------------------
-- 4a. Zamknięcia partii z arkusza Closed Positions: dokładny koszt/przychód w PLN
CREATE TABLE broker_closed_positions (
  bcp_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id          BIGINT NOT NULL REFERENCES accounts,
  listing_id          BIGINT NOT NULL REFERENCES listings,
  broker_position_ref TEXT NOT NULL,
  volume              NUMERIC(24,8) NOT NULL,
  open_price          NUMERIC(24,8) NOT NULL,
  close_price         NUMERIC(24,8) NOT NULL,
  opened_at           TIMESTAMPTZ NOT NULL,
  closed_at           TIMESTAMPTZ NOT NULL,
  purchase_value      NUMERIC(24,8) NOT NULL,        -- w walucie rachunku (PLN)
  sale_value          NUMERIC(24,8) NOT NULL,
  profit_loss         NUMERIC(24,8) NOT NULL,
  open_fx_rate        NUMERIC(24,10),
  close_fx_rate       NUMERIC(24,10),
  close_origin        TEXT,                          -- 'Android','xStation5','Correction'
  comment             TEXT,
  corporate_action_id BIGINT,                        -- FK niżej; dla artefaktów splitu (Correction/STC Transfer Out)
  dedup_key           TEXT NOT NULL UNIQUE,          -- 'xtb:closed:{pid}:{close_ms}:{volume}:{close_price}'
  row_hash            CHAR(64) NOT NULL,
  first_batch_id      BIGINT REFERENCES import_batches
);
CREATE INDEX ix_bcp_position ON broker_closed_positions (account_id, broker_position_ref);

-- 4b. Migawka Open Positions (wersjonowana czasem generowania raportu)
CREATE TABLE broker_position_snapshots (
  snapshot_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id            BIGINT NOT NULL REFERENCES import_batches,
  account_id          BIGINT NOT NULL REFERENCES accounts,
  as_of               TIMESTAMPTZ NOT NULL,          -- 'Data as of report generated'
  listing_id          BIGINT NOT NULL REFERENCES listings,
  row_level           TEXT NOT NULL CHECK (row_level IN ('INSTRUMENT','POSITION')),
  broker_position_ref TEXT,                          -- NULL dla wiersza instrumentu
  volume              NUMERIC(24,8) NOT NULL,
  open_price          NUMERIC(24,8),                 -- dla INSTRUMENT: średnia ważona brokera
  current_price       NUMERIC(24,8),                 -- NULL dla INSTRUMENT
  opened_at           TIMESTAMPTZ,
  value_cash          NUMERIC(24,8) NOT NULL,        -- wartość w PLN
  net_profit_cash     NUMERIC(24,8) NOT NULL,
  net_profit_pct      NUMERIC(12,4),
  implied_fx_rate     NUMERIC(24,10),                -- WYLICZONE: value / (volume * current_price)
  UNIQUE NULLS NOT DISTINCT (account_id, as_of, row_level, listing_id, broker_position_ref)  -- PG15+; wiersz INSTRUMENT ma ref = NULL
);

-- ---------------------------------------------------------------
-- 5. AKCJE KORPORACYJNE
-- ---------------------------------------------------------------
CREATE TABLE corporate_actions (
  action_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id    BIGINT NOT NULL REFERENCES listings,
  action_type   TEXT NOT NULL CHECK (action_type IN ('SPLIT','REVERSE_SPLIT','MERGER','SPINOFF','OTHER')),
  effective_date DATE,                               -- może być NIEZNANA (split wnioskowany z danych)
  ratio_new     NUMERIC(18,8) NOT NULL,              -- 10 (split 10:1 => 10 nowych za 1 starą)
  ratio_old     NUMERIC(18,8) NOT NULL DEFAULT 1,
  source        TEXT NOT NULL CHECK (source IN ('BROKER_ARTIFACT','INFERRED','EXTERNAL','MANUAL')),
  status        TEXT NOT NULL CHECK (status IN ('CONFIRMED','NEEDS_REVIEW','REJECTED')),
  evidence      JSONB,                               -- jakie wiersze/identyfikatory/źródła to uzasadniają
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE broker_closed_positions ADD FOREIGN KEY (corporate_action_id) REFERENCES corporate_actions;

-- Mapa „stara pozycja -> nowa pozycja” przy splitach realizowanych przez brokera jako zamknięcie + otwarcie.
-- Dane XTB zostają w tabelach surowych; tu tylko porównanie z wartością skorygowaną.
CREATE TABLE corporate_action_position_map (
  map_id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action_id             BIGINT NOT NULL REFERENCES corporate_actions,
  account_id            BIGINT NOT NULL REFERENCES accounts,
  old_position_ref      TEXT NOT NULL,               -- pozycja zamknięta jako 'Correction / STC Transfer Out'
  new_position_ref      TEXT NOT NULL,               -- pozycja otwarta przez brokera bez zakupu
  old_quantity          NUMERIC(24,8) NOT NULL,
  new_quantity          NUMERIC(24,8) NOT NULL,
  old_price             NUMERIC(24,8) NOT NULL,
  new_price             NUMERIC(24,8) NOT NULL,
  old_cost_cash         NUMERIC(24,8) NOT NULL,      -- faktycznie zapłacone (PLN)
  new_cost_cash_broker  NUMERIC(24,8) NOT NULL,      -- Value - Net Profit z migawki XTB
  cost_diff_cash        NUMERIC(24,8) NOT NULL,      -- new_cost_cash_broker - old_cost_cash
  broker_implied_fx     NUMERIC(24,10),              -- kurs użyty przez brokera przy nowej pozycji
  UNIQUE (action_id, old_position_ref)
);

-- ---------------------------------------------------------------
-- 6. DANE RYNKOWE (zewnętrzne źródła – patrz 06-brakujace-informacje.md)
-- ---------------------------------------------------------------
CREATE TABLE prices (
  listing_id  BIGINT NOT NULL REFERENCES listings,
  price_date  DATE   NOT NULL,
  close_price NUMERIC(24,8) NOT NULL,
  currency    CHAR(3) NOT NULL,
  source      TEXT NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (listing_id, price_date, source)
);
CREATE TABLE fx_rates (
  rate_date   DATE NOT NULL,
  base        CHAR(3) NOT NULL,                      -- 'USD'
  quote       CHAR(3) NOT NULL,                      -- 'PLN'  (ile quote za 1 base)
  rate        NUMERIC(24,10) NOT NULL,
  source      TEXT NOT NULL,                         -- 'NBP_A','ECB','XTB_IMPLIED'
  PRIMARY KEY (rate_date, base, quote, source)
);
CREATE TABLE etf_exposures (
  security_id BIGINT NOT NULL REFERENCES securities,
  as_of       DATE NOT NULL,
  dimension   TEXT NOT NULL CHECK (dimension IN ('COUNTRY','SECTOR','CURRENCY','ASSET_CLASS','TOP_HOLDING')),
  key         TEXT NOT NULL,
  weight      NUMERIC(9,6) NOT NULL,                 -- ułamek 0..1
  source      TEXT NOT NULL,
  PRIMARY KEY (security_id, as_of, dimension, key, source)
);

-- ---------------------------------------------------------------
-- 7. WARSTWA POCHODNA (do skasowania i przeliczenia w każdej chwili)
-- ---------------------------------------------------------------
CREATE TABLE derived_lots (
  lot_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id          BIGINT NOT NULL,
  listing_id          BIGINT NOT NULL,
  open_txn_id         BIGINT NOT NULL REFERENCES transactions,   -- BUY, który otworzył partię
  broker_position_ref TEXT,
  opened_at           TIMESTAMPTZ NOT NULL,
  qty_as_reported     NUMERIC(24,8) NOT NULL,        -- ilość dokładnie jak w transakcji BUY (dane XTB, bez korekt)
  price_as_reported   NUMERIC(24,8) NOT NULL,        -- cena dokładnie jak w transakcji BUY
  split_factor        NUMERIC(18,8) NOT NULL DEFAULT 1,   -- iloczyn splitów po dacie zakupu
  qty_initial         NUMERIC(24,8) NOT NULL,        -- SKORYGOWANA = qty_as_reported * split_factor
  price_adjusted      NUMERIC(24,8) NOT NULL,        -- SKORYGOWANA = price_as_reported / split_factor
  qty_remaining       NUMERIC(24,8) NOT NULL,        -- skorygowana
  cost_cash_initial   NUMERIC(24,8) NOT NULL,        -- faktycznie zapłacona gotówka (PLN); split jej NIE zmienia
  method              TEXT NOT NULL CHECK (method IN ('BROKER_POSITION','FIFO')),
  engine_version      TEXT NOT NULL,
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE derived_lot_closures (
  closure_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lot_id          BIGINT NOT NULL REFERENCES derived_lots ON DELETE CASCADE,
  sell_txn_id     BIGINT NOT NULL REFERENCES transactions,
  qty_closed      NUMERIC(24,8) NOT NULL,
  cost_cash       NUMERIC(24,8) NOT NULL,
  proceeds_cash   NUMERIC(24,8) NOT NULL,
  realized_pnl    NUMERIC(24,8) NOT NULL,
  bcp_id          BIGINT REFERENCES broker_closed_positions,   -- dopasowany wiersz brokera (kontrola)
  pnl_diff_vs_broker NUMERIC(24,8)
);

-- Przykładowe widoki (definicje do doprecyzowania w fazie implementacji)
-- ---------------------------------------------------------------
-- 8. TRANSFERY MIĘDZY WŁASNYMI RACHUNKAMI (dokument 10)
--    Klasyfikacja jest OSOBNA od księgi (transactions się nie zmienia).
-- ---------------------------------------------------------------
CREATE TABLE account_identifiers (
  identifier_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id    BIGINT NOT NULL REFERENCES accounts,
  scheme        TEXT NOT NULL CHECK (scheme IN ('BROKER_ACCOUNT_NO','XTB_PAYMENT_BA','IBAN','WALLET_ID','CARD_ALIAS','OTHER')),
  value         TEXT NOT NULL,
  UNIQUE (scheme, value)
);

-- Kontrahent zaobserwowany w opisach transakcji; decyzja „mój / cudzy” zapisywana RAZ na kontrahenta.
CREATE TABLE counterparties (
  counterparty_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scheme            TEXT NOT NULL,                 -- 'XTB_PAYMENT_BA', 'IBAN', ...
  value             TEXT NOT NULL,                 -- numer rachunku płatniczego (przykład: '1234567')
  display_name      TEXT,
  ownership_status  TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (ownership_status IN
                      ('UNKNOWN','OWN_CANDIDATE','OWN_CONFIRMED','EXTERNAL_CONFIRMED')),
  mapped_account_id BIGINT REFERENCES accounts,    -- własny rachunek (np. płatniczy, TRANSFERS_ONLY)
  decided_by        TEXT,                          -- 'user' | 'rule:<id>'
  decided_at        TIMESTAMPTZ,
  evidence          JSONB,
  UNIQUE (scheme, value)
);

CREATE TABLE scopes (                              -- zakresy obliczeń: 'INVESTMENT', 'NET_WORTH', 'ACCOUNT:<id>'
  scope_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code     TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'CUSTOM' CHECK (kind IN ('BROKER','INVESTMENT_ALL','NET_WORTH','ACCOUNT','RETIREMENT','CUSTOM'))
);
CREATE TABLE scope_accounts (
  scope_id   BIGINT NOT NULL REFERENCES scopes,
  account_id BIGINT NOT NULL REFERENCES accounts,
  PRIMARY KEY (scope_id, account_id)
);

CREATE TABLE transfer_groups (
  transfer_group_id TEXT PRIMARY KEY,
  status            TEXT NOT NULL CHECK (status IN ('MATCHED','AWAITING_LEG','OWN_UNTRACKED','AMBIGUOUS')),
  method            TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE classification_rules (                -- reguły kanałów per adapter (np. komentarz zawiera 'OT|DIR:I2B')
  rule_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_id    SMALLINT REFERENCES brokers,
  pattern      TEXT NOT NULL,                      -- regex na opisie transakcji
  extracts     JSONB NOT NULL,                     -- które grupy to schemat/wartość kontrahenta
  proposes     TEXT NOT NULL CHECK (proposes IN ('OWN_CANDIDATE','EXTERNAL')),
  enabled      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE flow_classification (
  classification_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  txn_id                  BIGINT NOT NULL REFERENCES transactions,
  counterparty_id         BIGINT REFERENCES counterparties,
  counterparty_account_id BIGINT REFERENCES accounts,        -- gdy kontrahent = znany rachunek własny
  transfer_group_id       TEXT REFERENCES transfer_groups,
  flow_class              TEXT NOT NULL CHECK (flow_class IN
                            ('INTERNAL_TRANSFER','OWN_TRANSFER_UNTRACKED','EXTERNAL','UNCLASSIFIED')),
  method                  TEXT NOT NULL CHECK (method IN
                            ('L1_EXPLICIT_REF','L2_LEG_MATCH','L3_COUNTERPARTY_REGISTRY','L4_USER','DEFAULT')),
  confidence              TEXT CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
  rule_id                 BIGINT REFERENCES classification_rules,
  decided_by              TEXT,
  classifier_version      TEXT NOT NULL,
  is_current              BOOLEAN NOT NULL DEFAULT TRUE,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_flow_current ON flow_classification (txn_id) WHERE is_current;

-- RĘCZNE migawki salda (nigdy wyliczane z transferów). Dwa różne cele:
--   VALUATION – wartość rachunku, który NIE ma własnej księgi (bank, karta/eWallet, lokata, PPK); wchodzi do majątku całkowitego;
--   CONTROL   – saldo rachunku z księgą (np. migawka z aplikacji TR) tylko do porównania z rekonstrukcją; NIGDY nie wchodzi do wartości.
-- Rozdzielenie chroni przed podwójnym liczeniem tej samej gotówki w majątku całkowitym.
CREATE TABLE account_balance_snapshots (
  snapshot_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id  BIGINT NOT NULL REFERENCES accounts,
  as_of       TIMESTAMPTZ NOT NULL,
  balance     NUMERIC(24,8) NOT NULL,
  currency    CHAR(3) NOT NULL,
  purpose     TEXT NOT NULL DEFAULT 'VALUATION' CHECK (purpose IN ('VALUATION','CONTROL')),
  source      TEXT NOT NULL CHECK (source IN ('MANUAL','IMPORT')),
  note        TEXT,
  UNIQUE (account_id, as_of, purpose)
);

CREATE FUNCTION fn_guard_valuation_snapshot() RETURNS trigger AS $$
BEGIN
  IF NEW.purpose = 'VALUATION' AND EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = NEW.account_id AND a.tracking_mode = 'FULL') THEN
    RAISE EXCEPTION 'Migawka VALUATION jest niedozwolona dla rachunku FULL (wartość pochodzi z księgi); użyj purpose = CONTROL'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.purpose = 'VALUATION' AND EXISTS (SELECT 1 FROM retirement_accounts r WHERE r.account_id = NEW.account_id) THEN
    RAISE EXCEPTION 'Wartość rachunku PPK pochodzi wyłącznie z migawek PPK (ppk_snapshots); migawka VALUATION w account_balance_snapshots jest niedozwolona'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_guard_valuation_snapshot BEFORE INSERT OR UPDATE ON account_balance_snapshots
  FOR EACH ROW EXECUTE FUNCTION fn_guard_valuation_snapshot();

-- WOLNE ŚRODKI per konto (słownik pojęć, dokument 9): suma cash_amount = wiersz 'Total' w eksporcie XTB
CREATE VIEW v_free_funds AS
  SELECT t.account_id, a.display_name, a.product_type, t.cash_currency,
         SUM(t.cash_amount) AS free_funds, MAX(t.occurred_at) AS last_operation_at
  FROM transactions t JOIN accounts a USING (account_id)
  WHERE t.status = 'ACTIVE' GROUP BY t.account_id, a.display_name, a.product_type, t.cash_currency;

CREATE VIEW v_cash_balance AS
  SELECT account_id, cash_currency, SUM(cash_amount) AS balance
  FROM transactions WHERE status = 'ACTIVE' GROUP BY account_id, cash_currency;

CREATE VIEW v_external_flows AS     -- wpłaty/wypłaty ZEWNĘTRZNE: decyduje klasa przepływu, nie typ źródłowy
  SELECT t.account_id, t.occurred_at, t.cash_amount, t.cash_currency     -- (przelewy do/z własnego rachunku płatniczego i wewnętrzne są wyłączone)
  FROM transactions t JOIN flow_classification fc ON fc.txn_id = t.txn_id AND fc.is_current
  WHERE t.status = 'ACTIVE' AND fc.flow_class = 'EXTERNAL';

-- ---------------------------------------------------------------
-- 9. WIELU BROKERÓW: dane początkowe i widoki zakresów (dokument 11, dokument 3 §3.10)
-- ---------------------------------------------------------------
INSERT INTO brokers (code, name) VALUES ('XTB', 'XTB'), ('TR', 'Trade Republic');

-- Zakresy domyślne. Skład (scope_accounts) uzupełnia aplikacja przy rejestracji rachunków:
--   BROKER:*         = rachunki inwestycyjne danego brokera;
--   INVESTMENT_ALL   = wszystkie rachunki inwestycyjne wszystkich brokerów ORAZ rachunki PPK (łączny portfel inwestycyjny);
--   PPK              = rachunki PPK (Oszczędności emerytalne), prezentowane osobno od brokerów;
--   NET_WORTH        = INVESTMENT_ALL + rachunki płatnicze/bankowe/lokaty/PPK (ręczne wyceny).
INSERT INTO scopes (code, name, kind) VALUES
  ('BROKER:XTB',     'Portfel XTB',                  'BROKER'),
  ('BROKER:TR',      'Portfel Trade Republic',       'BROKER'),
  ('INVESTMENT_ALL', 'Łączny portfel inwestycyjny',  'INVESTMENT_ALL'),
  ('NET_WORTH',      'Majątek całkowity',            'NET_WORTH'),
  ('PPK',            'Oszczędności emerytalne (PPK)', 'RETIREMENT');

-- Ilość otwartych pozycji w zakresie, zagregowana PO PAPIERZE (ten sam fundusz pod różnymi tickerami / u różnych brokerów)
CREATE VIEW v_open_quantity_by_scope_security AS
SELECT s.code AS scope_code, l.security_id, SUM(dl.qty_remaining) AS qty_remaining
FROM scopes s
JOIN scope_accounts sa ON sa.scope_id = s.scope_id
JOIN derived_lots dl   ON dl.account_id = sa.account_id AND dl.qty_remaining > 0
JOIN listings l        ON l.listing_id = dl.listing_id
GROUP BY s.code, l.security_id;

-- ---------------------------------------------------------------
-- 9a. PPK – „Oszczędności emerytalne” (dokument 13)
--     Uproszczony moduł oparty na RĘCZNYCH, NIEZMIENNYCH migawkach wartości rachunku. Bez API, bez historii transakcji, bez XIRR/TWR.
--     Fundusz (program) i jego strategia są oddzielone od rachunku i od wycen. Kwoty: NUMERIC(18,2) (grosz), nigdy float.
--     Migawki są stanami NARASTAJĄCYMI (wpłaty od początku), nie nowymi wpłatami – nie wolno ich sumować między datami.
-- ---------------------------------------------------------------
CREATE TABLE retirement_funds (                    -- fundusz / program, np. „Fundusz Zdefiniowanej Daty 2055”; dane niezależne od rachunku
  fund_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id SMALLINT NOT NULL REFERENCES brokers,         -- instytucja prowadząca (TFI); niczego nie zakładamy na sztywno
  name           TEXT NOT NULL,
  target_year    SMALLINT CHECK (target_year BETWEEN 2000 AND 2200),
  fund_kind      TEXT NOT NULL DEFAULT 'TARGET_DATE' CHECK (fund_kind IN ('TARGET_DATE','OTHER')),
  currency       CHAR(3) NOT NULL DEFAULT 'PLN',
  status         TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED')),
  notes          TEXT,
  UNIQUE (institution_id, name)
);

-- Źródła informacji o funduszu (KID, prospekt, karta funduszu, raport…). Dane o strategii i alokacji MUSZĄ pochodzić z oficjalnych źródeł.
CREATE TABLE fund_source_documents (
  document_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id   SMALLINT REFERENCES brokers,       -- wydawca (np. TFI)
  fund_id          BIGINT REFERENCES retirement_funds, -- opcjonalnie: dokument dotyczy konkretnego funduszu
  kind             TEXT NOT NULL CHECK (kind IN ('KID','PROSPECTUS','FUND_CARD','PERIODIC_REPORT','REGULATIONS','INSTITUTION_WEBSITE','OTHER')),
  title            TEXT NOT NULL,
  url              TEXT,
  publication_date DATE,                              -- data dokumentu
  retrieved_on     DATE NOT NULL,                     -- kiedy go pobrano / przeczytano
  is_official      BOOLEAN NOT NULL,                  -- dokument instytucji prowadzącej / regulatora (a nie opinia lub szacunek)
  note             TEXT
);

-- Słownik klas aktywów (klasyfikacja, a nie fakty o jakimkolwiek funduszu). Rozszerzalny.
CREATE TABLE retirement_asset_classes (
  class_code TEXT PRIMARY KEY,
  name_pl    TEXT NOT NULL,
  parent_code TEXT REFERENCES retirement_asset_classes,
  sort_order SMALLINT NOT NULL
);
INSERT INTO retirement_asset_classes (class_code, name_pl, sort_order) VALUES
  ('EQUITY',       'Akcje', 1),
  ('DEBT',         'Obligacje i inne instrumenty dłużne', 2),
  ('MONEY_MARKET', 'Instrumenty rynku pieniężnego i gotówka', 3),
  ('OTHER',        'Inne klasy aktywów', 4);

-- DOCELOWA POLITYKA INWESTYCYJNA funduszu: wersje w czasie (historia strategii). Każda wersja ma źródło i datę aktualności.
CREATE TABLE retirement_fund_strategies (
  strategy_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fund_id                BIGINT NOT NULL REFERENCES retirement_funds,
  valid_from             DATE NOT NULL,
  valid_to               DATE,
  description            TEXT,                         -- opis polityki inwestycyjnej (z dokumentu)
  glide_path_description TEXT,                         -- opis mechanizmu ograniczania ryzyka w miarę zbliżania się do roku docelowego
  parameters             JSONB,
  source_document_id     BIGINT NOT NULL REFERENCES fund_source_documents,
  as_of_date             DATE NOT NULL,                -- data aktualności informacji w źródle
  entered_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (fund_id, valid_from)
);

CREATE TABLE retirement_strategy_target_allocations (  -- docelowa alokacja (udział akcji, obligacji itd.), % z dokładnością do 0,0001
  strategy_id BIGINT NOT NULL REFERENCES retirement_fund_strategies,
  class_code  TEXT NOT NULL REFERENCES retirement_asset_classes,
  target_pct  NUMERIC(7,4) NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
  min_pct     NUMERIC(7,4) CHECK (min_pct BETWEEN 0 AND 100),
  max_pct     NUMERIC(7,4) CHECK (max_pct BETWEEN 0 AND 100),
  CHECK ((min_pct IS NULL OR min_pct <= target_pct) AND (max_pct IS NULL OR target_pct <= max_pct)),
  PRIMARY KEY (strategy_id, class_code)
);

CREATE TABLE retirement_strategy_glide_path (          -- docelowy udział klasy aktywów w zależności od liczby lat do roku docelowego
  strategy_id          BIGINT NOT NULL REFERENCES retirement_fund_strategies,
  years_to_target_from SMALLINT NOT NULL,              -- np. 40 (daleko od roku docelowego)
  years_to_target_to   SMALLINT NOT NULL,              -- np. 21; zakres włącznie
  class_code           TEXT NOT NULL REFERENCES retirement_asset_classes,
  target_pct           NUMERIC(7,4) NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
  CHECK (years_to_target_from >= years_to_target_to),
  PRIMARY KEY (strategy_id, years_to_target_from, class_code)
);

-- RZECZYWISTA ALOKACJA AKTYWÓW na określony dzień (raport okresowy, karta funduszu) – osobno od polityki docelowej
CREATE TABLE retirement_fund_allocation_reports (
  report_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fund_id            BIGINT NOT NULL REFERENCES retirement_funds,
  as_of_date         DATE NOT NULL,                    -- dzień, na który podano alokację
  source_document_id BIGINT NOT NULL REFERENCES fund_source_documents,
  entered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (fund_id, as_of_date, source_document_id)
);
CREATE TABLE retirement_fund_allocation_report_lines (
  report_id  BIGINT NOT NULL REFERENCES retirement_fund_allocation_reports,
  class_code TEXT NOT NULL REFERENCES retirement_asset_classes,
  actual_pct NUMERIC(7,4) NOT NULL CHECK (actual_pct BETWEEN 0 AND 100),
  PRIMARY KEY (report_id, class_code)
);

-- Nie wpisujemy niezweryfikowanych wartości jako faktów: strategia i alokacja wymagają OFICJALNEGO źródła dotyczącego tego funduszu
CREATE FUNCTION fn_guard_official_source() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM fund_source_documents d WHERE d.document_id = NEW.source_document_id AND d.is_official
                 AND (d.fund_id IS NULL OR d.fund_id = NEW.fund_id)) THEN
    RAISE EXCEPTION 'Strategia i alokacja funduszu muszą pochodzić z oficjalnego źródła tego funduszu (fund_source_documents.is_official)'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_strategy_official_source BEFORE INSERT OR UPDATE ON retirement_fund_strategies
  FOR EACH ROW EXECUTE FUNCTION fn_guard_official_source();
CREATE TRIGGER trg_allocation_official_source BEFORE INSERT OR UPDATE ON retirement_fund_allocation_reports
  FOR EACH ROW EXECUTE FUNCTION fn_guard_official_source();

CREATE TABLE retirement_fund_unit_prices (         -- HAK ROZBUDOWY (nieużywany teraz): ceny jednostek uczestnictwa, gdy pojawi się historia jednostek
  fund_id    BIGINT NOT NULL REFERENCES retirement_funds,
  price_date DATE NOT NULL,
  unit_price NUMERIC(24,8) NOT NULL,
  source     TEXT NOT NULL,
  PRIMARY KEY (fund_id, price_date, source)
);

CREATE TABLE retirement_accounts (                 -- konkretny rachunek PPK użytkownika (jeden lub wiele)
  account_id           BIGINT PRIMARY KEY REFERENCES accounts,   -- waluta rachunku: accounts.base_currency; instytucja: accounts.broker_id
  fund_id              BIGINT NOT NULL REFERENCES retirement_funds,
  joined_on            DATE NOT NULL,                            -- data przystąpienia do programu
  investing_started_on DATE,                                     -- opcjonalna data rozpoczęcia inwestowania
  status               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','CLOSED','TRANSFERRED')),
  notes                TEXT
);

CREATE FUNCTION fn_guard_retirement_account() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = NEW.account_id
                 AND a.product_type IN ('PPK','RETIREMENT_OTHER') AND a.account_class = 'RETIREMENT' AND a.tracking_mode = 'BALANCE_ONLY') THEN
    RAISE EXCEPTION 'Rachunek oszczędności emerytalnych musi mieć product_type = PPK (lub RETIREMENT_OTHER), account_class = RETIREMENT, tracking_mode = BALANCE_ONLY' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM retirement_funds f JOIN accounts a ON a.account_id = NEW.account_id
                 WHERE f.fund_id = NEW.fund_id AND f.institution_id = a.broker_id) THEN
    RAISE EXCEPTION 'Fundusz i rachunek PPK muszą należeć do tej samej instytucji' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_guard_retirement_account BEFORE INSERT OR UPDATE ON retirement_accounts
  FOR EACH ROW EXECUTE FUNCTION fn_guard_retirement_account();

-- Słownik rodzajów składek: kto płaci (pracownik / pracodawca / państwo). Rozszerzalny.
CREATE TABLE ppk_contribution_categories (
  category_code TEXT PRIMARY KEY,
  name_pl       TEXT NOT NULL,
  payer         TEXT NOT NULL CHECK (payer IN ('EMPLOYEE','EMPLOYER','STATE')),
  is_additional BOOLEAN NOT NULL DEFAULT FALSE,
  sort_order    SMALLINT NOT NULL
);
INSERT INTO ppk_contribution_categories (category_code, name_pl, payer, is_additional, sort_order) VALUES
  ('WELCOME',             'Składka powitalna',                'STATE',    FALSE, 1),
  ('EMPLOYER_BASIC',      'Składka podstawowa pracodawcy',    'EMPLOYER', FALSE, 2),
  ('EMPLOYEE_BASIC',      'Składka podstawowa pracownika',    'EMPLOYEE', FALSE, 3),
  ('EMPLOYER_ADDITIONAL', 'Składka dodatkowa pracodawcy',     'EMPLOYER', TRUE,  4),
  ('EMPLOYEE_ADDITIONAL', 'Składka dodatkowa pracownika',     'EMPLOYEE', TRUE,  5),
  ('STATE_ANNUAL',        'Dopłata roczna',                   'STATE',    FALSE, 6);

-- Normalizacja etykiety z tabeli źródłowej: małe litery, bez polskich znaków i interpunkcji, pojedyncze spacje
CREATE FUNCTION fn_ppk_normalize_label(p TEXT) RETURNS TEXT AS $$
  SELECT btrim(regexp_replace(lower(translate(COALESCE(p, ''), 'ąćęłńóśźżĄĆĘŁŃÓŚŹŻ', 'acelnoszzacelnoszz')), '[^a-z0-9]+', ' ', 'g'))
$$ LANGUAGE sql IMMUTABLE;

-- Aliasy nazw kategorii (zmiany nazw w tabeli źródłowej): globalne (institution_id NULL) i per instytucja
CREATE TABLE ppk_category_aliases (
  alias_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id   SMALLINT REFERENCES brokers,
  alias_normalized TEXT NOT NULL,
  category_code    TEXT NOT NULL REFERENCES ppk_contribution_categories,
  created_by       TEXT NOT NULL DEFAULT 'seed'
);
CREATE UNIQUE INDEX uq_ppk_alias ON ppk_category_aliases (COALESCE(institution_id::INT, 0), alias_normalized);
INSERT INTO ppk_category_aliases (alias_normalized, category_code)
SELECT fn_ppk_normalize_label(a.label), a.code FROM (VALUES
  ('Składka powitalna', 'WELCOME'), ('Wpłata powitalna', 'WELCOME'),
  ('Składka podstawowa pracodawcy', 'EMPLOYER_BASIC'), ('Wpłata podstawowa pracodawcy', 'EMPLOYER_BASIC'),
  ('Składka podstawowa pracownika', 'EMPLOYEE_BASIC'), ('Wpłata podstawowa pracownika', 'EMPLOYEE_BASIC'),
  ('Składka dodatkowa pracodawcy', 'EMPLOYER_ADDITIONAL'), ('Wpłata dodatkowa pracodawcy', 'EMPLOYER_ADDITIONAL'),
  ('Składka dodatkowa pracownika', 'EMPLOYEE_ADDITIONAL'), ('Wpłata dodatkowa pracownika', 'EMPLOYEE_ADDITIONAL'),
  ('Dopłata roczna', 'STATE_ANNUAL'), ('Dopłata roczna od państwa', 'STATE_ANNUAL')) AS a(label, code);

CREATE FUNCTION fn_ppk_resolve_category(p_label TEXT, p_institution INTEGER DEFAULT NULL) RETURNS TEXT AS $$
  SELECT category_code FROM ppk_category_aliases
  WHERE alias_normalized = fn_ppk_normalize_label(p_label) AND (institution_id = p_institution OR institution_id IS NULL)
  ORDER BY (institution_id IS NULL), alias_id LIMIT 1
$$ LANGUAGE sql STABLE;

-- Kwota z tabeli źródłowej -> NUMERIC (grosze). Obsługa: spacje (także twarde), symbole walut, przecinek/kropka dziesiętna, separatory tysięcy.
-- NULL = brak danych / zapis niejednoznaczny (np. „1.234”) / niepoprawny – NIGDY zero. Wartość zerowa to jawne „0,00”.
CREATE FUNCTION fn_ppk_parse_amount(p TEXT) RETURNS NUMERIC AS $$
DECLARE s TEXT; neg BOOLEAN := FALSE; dec_sep TEXT; dec_part TEXT; thou TEXT; body TEXT;
BEGIN
  IF p IS NULL THEN RETURN NULL; END IF;
  s := regexp_replace(p, '[\u00a0\u202f\u2009\u2007\u200b\s]+', '', 'g');
  s := regexp_replace(s, '(PLN|zł|ZŁ|zl|ZL|EUR|USD|CHF|GBP|€|\$|£)', '', 'gi');
  s := replace(replace(s, chr(8722), '-'), chr(8211), '-');
  IF s = '' OR lower(s) IN ('-', '—', 'n/d', 'b.d.', 'bd', 'brak') THEN RETURN NULL; END IF;
  IF left(s, 1) IN ('-', '+') THEN neg := (left(s, 1) = '-'); s := substr(s, 2); END IF;
  IF s ~ '^\d+$' THEN
    body := s;
  ELSIF s ~ '^\d{1,3}([.,]\d{3}){2,}$' AND (s !~ '\.' OR s !~ ',') THEN
    body := regexp_replace(s, '[.,]', '', 'g');                  -- 1.234.567 -> same separatory tysięcy
  ELSIF s ~ '^\d{1,3}[.,]\d{3}$' THEN
    RETURN NULL;                                                  -- „1.234” / „1,234”: niejednoznaczne (tysiące czy ułamek) – wymaga potwierdzenia
  ELSIF s ~ '^\d+[.,]\d{1,2}$' THEN
    body := replace(s, ',', '.');
  ELSIF s ~ '^\d{1,3}([.,]\d{3})+[.,]\d{1,2}$' THEN
    dec_sep := substring(s FROM '([.,])\d{1,2}$');
    dec_part := substring(s FROM '[.,](\d{1,2})$');
    thou := left(s, length(s) - length(dec_part) - 1);
    IF position(dec_sep IN thou) > 0 OR (thou ~ '\.' AND thou ~ ',') THEN RETURN NULL; END IF;
    body := regexp_replace(thou, '[.,]', '', 'g') || '.' || dec_part;
  ELSE
    RETURN NULL;
  END IF;
  RETURN CASE WHEN neg THEN -(body::NUMERIC) ELSE body::NUMERIC END;
END $$ LANGUAGE plpgsql IMMUTABLE;

-- MIGAWKI: niezmienne, tylko dopisywane. Każda ma datę wyceny, datę wprowadzenia i zatwierdzenie przez użytkownika.
CREATE TABLE ppk_snapshots (
  snapshot_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id              BIGINT NOT NULL REFERENCES retirement_accounts,
  valuation_date          DATE NOT NULL,
  entered_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at             TIMESTAMPTZ NOT NULL,                 -- migawka powstaje dopiero po zatwierdzeniu (wcześniej: ppk_import_drafts)
  approved_by             TEXT NOT NULL CHECK (btrim(approved_by) <> ''),
  institution_total_value NUMERIC(18,2) NOT NULL CHECK (institution_total_value >= 0),   -- NADRZĘDNA wartość podana przez instytucję
  categories_value_sum    NUMERIC(18,2) NOT NULL,               -- suma znanych wartości kategorii
  categories_paid_in_sum  NUMERIC(18,2) NOT NULL,               -- suma znanych wpłat (narastająco)
  lines_count             SMALLINT NOT NULL,
  values_complete         BOOLEAN NOT NULL,                     -- każda kategoria ma znaną wartość i jest co najmniej jedna kategoria
  paid_in_complete        BOOLEAN NOT NULL,
  control_difference      NUMERIC(18,2),                        -- institution_total_value − categories_value_sum; NIE jest korygowana; NULL przy niepełnych wartościach
  source                  TEXT,
  note                    TEXT,
  content_hash            CHAR(64) NOT NULL,                    -- skrót treści bez daty (wykrywanie ponownego importu tych samych liczb)
  snapshot_hash           CHAR(64) NOT NULL,                    -- skrót treści z datą wyceny
  supersedes_snapshot_id  BIGINT REFERENCES ppk_snapshots,      -- poprawka migawki z tego samego dnia = NOWA wersja, stara zostaje
  units_total             NUMERIC(24,8),                        -- HAKI ROZBUDOWY (nieużywane teraz): liczba jednostek, cena jednostki
  unit_price              NUMERIC(24,8),
  unit_price_date         DATE,
  CONSTRAINT ck_ppk_control CHECK ((values_complete AND control_difference = institution_total_value - categories_value_sum)
                                OR (NOT values_complete AND control_difference IS NULL)),
  UNIQUE (account_id, snapshot_hash)
);

-- Wiersze migawki: wiersz kategorii istnieje tylko, gdy kategoria była w tabeli. BRAK wiersza = brak danych (NIE zero).
-- NULL w paid_in / value = brak tej wartości; jawne 0,00 = rzeczywista wartość zerowa.
CREATE TABLE ppk_snapshot_lines (
  snapshot_id   BIGINT NOT NULL REFERENCES ppk_snapshots,
  category_code TEXT NOT NULL REFERENCES ppk_contribution_categories,
  paid_in       NUMERIC(18,2) CHECK (paid_in IS NULL OR paid_in >= 0),      -- wpłaty NARASTAJĄCO od początku
  value         NUMERIC(18,2) CHECK (value IS NULL OR value >= 0),          -- aktualna wartość kategorii
  origin        TEXT NOT NULL DEFAULT 'ENTERED' CHECK (origin IN ('ENTERED','CARRIED_FORWARD_CONFIRMED')),
                                                                            -- CARRIED_FORWARD: użytkownik potwierdził przeniesienie niezmienionej wartości
  raw_label     TEXT, raw_paid_in TEXT, raw_value TEXT,                     -- oryginał z wklejonej tabeli (audyt)
  PRIMARY KEY (snapshot_id, category_code)
);

CREATE FUNCTION fn_ppk_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Migawki PPK są niezmienne (tylko dopisywanie); popraw dane nową migawką' USING ERRCODE = 'restrict_violation';
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_ppk_snapshots_immutable BEFORE UPDATE OR DELETE ON ppk_snapshots     FOR EACH ROW EXECUTE FUNCTION fn_ppk_immutable();
CREATE TRIGGER trg_ppk_lines_immutable     BEFORE UPDATE OR DELETE ON ppk_snapshot_lines FOR EACH ROW EXECUTE FUNCTION fn_ppk_immutable();

-- Spójność nagłówka z wierszami (sprawdzana przy zatwierdzeniu transakcji, niezależnie od ścieżki zapisu)
CREATE FUNCTION fn_ppk_check_snapshot() RETURNS trigger AS $$
DECLARE r RECORD;
BEGIN
  SELECT COALESCE(SUM(l.value), 0) AS sv, COALESCE(SUM(l.paid_in), 0) AS sp, COUNT(*) AS n,
         (COUNT(*) > 0 AND bool_and(l.value IS NOT NULL)) AS vc, (COUNT(*) > 0 AND bool_and(l.paid_in IS NOT NULL)) AS pc
    INTO r FROM ppk_snapshot_lines l WHERE l.snapshot_id = NEW.snapshot_id;
  IF r.sv <> NEW.categories_value_sum OR r.sp <> NEW.categories_paid_in_sum OR r.n <> NEW.lines_count
     OR r.vc <> NEW.values_complete OR r.pc <> NEW.paid_in_complete THEN
    RAISE EXCEPTION 'Migawka PPK % jest niespójna z wierszami (sumy lub kompletność)', NEW.snapshot_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER trg_ppk_snapshot_consistency AFTER INSERT ON ppk_snapshots
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_ppk_check_snapshot();

-- Szkice importu: wklejona tabela + rozpoznane dane do PODGLĄDU. Szkic NIE jest migawką i nie wpływa na żadne widoki.
CREATE TABLE ppk_import_drafts (
  draft_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id  BIGINT NOT NULL REFERENCES retirement_accounts,
  raw_text    TEXT NOT NULL,
  parsed      JSONB,                                  -- rozpoznane wiersze (kategoria, kwoty, ostrzeżenia, nierozpoznane etykiety)
  warnings    JSONB,
  status      TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED','DISCARDED')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at  TIMESTAMPTZ,
  snapshot_id BIGINT REFERENCES ppk_snapshots
);

-- Zatwierdzenie = jedyna droga do powstania migawki. Zwraca (snapshot_id, outcome): CREATED | DUPLICATE | CORRECTION.
--   p_lines: tablica JSON [{"category":"WELCOME","paid_in":"250.00","value":"440.40","origin":"ENTERED","raw_label":"..."}];
--   kwoty jako tekst z kropką (po normalizacji fn_ppk_parse_amount), maks. 2 miejsca; brak klucza / null = brak danych; "0.00" = zero.
CREATE FUNCTION fn_ppk_commit_snapshot(p_account_id BIGINT, p_valuation_date DATE, p_total_value NUMERIC, p_lines JSONB,
                                       p_approved_by TEXT, p_source TEXT DEFAULT NULL, p_note TEXT DEFAULT NULL, p_draft_id BIGINT DEFAULT NULL)
RETURNS TABLE (snapshot_id BIGINT, outcome TEXT) AS $$
#variable_conflict use_column
DECLARE
  v_content TEXT; v_chash TEXT; v_shash TEXT; v_latest RECORD; v_same_date BIGINT; v_id BIGINT; v_dup BIGINT;
  v_sum_val NUMERIC(18,2); v_sum_paid NUMERIC(18,2); v_n INT; v_vc BOOLEAN; v_pc BOOLEAN; v_super BIGINT := NULL; v_outcome TEXT := 'CREATED';
BEGIN
  IF p_approved_by IS NULL OR btrim(p_approved_by) = '' THEN
    RAISE EXCEPTION 'Migawka PPK nie może powstać bez zatwierdzenia przez użytkownika (approved_by)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM retirement_accounts ra WHERE ra.account_id = p_account_id) THEN
    RAISE EXCEPTION 'Rachunek % nie jest rachunkiem PPK', p_account_id;
  END IF;
  IF p_total_value IS NULL OR p_total_value < 0 OR p_total_value <> round(p_total_value, 2) THEN
    RAISE EXCEPTION 'Całkowita wartość rachunku musi być kwotą >= 0 z dokładnością do grosza';
  END IF;
  IF p_lines IS NULL OR jsonb_typeof(p_lines) <> 'array' THEN RAISE EXCEPTION 'Wiersze migawki muszą być tablicą JSON'; END IF;
  PERFORM pg_advisory_xact_lock(p_account_id);

  IF EXISTS (SELECT 1 FROM jsonb_to_recordset(p_lines) AS x(category TEXT)
             WHERE x.category IS NULL OR NOT EXISTS (SELECT 1 FROM ppk_contribution_categories c WHERE c.category_code = x.category)) THEN
    RAISE EXCEPTION 'Nieznana kategoria składki w wierszach migawki';
  END IF;
  IF (SELECT COUNT(*) <> COUNT(DISTINCT x.category) FROM jsonb_to_recordset(p_lines) AS x(category TEXT)) THEN
    RAISE EXCEPTION 'Powtórzona kategoria składki w wierszach migawki';
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_to_recordset(p_lines) AS x(category TEXT, paid_in TEXT, value TEXT)
             WHERE (x.paid_in IS NOT NULL AND x.paid_in !~ '^\d+(\.\d{1,2})?$') OR (x.value IS NOT NULL AND x.value !~ '^\d+(\.\d{1,2})?$')) THEN
    RAISE EXCEPTION 'Kwota w wierszu migawki musi być nieujemna, z kropką dziesiętną i maksymalnie 2 miejscami po przecinku (grosz)';
  END IF;

  SELECT COALESCE(string_agg(x.category || ':' || COALESCE(to_char(x.paid_in::NUMERIC, 'FM999999999990.00'), 'null') || ':' ||
                             COALESCE(to_char(x.value::NUMERIC, 'FM999999999990.00'), 'null'), ';' ORDER BY x.category), '')
    INTO v_content FROM jsonb_to_recordset(p_lines) AS x(category TEXT, paid_in TEXT, value TEXT);
  v_content := p_account_id || '|' || to_char(p_total_value, 'FM999999999990.00') || '|' || v_content;
  v_chash := encode(sha256(convert_to(v_content, 'UTF8')), 'hex');
  v_shash := encode(sha256(convert_to(p_valuation_date::TEXT || '|' || v_content, 'UTF8')), 'hex');

  -- ponowny import tej samej migawki: identyczna treść jak najnowsza (także z inną datą) albo identyczna migawka z tą samą datą
  SELECT s.snapshot_id, s.content_hash INTO v_latest FROM v_ppk_latest_snapshot s WHERE s.account_id = p_account_id;
  IF FOUND AND v_latest.content_hash = v_chash THEN
    RETURN QUERY SELECT v_latest.snapshot_id, 'DUPLICATE'::TEXT; RETURN;
  END IF;
  SELECT s.snapshot_id INTO v_dup FROM ppk_snapshots s WHERE s.account_id = p_account_id AND s.snapshot_hash = v_shash;
  IF v_dup IS NOT NULL THEN RETURN QUERY SELECT v_dup, 'DUPLICATE'::TEXT; RETURN; END IF;

  SELECT e.snapshot_id INTO v_same_date FROM v_ppk_effective_snapshots e WHERE e.account_id = p_account_id AND e.valuation_date = p_valuation_date;
  IF v_same_date IS NOT NULL THEN v_super := v_same_date; v_outcome := 'CORRECTION'; END IF;

  SELECT COALESCE(SUM(x.value::NUMERIC), 0), COALESCE(SUM(x.paid_in::NUMERIC), 0), COUNT(*),
         (COUNT(*) > 0 AND bool_and(x.value IS NOT NULL)), (COUNT(*) > 0 AND bool_and(x.paid_in IS NOT NULL))
    INTO v_sum_val, v_sum_paid, v_n, v_vc, v_pc FROM jsonb_to_recordset(p_lines) AS x(category TEXT, paid_in TEXT, value TEXT);

  INSERT INTO ppk_snapshots (account_id, valuation_date, approved_at, approved_by, institution_total_value, categories_value_sum, categories_paid_in_sum,
                             lines_count, values_complete, paid_in_complete, control_difference, source, note, content_hash, snapshot_hash, supersedes_snapshot_id)
  VALUES (p_account_id, p_valuation_date, now(), p_approved_by, p_total_value, v_sum_val, v_sum_paid, v_n, v_vc, v_pc,
          CASE WHEN v_vc THEN p_total_value - v_sum_val END, p_source, p_note, v_chash, v_shash, v_super)
  RETURNING ppk_snapshots.snapshot_id INTO v_id;

  INSERT INTO ppk_snapshot_lines (snapshot_id, category_code, paid_in, value, origin, raw_label, raw_paid_in, raw_value)
  SELECT v_id, x.category, x.paid_in::NUMERIC, x.value::NUMERIC, COALESCE(x.origin, 'ENTERED'), x.raw_label, x.raw_paid_in, x.raw_value
    FROM jsonb_to_recordset(p_lines) AS x(category TEXT, paid_in TEXT, value TEXT, origin TEXT, raw_label TEXT, raw_paid_in TEXT, raw_value TEXT);

  IF p_draft_id IS NOT NULL THEN
    UPDATE ppk_import_drafts d SET status = 'APPROVED', decided_at = now(), snapshot_id = v_id WHERE d.draft_id = p_draft_id AND d.account_id = p_account_id;
  END IF;
  RETURN QUERY SELECT v_id, v_outcome;
END $$ LANGUAGE plpgsql;

-- Migawki obowiązujące: dla danej daty wyceny ostatnia wersja (nie zastąpiona poprawką)
CREATE VIEW v_ppk_effective_snapshots AS
SELECT s.* FROM ppk_snapshots s
WHERE NOT EXISTS (SELECT 1 FROM ppk_snapshots n WHERE n.supersedes_snapshot_id = s.snapshot_id);

-- Najnowsza obowiązująca migawka rachunku (wg daty wyceny). Migawki są STANAMI NARASTAJĄCYMI – używamy najnowszej, a nie sumy migawek.
CREATE VIEW v_ppk_latest_snapshot AS
SELECT DISTINCT ON (account_id) * FROM v_ppk_effective_snapshots
ORDER BY account_id, valuation_date DESC, entered_at DESC, snapshot_id DESC;

-- Stan bieżący rachunku PPK: wartość (nadrzędna wartość instytucji), wpłaty wg płatnika, różnica, kontrola, kompletność
CREATE VIEW v_ppk_account_current AS
SELECT s.account_id, s.snapshot_id, s.valuation_date, s.institution_total_value AS value, a.base_currency AS currency,
       s.categories_value_sum, s.control_difference, s.values_complete, s.paid_in_complete, s.lines_count,
       (CURRENT_DATE - s.valuation_date) AS days_since_valuation,
       ((CURRENT_DATE - s.valuation_date) > 45) AS is_stale,       -- nieświeża wycena (próg 45 dni; tylko flaga, nie blokuje)
       SUM(l.paid_in) FILTER (WHERE c.payer = 'EMPLOYEE') AS paid_in_employee,
       SUM(l.paid_in) FILTER (WHERE c.payer = 'EMPLOYER') AS paid_in_employer,
       SUM(l.paid_in) FILTER (WHERE c.payer = 'STATE')    AS paid_in_state,
       SUM(l.paid_in)                                      AS paid_in_total,
       CASE WHEN s.paid_in_complete THEN s.institution_total_value - SUM(l.paid_in) END AS indicative_change_vs_paid_in,   -- orientacyjna zmiana wartości względem wpłat, NIE precyzyjny wynik
       CASE WHEN s.paid_in_complete THEN s.institution_total_value - COALESCE(SUM(l.paid_in) FILTER (WHERE c.payer = 'EMPLOYEE'), 0) END AS value_minus_employee_paid_in,
       CASE WHEN NOT s.values_complete THEN 'INCOMPLETE'
            WHEN s.control_difference = 0 THEN 'EXACT'
            WHEN abs(s.control_difference) <= 0.01 * s.lines_count THEN 'ROUNDING'
            ELSE 'MISMATCH' END AS control_status,
       EXISTS (SELECT 1 FROM v_ppk_effective_snapshots e2
               JOIN ppk_snapshot_lines l2 ON l2.snapshot_id = e2.snapshot_id
               WHERE e2.account_id = s.account_id AND e2.valuation_date < s.valuation_date
                 AND NOT EXISTS (SELECT 1 FROM ppk_snapshot_lines l3 WHERE l3.snapshot_id = s.snapshot_id AND l3.category_code = l2.category_code)) AS has_missing_categories
FROM v_ppk_latest_snapshot s
JOIN accounts a ON a.account_id = s.account_id
LEFT JOIN ppk_snapshot_lines l ON l.snapshot_id = s.snapshot_id
LEFT JOIN ppk_contribution_categories c ON c.category_code = l.category_code
GROUP BY s.account_id, s.snapshot_id, s.valuation_date, s.institution_total_value, s.categories_value_sum, s.control_difference,
         s.values_complete, s.paid_in_complete, s.lines_count, a.base_currency;

-- Historia (np. wykres): wartość i wpłaty narastająco na każdą datę wyceny
CREATE VIEW v_ppk_history AS
SELECT account_id, snapshot_id, valuation_date, institution_total_value AS value, categories_paid_in_sum AS paid_in_total, paid_in_complete,
       CASE WHEN paid_in_complete THEN institution_total_value - categories_paid_in_sum END AS indicative_change_vs_paid_in
FROM v_ppk_effective_snapshots;

-- Zmiany między kolejnymi stanami (OBLICZANE, nigdy nie zapisywane jako wpłaty bez potwierdzenia użytkownika)
CREATE VIEW v_ppk_snapshot_deltas AS
SELECT account_id, snapshot_id, valuation_date, institution_total_value AS value,
       LAG(valuation_date) OVER w AS prev_valuation_date,
       institution_total_value - LAG(institution_total_value) OVER w AS delta_value,
       categories_paid_in_sum - LAG(categories_paid_in_sum) OVER w AS delta_paid_in_total,
       (paid_in_complete AND COALESCE(LAG(paid_in_complete) OVER w, FALSE)) AS delta_paid_in_reliable,
       'zmiana stanu narastającego; NIE jest zapisana jako wpłata' AS note
FROM v_ppk_effective_snapshots
WINDOW w AS (PARTITION BY account_id ORDER BY valuation_date);

CREATE VIEW v_ppk_category_deltas AS
SELECT account_id, snapshot_id, valuation_date, category_code, paid_in, value, prev_valuation_date,
       paid_in - prev_paid_in AS delta_paid_in, value - prev_value AS delta_value,
       'zmiana stanu narastającego; NIE jest zapisana jako wpłata' AS note
FROM (SELECT e.account_id, e.snapshot_id, e.valuation_date, l.category_code, l.paid_in, l.value,
             LAG(e.valuation_date) OVER w AS prev_valuation_date, LAG(l.paid_in) OVER w AS prev_paid_in, LAG(l.value) OVER w AS prev_value
      FROM v_ppk_effective_snapshots e JOIN ppk_snapshot_lines l ON l.snapshot_id = e.snapshot_id
      WINDOW w AS (PARTITION BY e.account_id, l.category_code ORDER BY e.valuation_date)) d;

-- Ostrzeżenia (nie blokują zapisu): wpłaty narastające zmalały, kategoria zniknęła, niezgodność kontrolna, dane niepełne
CREATE VIEW v_ppk_snapshot_warnings AS
SELECT snapshot_id, account_id, 'PAID_IN_DECREASED' AS warning, category_code AS detail FROM v_ppk_category_deltas WHERE delta_paid_in < 0
UNION ALL
SELECT e.snapshot_id, e.account_id, 'CATEGORY_MISSING', p.category_code
FROM v_ppk_effective_snapshots e
JOIN LATERAL (SELECT DISTINCT l.category_code FROM v_ppk_effective_snapshots pe JOIN ppk_snapshot_lines l ON l.snapshot_id = pe.snapshot_id
              WHERE pe.account_id = e.account_id AND pe.valuation_date < e.valuation_date) p ON TRUE
WHERE NOT EXISTS (SELECT 1 FROM ppk_snapshot_lines x WHERE x.snapshot_id = e.snapshot_id AND x.category_code = p.category_code)
UNION ALL
SELECT snapshot_id, account_id, 'CONTROL_MISMATCH', control_difference::TEXT FROM v_ppk_effective_snapshots
WHERE values_complete AND abs(control_difference) > 0.01 * lines_count
UNION ALL
SELECT snapshot_id, account_id, 'INCOMPLETE_DATA', CASE WHEN NOT values_complete THEN 'values' ELSE 'paid_in' END FROM v_ppk_effective_snapshots
WHERE NOT values_complete OR NOT paid_in_complete;

-- Wpłaty PPK w zakresie (stany narastające z NAJNOWSZYCH migawek): wchodzą do kapitału netto zakresu (pracownik + pracodawca + państwo)
CREATE VIEW v_scope_ppk_contributions AS
SELECT s.code AS scope_code, p.currency,
       SUM(p.paid_in_employee) AS ppk_employee, SUM(p.paid_in_employer) AS ppk_employer, SUM(p.paid_in_state) AS ppk_state,
       SUM(p.paid_in_total) AS ppk_total, SUM(p.value) AS ppk_value,
       COUNT(*) FILTER (WHERE NOT p.paid_in_complete) AS ppk_incomplete_accounts,
       COUNT(*) FILTER (WHERE p.is_stale) AS ppk_stale_accounts
FROM scopes s
JOIN scope_accounts sa ON sa.scope_id = s.scope_id
JOIN v_ppk_account_current p ON p.account_id = sa.account_id
GROUP BY s.code, p.currency;

-- ---------------------------------------------------------------
-- 10. WARTOŚĆ, KAPITAŁ NETTO I WYNIK (dokument 12): jedna definicja, każdy rachunek liczony DOKŁADNIE RAZ
--
--   wartość portfela brokera = wartość papierów + gotówka (wolne środki; oprocentowana gotówka jest aktywem inwestycyjnym)
--   kapitał netto zakresu    = suma przepływów przez GRANICĘ zakresu (wpłaty/wypłaty, także z/do własnego rachunku bankowego)
--   wynik inwestycyjny       = wartość portfela − kapitał netto   (tylko zakresy inwestycyjne; NIE dla majątku całkowitego)
-- ---------------------------------------------------------------

-- Ostatnia WYCENA rachunku bez księgi: ręczna migawka VALUATION albo (dla PPK) najnowsza migawka PPK. Jedno źródło na rachunek.
CREATE VIEW v_latest_valuation_snapshot AS
SELECT DISTINCT ON (account_id) account_id, as_of, balance, currency
FROM (SELECT account_id, as_of, balance, currency FROM account_balance_snapshots WHERE purpose = 'VALUATION'
      UNION ALL
      SELECT l.account_id, l.valuation_date::TIMESTAMPTZ, l.institution_total_value, a.base_currency
      FROM v_ppk_latest_snapshot l JOIN accounts a ON a.account_id = l.account_id) u
ORDER BY account_id, as_of DESC;

-- Czy rachunek ma WYCENĘ? FULL: z księgi. Pozostałe: tylko gdy istnieje wycena (migawka VALUATION lub migawka PPK).
CREATE VIEW v_account_is_valued AS
SELECT a.account_id,
       (a.tracking_mode = 'FULL' OR EXISTS (SELECT 1 FROM v_latest_valuation_snapshot s WHERE s.account_id = a.account_id)) AS is_valued
FROM accounts a;

-- Wartość papierów rachunku z księgą. Pierwszeństwo: świeża migawka brokera > ceny rynkowe > nieświeża migawka > brak pozycji (0) > brak wyceny (NULL)
CREATE VIEW v_account_securities_value AS
WITH snap AS (
  SELECT b.account_id, b.as_of, SUM(b.value_cash) AS value
  FROM broker_position_snapshots b
  JOIN (SELECT account_id, MAX(as_of) AS as_of FROM broker_position_snapshots WHERE row_level = 'INSTRUMENT' GROUP BY account_id) m
       ON m.account_id = b.account_id AND m.as_of = b.as_of
  WHERE b.row_level = 'INSTRUMENT'
  GROUP BY b.account_id, b.as_of),
px AS (
  SELECT dl.account_id,
         SUM(dl.qty_remaining * lp.close_price * lp.fx) AS value,
         COUNT(*) FILTER (WHERE lp.close_price IS NULL OR lp.fx IS NULL) AS unpriced
  FROM derived_lots dl
  JOIN accounts a ON a.account_id = dl.account_id
  LEFT JOIN LATERAL (
       SELECT p.close_price,
              CASE WHEN p.currency = a.base_currency THEN 1::numeric
                   ELSE (SELECT f.rate FROM fx_rates f WHERE f.base = p.currency AND f.quote = a.base_currency
                         ORDER BY f.rate_date DESC LIMIT 1) END AS fx
       FROM prices p WHERE p.listing_id = dl.listing_id ORDER BY p.price_date DESC LIMIT 1) lp ON TRUE
  WHERE dl.qty_remaining > 0
  GROUP BY dl.account_id),
lots AS (SELECT DISTINCT account_id FROM derived_lots WHERE qty_remaining > 0)
SELECT a.account_id,
       CASE
         WHEN snap.account_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.account_id AND t.status = 'ACTIVE'
                                                         AND t.txn_type IN ('BUY','SELL') AND t.occurred_at > snap.as_of) THEN snap.value
         WHEN px.account_id IS NOT NULL AND px.unpriced = 0 THEN px.value
         WHEN snap.account_id IS NOT NULL THEN snap.value
         WHEN lots.account_id IS NULL THEN 0
         ELSE NULL END AS securities_value,
       CASE
         WHEN snap.account_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.account_id AND t.status = 'ACTIVE'
                                                         AND t.txn_type IN ('BUY','SELL') AND t.occurred_at > snap.as_of) THEN 'SNAPSHOT'
         WHEN px.account_id IS NOT NULL AND px.unpriced = 0 THEN 'PRICES'
         WHEN snap.account_id IS NOT NULL THEN 'SNAPSHOT_STALE'
         WHEN lots.account_id IS NULL THEN 'NO_POSITIONS'
         ELSE 'MISSING' END AS value_source
FROM accounts a
LEFT JOIN snap ON snap.account_id = a.account_id
LEFT JOIN px   ON px.account_id   = a.account_id
LEFT JOIN lots ON lots.account_id = a.account_id
WHERE a.tracking_mode = 'FULL';

-- Wartość KAŻDEGO rachunku dokładnie raz (jedna wartość i jedno źródło).
--   FULL: wolne środki z księgi + wartość papierów;   BALANCE_ONLY / TRANSFERS_ONLY: ostatnia migawka VALUATION, a bez niej NULL.
CREATE VIEW v_account_valuation AS
SELECT a.account_id, a.account_class, a.tracking_mode, a.base_currency AS currency,
       CASE WHEN a.tracking_mode = 'FULL' THEN COALESCE(ff.free_funds, 0) END AS free_funds,
       CASE WHEN a.tracking_mode = 'FULL' THEN sv.securities_value END AS securities_value,
       CASE WHEN a.tracking_mode = 'FULL' THEN COALESCE(ff.free_funds, 0) + sv.securities_value
            ELSE ls.balance END AS value,
       CASE WHEN a.tracking_mode = 'FULL' THEN 'LEDGER:' || sv.value_source
            WHEN ls.account_id IS NOT NULL THEN 'SNAPSHOT_MANUAL' ELSE 'NONE' END AS value_source,
       (CASE WHEN a.tracking_mode = 'FULL' THEN COALESCE(ff.free_funds, 0) + sv.securities_value ELSE ls.balance END) IS NOT NULL AS is_valued,
       EXISTS (SELECT 1 FROM v_free_funds f2 WHERE f2.account_id = a.account_id AND f2.cash_currency <> a.base_currency) AS has_foreign_currency_cash
FROM accounts a
LEFT JOIN v_free_funds ff ON ff.account_id = a.account_id AND ff.cash_currency = a.base_currency
LEFT JOIN v_account_securities_value sv ON sv.account_id = a.account_id
LEFT JOIN v_latest_valuation_snapshot ls ON ls.account_id = a.account_id AND ls.currency = a.base_currency;

CREATE VIEW v_scope_value AS
SELECT s.code AS scope_code, s.kind, v.currency,
       SUM(v.free_funds)       AS free_funds,
       SUM(v.securities_value) AS securities_value,
       SUM(v.value)            AS value,
       SUM(v.value) FILTER (WHERE v.account_class IN ('INVESTMENT','RETIREMENT')) AS investment_value,     -- aktywa inwestycyjne: brokerzy + PPK
       SUM(v.value) FILTER (WHERE v.account_class = 'RETIREMENT') AS retirement_value,
       SUM(v.value) FILTER (WHERE v.account_class NOT IN ('INVESTMENT','RETIREMENT')) AS other_assets_value,
       COUNT(*) FILTER (WHERE NOT v.is_valued) AS unvalued_accounts,
       COUNT(*) AS accounts
FROM scopes s
JOIN scope_accounts sa ON sa.scope_id = s.scope_id
JOIN v_account_valuation v ON v.account_id = sa.account_id
GROUP BY s.code, s.kind, v.currency;

CREATE VIEW v_scope_free_funds AS
SELECT scope_code, currency, free_funds FROM v_scope_value;

-- Przepływy przez GRANICĘ zakresu. Przepływ jest wewnętrzny tylko wtedy, gdy kontrahent jest rachunkiem własnym NALEŻĄCYM do zakresu
-- ORAZ WYCENIONYM (inaczej jego wartość nie jest po stronie wartości i powstałaby sztuczna strata).
-- Transfer papierów w naturze liczony wg wartości z dnia transferu (gross_amount).
CREATE VIEW v_scope_boundary_flows AS
SELECT s.code AS scope_code, t.txn_id, t.account_id, t.occurred_at,
       CASE t.txn_type WHEN 'TRANSFER_IN_KIND' THEN t.gross_amount
                       WHEN 'TRANSFER_OUT_KIND' THEN -t.gross_amount
                       ELSE t.cash_amount END AS amount,
       t.cash_currency AS currency, t.txn_type, fc.flow_class, fc.counterparty_account_id
FROM scopes s
JOIN scope_accounts sa ON sa.scope_id = s.scope_id
JOIN transactions t    ON t.account_id = sa.account_id AND t.status = 'ACTIVE'
LEFT JOIN flow_classification fc ON fc.txn_id = t.txn_id AND fc.is_current
WHERE t.txn_type IN ('CASH_DEPOSIT','CASH_WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT','TRANSFER_IN_KIND','TRANSFER_OUT_KIND')
  AND NOT EXISTS (SELECT 1 FROM scope_accounts sb
                  JOIN v_account_is_valued iv ON iv.account_id = sb.account_id AND iv.is_valued
                  WHERE sb.scope_id = s.scope_id AND sb.account_id = fc.counterparty_account_id);

CREATE VIEW v_scope_contributions AS
WITH f AS (
  SELECT scope_code, currency, SUM(amount) AS flows_net,
         SUM(amount) FILTER (WHERE COALESCE(flow_class, 'UNCLASSIFIED') IN ('EXTERNAL','UNCLASSIFIED')) AS from_external,
         SUM(amount) FILTER (WHERE flow_class IN ('OWN_TRANSFER_UNTRACKED','INTERNAL_TRANSFER')) AS from_own_accounts
  FROM v_scope_boundary_flows GROUP BY scope_code, currency)
SELECT COALESCE(f.scope_code, p.scope_code) AS scope_code, COALESCE(f.currency, p.currency) AS currency,
       COALESCE(f.flows_net, 0) + COALESCE(p.ppk_total, 0) AS net_contributions,      -- przepływy przez granicę + wpłaty PPK (pracownik, pracodawca, państwo)
       f.from_external, f.from_own_accounts,
       p.ppk_employee, p.ppk_employer, p.ppk_state, p.ppk_total, p.ppk_value,
       COALESCE(p.ppk_incomplete_accounts, 0) AS ppk_incomplete_accounts,
       COALESCE(p.ppk_stale_accounts, 0) AS ppk_stale_accounts
FROM f FULL JOIN v_scope_ppk_contributions p ON p.scope_code = f.scope_code AND p.currency = f.currency;

-- Wynik inwestycyjny: tylko zakresy inwestycyjne (BROKER, INVESTMENT_ALL, ACCOUNT, CUSTOM). Dla NET_WORTH wynik NIE jest definiowany
-- (rachunek bankowy zawiera także pieniądze niezwiązane z inwestowaniem).
CREATE VIEW v_scope_result AS
SELECT v.scope_code, v.currency, v.free_funds, v.securities_value, v.value AS portfolio_value,
       COALESCE(c.net_contributions, 0) AS net_contributions,
       v.value - COALESCE(c.net_contributions, 0) AS investment_result,        -- tożsamość wartość − kapitał; przy PPK w zakresie jest ORIENTACYJNY
       -- część precyzyjna: bez PPK (księga brokerów)
       (v.value - COALESCE(c.ppk_value, 0)) - (COALESCE(c.net_contributions, 0) - COALESCE(c.ppk_total, 0)) AS investment_result_excl_retirement,
       -- część orientacyjna: PPK – różnica wartość − łączne wpłaty (nie uwzględnia czasu wpłat, kosztów ani korekt; NIE jest wynikiem inwestycyjnym)
       CASE WHEN c.ppk_value IS NOT NULL AND COALESCE(c.ppk_incomplete_accounts, 0) = 0 THEN c.ppk_value - c.ppk_total END AS retirement_indicative_change,
       (c.ppk_value IS NOT NULL) AS result_is_indicative,
       (v.unvalued_accounts = 0 AND COALESCE(c.ppk_incomplete_accounts, 0) = 0) AS is_complete,
       (COALESCE(c.ppk_stale_accounts, 0) > 0) AS has_stale_retirement_valuation
FROM v_scope_value v
LEFT JOIN v_scope_contributions c ON c.scope_code = v.scope_code AND c.currency = v.currency
WHERE v.kind <> 'NET_WORTH';

-- Majątek całkowity: każdy rachunek raz; rachunki bez wyceny NIE są szacowane (flaga + liczba), a przepływy do nich zostają widoczne osobno.
CREATE VIEW v_net_worth AS
SELECT v.scope_code, v.currency, v.value AS net_worth_valued, v.investment_value, v.retirement_value, v.other_assets_value,
       v.unvalued_accounts, (v.unvalued_accounts = 0) AS is_complete,
       COALESCE(c.from_own_accounts, 0) AS net_inflow_from_unvalued_own_accounts
FROM v_scope_value v
LEFT JOIN v_scope_contributions c ON c.scope_code = v.scope_code AND c.currency = v.currency
WHERE v.kind = 'NET_WORTH';

-- Udział PPK w łącznym portfelu inwestycyjnym (aktywa inwestycyjne = brokerzy + PPK)
CREATE VIEW v_ppk_share_of_investments AS
SELECT scope_code, currency, retirement_value AS ppk_value, investment_value,
       retirement_value / NULLIF(investment_value, 0) AS ppk_share
FROM v_scope_value WHERE kind = 'INVESTMENT_ALL';

-- „Nie wpisuj ponownie, jeśli się nie zmieniło”: PODPOWIEDZI do formularza (nie zapisują niczego; do zapisu wymagane potwierdzenie użytkownika).
--   Wartość kategorii zmienia się z rynkiem, więc podpowiadamy ją TYLKO gdy ostatnio było 0,00 / 0,00; wpłaty narastające można podpowiedzieć zawsze (mogły się nie zmienić).
CREATE VIEW v_ppk_carry_forward_candidates AS
SELECT s.account_id, s.snapshot_id AS from_snapshot_id, s.valuation_date AS from_valuation_date, l.category_code,
       l.paid_in AS candidate_paid_in,
       CASE WHEN l.paid_in = 0 AND l.value = 0 THEN l.value END AS candidate_value,
       CASE WHEN l.paid_in = 0 AND l.value = 0 THEN 'CARRY_ZERO' WHEN l.paid_in IS NOT NULL THEN 'PAID_IN_ONLY' ELSE 'NONE' END AS suggestion
FROM v_ppk_latest_snapshot s JOIN ppk_snapshot_lines l ON l.snapshot_id = s.snapshot_id;

-- Strategia: obowiązująca polityka docelowa vs ostatnia RZECZYWISTA alokacja (osobne tabele, osobne źródła i daty)
CREATE VIEW v_fund_current_strategy AS
SELECT DISTINCT ON (fund_id) * FROM retirement_fund_strategies
WHERE valid_from <= CURRENT_DATE AND (valid_to IS NULL OR valid_to >= CURRENT_DATE)
ORDER BY fund_id, valid_from DESC;

CREATE VIEW v_fund_latest_actual_allocation AS
SELECT DISTINCT ON (r.fund_id, l.class_code) r.fund_id, l.class_code, l.actual_pct, r.as_of_date AS actual_as_of_date, r.source_document_id AS actual_source_document_id
FROM retirement_fund_allocation_reports r JOIN retirement_fund_allocation_report_lines l ON l.report_id = r.report_id
ORDER BY r.fund_id, l.class_code, r.as_of_date DESC;

CREATE VIEW v_fund_target_vs_actual AS
SELECT COALESCE(s.fund_id, a.fund_id) AS fund_id, COALESCE(ta.class_code, a.class_code) AS class_code,
       ta.target_pct, s.as_of_date AS target_as_of_date, s.source_document_id AS target_source_document_id,
       a.actual_pct, a.actual_as_of_date, a.actual_source_document_id,
       a.actual_pct - ta.target_pct AS deviation_pct
FROM v_fund_current_strategy s
JOIN retirement_strategy_target_allocations ta ON ta.strategy_id = s.strategy_id
FULL JOIN v_fund_latest_actual_allocation a ON a.fund_id = s.fund_id AND a.class_code = ta.class_code;

-- Kontrola sum alokacji (flaga, nie blokada: raporty mogą sumować się do 99,9 przez zaokrąglenia)
CREATE VIEW v_fund_allocation_checks AS
SELECT 'TARGET' AS allocation_kind, ta.strategy_id AS ref_id, SUM(ta.target_pct) AS sum_pct, abs(SUM(ta.target_pct) - 100) <= 0.5 AS sum_ok
FROM retirement_strategy_target_allocations ta GROUP BY ta.strategy_id
UNION ALL
SELECT 'ACTUAL', l.report_id, SUM(l.actual_pct), abs(SUM(l.actual_pct) - 100) <= 0.5
FROM retirement_fund_allocation_report_lines l GROUP BY l.report_id;
