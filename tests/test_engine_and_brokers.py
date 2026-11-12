"""
Testy czesci, ktore powstaly przy przenosinach do wersji instalowalnej:
* wbudowana baza (pierwsze uruchomienie: role i losowe hasla, scram zamiast trust, migracje; restart na tych samych danych; kopia zapasowa),
* lista brokerow (rozpoznawanie plikow, rejestracja nowego brokera),
* XTB: pytanie o walute instrumentow .PL i uzupelnianie walut ze specyfikacji pobranej PO imporcie (pierwszy import bez internetu).
"""
import json, os, shutil, sys, tempfile, unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import baza_testowa as tm  # noqa: E402
from invapp import brokers, config  # noqa: E402
from invapp.brokers.base import Broker  # noqa: E402
from invapp.web import importing  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class TestEmbeddedDatabase(unittest.TestCase):
    """Wlasny katalog danych (osobny serwer), zeby nie mieszac z baza testow modelu."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="invapp_engine_")
        cls.old = os.environ.get(config.DATA_DIR_ENV)
        os.environ[config.DATA_DIR_ENV] = cls.dir
        from invapp.db import engine
        cls.engine = engine
        engine.stop()

    @classmethod
    def tearDownClass(cls):
        cls.engine.stop()
        if cls.old is None:
            os.environ.pop(config.DATA_DIR_ENV, None)
        else:
            os.environ[config.DATA_DIR_ENV] = cls.old
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_first_start_creates_roles_passwords_and_schema_and_restart_reuses_them(self):
        import psycopg2
        e = self.engine
        st = e.start()
        sec = json.loads(config.secrets_file().read_text(encoding="utf-8"))
        self.assertEqual(set(sec), {"postgres", e.OWNER, e.APP})
        self.assertTrue(all(len(v) >= 20 for v in sec.values()))                       # losowe hasla, uzytkownik nic nie wpisuje
        self.assertEqual(st["host"], "127.0.0.1")
        with self.assertRaises(psycopg2.OperationalError):                              # bez hasla nie wolno (scram zamiast trust)
            psycopg2.connect(host=st["host"], port=st["port"], dbname=e.DB_NAME, user="postgres", connect_timeout=3)
        c = e.connect("app", readonly=True); cur = c.cursor()
        cur.execute("select count(*) from schema_migrations"); n = cur.fetchone()[0]
        from invapp.db import migrate
        self.assertEqual(n, len(migrate.migration_files()))
        c.close()
        c = e.connect("app"); cur = c.cursor()
        with self.assertRaises(psycopg2.Error):                                         # rola aplikacji nie zapisuje ksiegi
            cur.execute("insert into accounts(broker_id,external_no,display_name,product_type,tax_regime) values (1,'x','x','STANDARD','PIT38')")
        c.rollback(); c.close()
        # restart programu na tych samych danych
        e.stop(); self.assertFalse(e.is_running())
        e.start()
        self.assertEqual(json.loads(config.secrets_file().read_text(encoding="utf-8")), sec)
        c = e.connect("owner"); cur = c.cursor(); cur.execute("select current_user"); self.assertEqual(cur.fetchone()[0], e.OWNER); c.close()
        from invapp.db import backup
        out = backup.create("test")
        self.assertTrue(out.exists() and out.stat().st_size > 1000)
        self.assertFalse((config.pgdata_dir() / "log").exists())                          # dziennik serwera poza katalogiem danych
        self.assertTrue((config.logs_dir() / "baza.log").exists())
        # nieczyste zamkniecie (zabity proces serwera, jak przy zamknieciu okna): kolejny start odtwarza baze i dziala
        import psutil
        pid = e._postmaster(config.pgdata_dir())[0]
        p = psutil.Process(pid); p.kill(); p.wait(10); del p
        e._state.update(server=None, host=None, port=None, secrets=None, started_here=False, stop_on_exit=False)
        e.start()
        c = e.connect("owner"); cur = c.cursor(); cur.execute("select count(*) from schema_migrations"); self.assertEqual(cur.fetchone()[0], n); c.close()


class FakeBroker(Broker):
    code, key, name, accept = "TESTB", "testb", "Testowy broker", (".txt",)

    def matches(self, filename, head):
        return filename.endswith(".txt") and head.startswith(b"TESTB")


class TestBrokerRegistry(tm.Base):
    def test_upload_detection(self):
        csv_head = open(os.path.join(ROOT, "export", "Eksport transakcji.csv"), "rb").read(2048) if os.path.exists(os.path.join(ROOT, "export", "Eksport transakcji.csv")) else None
        self.assertEqual(brokers.for_upload("IKE_12345678_2006-01-01_2026-11-11.xlsx", b"PK\x03\x04").code, "XTB")
        self.assertIsNone(brokers.for_upload("raport.xlsx", b"PK\x03\x04"))                          # nazwa nie z XTB
        if csv_head:
            self.assertEqual(brokers.for_upload("Eksport transakcji.csv", csv_head).code, "TR")
        self.assertIsNone(brokers.for_upload("inne.csv", b'"a","b"\n'))
        self.assertIn("Trade Republic", importing.unsupported_message())

    def test_new_broker_is_registered_with_its_own_scope(self):
        brokers.BROKERS.append(FakeBroker())
        try:
            brokers.ensure_registered(tm._conn); brokers.ensure_registered(tm._conn)               # idempotentnie
            self.assertEqual(self.one("select count(*) from financial_institutions where code='TESTB'"), 1)
            self.assertEqual(self.one("select kind from scopes where code='BROKER:TESTB'"), "BROKER")
            self.assertEqual(brokers.for_upload("plik.txt", b"TESTB...").code, "TESTB")
            from invapp.web import dashboard
            self.assertIn("testb", dashboard._scopes())                                          # zakladka powstaje z listy brokerow
        finally:
            brokers.BROKERS.pop()
            self.cur.execute("delete from scopes where code='BROKER:TESTB'"); self.cur.execute("delete from financial_institutions where code='TESTB'"); tm._conn.commit()


if __name__ == "__main__":
    unittest.main()
