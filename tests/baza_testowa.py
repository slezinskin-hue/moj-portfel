"""
Wspolna baza testow: tymczasowy PostgreSQL (pakiet pgserver), schemat = wszystkie migracje aplikacji, pomocnicze funkcje (konta, transakcje,
odczyt widokow). Kazdy modul testow dostaje wlasna, swieza baze; kazdy test dziala w transakcji wycofywanej na koncu.
"""
import itertools, os, shutil, sys, tempfile, unittest
from decimal import Decimal as D

import pgserver, psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("INVAPP_DATA_DIR", os.path.join(tempfile.gettempdir(), "invapp_app_testdata"))   # testy nigdy nie uzywaja danych uzytkownika
from invapp.db.migrate import concatenated_sql          # schemat = wszystkie migracje invapp/db/migrations po kolei
SQL = open(os.environ["INVAPP_SCHEMA"], encoding="utf-8").read() if os.environ.get("INVAPP_SCHEMA") else concatenated_sql()   # INVAPP_SCHEMA: tylko dla testu mutacyjnego
_srv = _conn = None
ROLE_SQL = "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investing_app') THEN CREATE ROLE investing_app NOLOGIN; END IF; END $$;"   # rola aplikacji (migracje 0003/0006 nadaja jej uprawnienia)


_db_no = itertools.count(1)


def setUpModule():
    """Jeden serwer na proces testow; KAZDY modul testow dostaje wlasna, swieza baze (wczesniej kolejny modul kasowal katalog dzialajacego serwera)."""
    global _srv, _conn
    if _srv is None:
        # ten plik bywa zaimportowany dwa razy (jako tests.test_model_principles i test_model_principles) - wtedy NIE kasujemy katalogu dzialajacego serwera
        d = os.path.join(tempfile.gettempdir(), f"invapp_app_pgdata_tests_{os.getpid()}")
        if not any(str(p).endswith(os.path.basename(d)) for p in pgserver.PostgresServer._instances):
            shutil.rmtree(d, ignore_errors=True)
        _srv = pgserver.get_server(d, cleanup_mode="delete")
    import uuid
    name = f"t{next(_db_no)}_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_srv.get_uri()); admin.autocommit = True
    admin.cursor().execute(f"DROP DATABASE IF EXISTS {name}"); admin.cursor().execute(f"CREATE DATABASE {name}"); admin.close()
    _conn = psycopg2.connect(_srv.get_uri(name))
    cur = _conn.cursor(); cur.execute(ROLE_SQL); cur.execute(SQL); _conn.commit()


def tearDownModule():
    _conn.close()


class Base(unittest.TestCase):
    """Pomocnicze funkcje: konta, zakresy, transakcje, klasyfikacja, odczyt widoków."""

    def setUp(self):
        self.cur = _conn.cursor()
        self.seq = itertools.count(1)
        self.xtb = self.one("select broker_id from financial_institutions where code='XTB'")
        self.tr = self.one("select broker_id from financial_institutions where code='TR'")

    def tearDown(self):
        _conn.rollback()

    # --- SQL helpers
    def one(self, q, a=None):
        self.cur.execute(q, a); return self.cur.fetchone()[0]

    def rows(self, q, a=None):
        self.cur.execute(q, a); cols = [c[0] for c in self.cur.description]
        return [dict(zip(cols, r)) for r in self.cur.fetchall()]

    def account(self, broker, ext, cls="INVESTMENT", mode="FULL", ptype="STANDARD", tax="PIT38", scopes=()):
        a = self.one("""insert into accounts(broker_id,external_no,display_name,product_type,account_class,tracking_mode,tax_regime)
                        values (%s,%s,%s,%s,%s,%s,%s) returning account_id""", (broker, ext, ext, ptype, cls, mode, tax))
        for code in scopes:
            self.cur.execute("insert into scope_accounts select scope_id,%s from scopes where code=%s", (a, code))
        return a

    def tx(self, acct, broker, typ, amount, ts, scheme="T", **kw):
        n = next(self.seq)
        return self.one("""insert into transactions(account_id,broker_id,txn_type,occurred_at,cash_amount,cash_currency,external_id,external_id_scheme,
                                                     dedup_key,source_kind,listing_id,quantity,price,price_currency,gross_amount,status)
                           values (%s,%s,%s,%s,%s,'PLN',%s,%s,%s,'BROKER_IMPORT',%s,%s,%s,%s,%s,%s) returning txn_id""",
                        (acct, broker, typ, ts, amount, f"{scheme}{n}-{acct}", scheme, f"k:{scheme}:{n}:{acct}", kw.get("listing"), kw.get("qty"),
                         kw.get("price"), "PLN" if kw.get("price") is not None else None, kw.get("gross"), kw.get("status", "ACTIVE")))

    def classify(self, txn, flow_class, counterparty_account=None):
        self.cur.execute("""insert into flow_classification(txn_id,counterparty_account_id,flow_class,method,classifier_version)
                            values (%s,%s,%s,'L4_USER','test')""", (txn, counterparty_account, flow_class))

    def security(self, name="Fundusz"):
        s = self.one("insert into securities(name,asset_class) values (%s,'ETF') returning security_id", (name,))
        return self.one("insert into listings(security_id,display_name,trading_currency) values (%s,%s,'PLN') returning listing_id", (s, name))

    def lot(self, acct, listing, open_txn, qty, remaining=None):
        self.cur.execute("""insert into derived_lots(account_id,listing_id,open_txn_id,opened_at,qty_as_reported,price_as_reported,split_factor,qty_initial,
                                                       price_adjusted,qty_remaining,cost_cash_initial,method,engine_version)
                            values (%s,%s,%s,now(),%s,1,1,%s,1,%s,1,'BROKER_POSITION','test')""", (acct, listing, open_txn, qty, qty, qty if remaining is None else remaining))

    def price(self, listing, value):
        self.cur.execute("insert into prices(listing_id,price_date,close_price,currency,source) values (%s,'2026-11-01',%s,'PLN','t')", (listing, value))

    def snapshot_positions(self, acct, listing, value, as_of="2026-11-01T12:00Z"):
        b = self.one("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,status)
                        values (%s,'f',repeat('a',64),'p','1','COMMITTED') returning batch_id""", (self.xtb,))
        self.cur.execute("""insert into broker_position_snapshots(batch_id,account_id,as_of,listing_id,row_level,volume,value_cash,net_profit_cash)
                            values (%s,%s,%s,%s,'INSTRUMENT',1,%s,0)""", (b, acct, as_of, listing, value))

    def balance_snapshot(self, acct, balance, purpose="VALUATION", as_of="2026-11-02T00:00Z"):
        self.cur.execute("""insert into account_balance_snapshots(account_id,as_of,balance,currency,purpose,source)
                            values (%s,%s,%s,'PLN',%s,'MANUAL')""", (acct, as_of, balance, purpose))

    # --- odczyt widoków
    def result(self, scope):
        r = self.rows("select * from v_scope_result where scope_code=%s", (scope,))
        return r[0] if r else None

    def networth(self):
        r = self.rows("select * from v_net_worth where scope_code='NET_WORTH'"); return r[0] if r else None

    def assertResult(self, scope, value, capital, result):
        r = self.result(scope)
        self.assertIsNotNone(r, f"brak wiersza wyniku dla {scope}")
        self.assertEqual((r["portfolio_value"], r["net_contributions"], r["investment_result"]), (D(value), D(capital), D(result)), scope)
