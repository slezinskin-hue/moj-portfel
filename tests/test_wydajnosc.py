"""
Optymalizacje wydajnosci (2026-11-20) - kazda daje TE SAME wyniki co wczesniej, tylko taniej:
* pula polaczen tylko do odczytu (db/engine.py): polaczenie wraca do puli, zepsute nie wraca, zatrzymanie serwera oproznia pule;
* historia: jedno liczenie serii naraz (rownolegle /api/historia i /api/wskazniki), wykres nie czeka na pobieranie notowan z sieci;
* odpowiedz JSON: jedna serializacja, bajty jak dawniej (json.dumps -> json.loads -> JSONResponse);
* wskazniki: podsumowanie bez listy pozycji = to samo podsumowanie co pelne;
* model PPK z pamieci podrecznej: nowa migawka = nowy wynik; zwracana kopia;
* zadanie w tle: krok bez zmian nie przeladowuje strony; krok brokera (mogl zmienic dane) - nastepny krok przeladowuje zawsze;
* wykres portfela: petla tylko po posiadanych pozycjach - zamkniecie i ponowne otwarcie pozycji liczone jak dotad.
"""
import json, os, shutil, sys, tempfile, threading, time, unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import baza_testowa as tm  # noqa: E402
from invapp import config  # noqa: E402
from invapp.core import cash, ppk  # noqa: E402
from invapp.pricing import history  # noqa: E402
from invapp.web import dashboard  # noqa: E402
from test_history import HistBase, span  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class TestConnectionPool(unittest.TestCase):
    """Wlasny katalog danych (osobny serwer)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="invapp_pool_")
        cls.old = os.environ.get(config.DATA_DIR_ENV)
        os.environ[config.DATA_DIR_ENV] = cls.dir
        from invapp.db import engine
        cls.e = engine
        engine.stop(); engine.start()

    @classmethod
    def tearDownClass(cls):
        cls.e.stop()
        if cls.old is None:
            os.environ.pop(config.DATA_DIR_ENV, None)
        else:
            os.environ[config.DATA_DIR_ENV] = cls.old
        shutil.rmtree(cls.dir, ignore_errors=True)

    def pid(self, c):
        cur = c.cursor(); cur.execute("select pg_backend_pid()"); return cur.fetchone()[0]

    def test_readonly_connection_is_reused_and_stays_readonly(self):
        import psycopg2
        c = self.e.connect("app", readonly=True); p1 = self.pid(c); c.close()
        c = self.e.connect("app", readonly=True); p2 = self.pid(c)
        self.assertEqual(p1, p2)                                                              # to samo polaczenie z puli
        self.assertTrue(c.readonly and c.autocommit)
        with self.assertRaises(psycopg2.Error):                                               # nadal tylko do odczytu
            c.cursor().execute("create temp table x(a int)")
        c.close()
        c = self.e.connect("app", readonly=True); self.assertEqual(self.pid(c), p1); c.close()   # blad zapytania nie psuje polaczenia (autocommit)

    def test_double_close_returns_connection_once(self):
        c = self.e.connect("app", readonly=True); c.close(); c.close()                        # drugie close() nie dodaje go drugi raz
        self.assertEqual([x for x, _ in self.e._pool["idle"]].count(c), 1)
        a = self.e.connect("app", readonly=True); b = self.e.connect("app", readonly=True)
        self.assertIsNot(a, b); a.close(); b.close()

    def test_two_users_get_two_connections_and_writers_are_not_pooled(self):
        a = self.e.connect("app", readonly=True); b = self.e.connect("app", readonly=True)
        self.assertNotEqual(self.pid(a), self.pid(b))
        a.close(); b.close()
        w = self.e.connect("app"); pw = self.pid(w); w.close(); self.assertTrue(w.closed)       # zapis (notowania) - zawsze nowe polaczenie
        o = self.e.connect("owner"); o.close(); self.assertTrue(o.closed)
        r = self.e.connect("app", readonly=True); self.assertNotEqual(self.pid(r), pw); r.close()

    def test_broken_connection_is_not_returned_and_stop_drains_pool(self):
        c = self.e.connect("app", readonly=True); p = self.pid(c)
        c.close_for_real(); c.close()                                                         # zamkniete - nie wraca do puli
        self.assertNotIn(c, [x for x, _ in self.e._pool["idle"]])
        c2 = self.e.connect("app", readonly=True); self.assertNotEqual(self.pid(c2), p); c2.close()
        self.assertTrue(self.e._pool["idle"])
        idle = [x for x, _ in self.e._pool["idle"]]
        self.e.stop()
        self.assertEqual(self.e._pool["idle"], []); self.assertTrue(all(x.closed for x in idle))
        self.e.start()
        c3 = self.e.connect("app", readonly=True); cur = c3.cursor(); cur.execute("select 1"); self.assertEqual(cur.fetchone()[0], 1); c3.close()


class TestHistoryLocks(unittest.TestCase):
    """Bez bazy: polaczenie i liczenie serii podstawione."""

    def setUp(self):
        dashboard.invalidate_caches()
        self.calls = 0

        class Conn:
            def cursor(self):
                return None

            def close(self):
                pass
        self.patches = [mock.patch.object(dashboard, "_connect", lambda readonly=False: Conn()),
                        mock.patch.object(dashboard, "_display", lambda cur: "PLN")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if dashboard._lock.acquire(timeout=3):                 # blokada zakleszczona przez zepsuty kod (mutacja) - test juz nie przeszedl, nie czekamy w nieskonczonosc
            dashboard._lock.release()
            dashboard.invalidate_caches()

    def test_parallel_requests_build_series_once(self):
        def build(conn, code, w):
            self.calls += 1; time.sleep(0.3); return {"series": [], "code": code}
        with mock.patch.object(dashboard, "_history_build", build):
            out = []
            ts = [threading.Thread(target=lambda: out.append(dashboard.history_data("all"))) for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(out), 3); self.assertTrue(all(o is out[0] for o in out))

    def test_chart_does_not_wait_for_live_quotes_download(self):
        from invapp.pricing import service
        started, release = threading.Event(), threading.Event()

        def slow_quotes(conn, provider, pause=0.3):
            started.set(); release.wait(10); return {"quotes_new": 0}

        class W:
            def commit(self):
                pass

            def close(self):
                pass
        done = threading.Event()
        try:
            with mock.patch.object(service, "refresh_quotes", slow_quotes), mock.patch.object(dashboard, "_connect", lambda readonly=False: W() if not readonly else None):
                t = threading.Thread(target=lambda: dashboard.refresh_live_if_stale(force=True), daemon=True); t.start()
                self.assertTrue(started.wait(5))
                with mock.patch.object(dashboard, "_history_build", lambda conn, code, w: {"series": []}), \
                     mock.patch.object(dashboard, "_connect", lambda readonly=False: type("C", (), {"cursor": lambda s: None, "close": lambda s: None})()):
                    h = threading.Thread(target=lambda: (dashboard.history_data("xtb"), done.set()), daemon=True); h.start()
                    self.assertTrue(done.wait(1.0), "wykres czeka na pobieranie notowan")      # watki w tle (daemon): zablokowany test nie wisi
        finally:
            release.set()

    def test_invalidation_during_live_download_is_not_overwritten(self):
        from invapp.pricing import service

        class W:
            def commit(self):
                pass

            def close(self):
                pass

        def quotes(conn, provider, pause=0.3):
            dashboard.invalidate_caches(); return {"quotes_new": 1}                          # np. zapis ustawien w trakcie pobierania
        with mock.patch.object(service, "refresh_quotes", quotes), mock.patch.object(dashboard, "_connect", lambda readonly=False: W()):
            dashboard.refresh_live_if_stale(force=True)
        self.assertEqual(dashboard._cache["live_at"], 0.0)                                   # nastepne zadanie pobierze ceny od nowa

    def test_all_chart_parts_come_from_one_build_with_the_total(self):
        built = []

        def build(conn, code, end=None):
            built.append(code); return {"series": [], "code": code, "n": len(built)}
        with mock.patch.object(history, "build_series", build):
            codes = ["INVESTMENT_ALL", "BROKER:XTB", "BROKER:TR"]
            single = [dashboard._raw_series(None, c) for c in codes]                         # kazda w pamieci, ale z osobnych liczen (np. zakladki)
            first = dashboard._raw_together(None, codes)
            self.assertTrue(all(a is not b for a, b in zip(first, single)))                 # rozne liczenia -> komplet liczony razem od nowa
            self.assertEqual(built, codes + codes)
            again = dashboard._raw_together(None, codes)
            self.assertTrue(all(a is b for a, b in zip(first, again))); self.assertEqual(len(built), 6)   # z pamieci, ten sam komplet
            self.assertIs(dashboard._raw_series(None, "BROKER:XTB"), first[1])               # zakladka XTB: seria z kompletu

    def test_invalidation_during_build_is_not_cached(self):
        def build(conn, code, w):
            self.calls += 1; dashboard.invalidate_history(); return {"series": [], "n": self.calls}
        with mock.patch.object(dashboard, "_history_build", build):
            dashboard.history_data("all"); dashboard.history_data("all")
        self.assertEqual(self.calls, 2)                                                       # wynik sprzed uniewaznienia nie zostal zapamietany


class TestJsonResponse(unittest.TestCase):
    def test_same_bytes_as_double_serialization(self):
        from fastapi.responses import JSONResponse
        from invapp.web import app as webapp
        obj = {"a": D("12345.678901234567890123"), "b": [D("-0.01"), D(0), None, True], "d": date(2026, 11, 20),
               "t": datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc), "pl": "zażółć gęślą jaźń – €", "n": {"x": (1, 2.5)}, 7: "klucz liczbowy"}
        old = JSONResponse(json.loads(json.dumps(obj, default=webapp._json_default))).body
        self.assertEqual(webapp._JSON(obj).body, old)
        with self.assertRaises(ValueError):                                                   # NaN - blad jak dawniej (allow_nan=False)
            webapp._JSON({"x": D("NaN")})


class TestBackgroundSteps(unittest.TestCase):
    def test_changed_rules(self):
        from invapp.web.importing import _changed
        self.assertFalse(_changed({"quotes_new": 0, "quotes_same": 5, "errors": [], "fx": {"USD": "4"}, "fx_new": 0}))
        self.assertTrue(_changed({"quotes_new": 1, "quotes_same": 4, "errors": [], "fx": {}, "fx_new": 0}))
        self.assertTrue(_changed({"quotes_new": 0, "quotes_same": 5, "errors": [], "fx": {}, "fx_new": 1}))
        self.assertFalse(_changed({"table": "220/A/NBP/2026", "date": "2026-11-20", "added": 0}))
        self.assertTrue(_changed({"table": "220/A/NBP/2026", "date": "2026-11-20", "added": 4}))
        self.assertFalse(_changed({"waluta": "PLN"}))
        self.assertTrue(_changed({"listings": 3, "price_rows": 0, "fx": {}, "errors": []}))      # historia: zawsze odswiezenie (jak dotad)
        self.assertTrue(_changed([{"symbol": "X"}])); self.assertTrue(_changed(None))

    def test_broker_step_forces_next_refresh(self):
        from invapp.web import importing
        quiet = {"quotes_new": 0, "quotes_same": 1, "errors": [], "fx": {}, "fx_new": 0}
        steps = {"quick": [("powiazania", lambda: [])], "slow": [], "history": []}

        class Conn:
            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass
        v0 = importing.STATE["version"]
        with mock.patch.object(importing, "owner_conn", lambda: Conn()), mock.patch.object(importing, "_phase_steps", lambda c, p: steps), \
             mock.patch.object(dashboard, "refresh_quotes_shared", lambda c: quiet), \
             mock.patch("invapp.pricing.history.refresh_history", lambda c, p: {"listings": 0}), \
             mock.patch("invapp.pricing.benchmarks.refresh", lambda c, p: {"x": 1}), \
             mock.patch.object(importing, "_display_currency_history", lambda c: {"waluta": "PLN"}), \
             mock.patch("invapp.pricing.profiles.refresh", lambda c, p: {"profile": 3}), \
             mock.patch("invapp.pricing.service.refresh_nbp", lambda c, p: {"table": "t", "date": "d", "added": 0}):
            importing._pricing_pass()
        # notowania bez zmian, ale po kroku brokera -> odswiezenie; historia, indeksy -> odswiezenie; waluta PLN, sektory, NBP bez nowych -> bez
        self.assertEqual(importing.STATE["version"] - v0, 3)


class TestSummaryAndModels(HistBase):
    def setUp(self):
        super().setUp()
        dashboard.invalidate_caches()

        class NoClose:
            def __init__(s, c):
                s._c = c

            def close(s):
                pass

            def __getattr__(s, k):
                return getattr(s._c, k)
        self.p = mock.patch.object(dashboard, "_connect", lambda readonly=False: NoClose(tm._conn)); self.p.start()

    def tearDown(self):
        self.p.stop(); dashboard.invalidate_caches()
        super().tearDown()

    def test_summary_only_scope_equals_full_scope(self):
        self.cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = 'INVESTMENT_ALL'", (self.acct,))
        a = self.instrument("AAA.PL", "PLN"); self.buy(a, date.today() - timedelta(days=20), 10, 1000)
        self.store_prices(a, "PLN", span(date.today() - timedelta(days=20), 21, lambda i: D(100) + i))
        acc = ppk.add_account(self.cur, "PPK", "Przykładowe TFI", "Fundusz", "2023-05-10")
        ppk.add_snapshot(self.cur, acc, (date.today() - timedelta(days=5)).isoformat(), "2 680,00",
                         {"EMPLOYEE_BASIC": {"wplaty": "1 000,00", "wartosc": "1 200,00"}, "EMPLOYER_BASIC": {"wplaty": "750", "wartosc": "900,00"},
                          "WELCOME": {"wplaty": "250,00", "wartosc": "300,00"}, "STATE_ANNUAL": {"wplaty": "240,00", "wartosc": "280,00"}})
        for incl in (False, True):
            cash.set_setting(self.cur, "include_ppk", incl)
            for sc in ("all", "xtb"):
                full, short = dashboard.live_data(sc), dashboard.live_data(sc, summary_only=True)
                self.assertEqual(short["scope"], full["scope"], (sc, incl))
                self.assertEqual(short["positions"], [])

    def test_ppk_model_cache_follows_data_and_returns_copy(self):
        acc = ppk.add_account(self.cur, "PPK", "Przykładowe TFI", "Fundusz", "2023-05-10")
        lines = {"EMPLOYEE_BASIC": {"wplaty": "1 000,00", "wartosc": "1 200,00"}, "EMPLOYER_BASIC": {"wplaty": "750", "wartosc": "900,00"}}
        ppk.add_snapshot(self.cur, acc, (date.today() - timedelta(days=40)).isoformat(), "2 100,00", lines)
        m1 = ppk.model(self.cur)
        m1["flows"].append("zmiana wolajacego"); m1["daily"].clear()
        m2 = ppk.model(self.cur)
        self.assertEqual(m2, ppk._model(self.cur, date.today()))                             # kopia - zmiany wolajacego nie psuja pamieci
        ppk.add_snapshot(self.cur, acc, (date.today() - timedelta(days=5)).isoformat(), "2 300,00",
                         {"EMPLOYEE_BASIC": {"wplaty": "1 100,00", "wartosc": "1 300,00"}, "EMPLOYER_BASIC": {"wplaty": "825", "wartosc": "1 000,00"}})
        m3 = ppk.model(self.cur)
        self.assertEqual(m3, ppk._model(self.cur, date.today()))                             # nowa migawka = nowy wynik
        self.assertNotEqual(m3["value"], m2["value"])
        self.cur.execute("update accounts set display_name = 'PPK 2' where account_id = %s", (acc,))
        self.assertEqual(ppk.model(self.cur), ppk._model(self.cur, date.today()))

    def test_position_closed_and_reopened_is_valued_like_before(self):
        a = self.instrument("AAA.PL", "PLN"); b = self.instrument("BBB.PL", "PLN"); d0 = date(2025, 1, 1)
        lot = self.buy(a, d0, 10, 1000); self.buy(b, d0 + timedelta(days=2), 4, 400)
        self.sell(a, lot, d0 + timedelta(days=5), 10, 1100, 1000)                             # A zamknieta
        self.buy(a, d0 + timedelta(days=12), 5, 600)                                          # A otwarta ponownie
        self.store_prices(a, "PLN", span(d0, 20, lambda i: D(100) + i)); self.store_prices(b, "PLN", span(d0, 20, lambda i: D(100) - i))
        s = history.build_series(tm._conn, end=d0 + timedelta(days=19))
        by = {p["date"]: p for p in s["series"]}
        day = lambda i: by[(d0 + timedelta(days=i)).isoformat()]  # noqa: E731
        self.assertEqual((day(1)["value"], day(1)["positions"]), (D("1010.00"), 1))
        self.assertEqual((day(4)["value"], day(4)["positions"]), (D("1040.00") + 4 * D(96), 2))
        self.assertEqual((day(8)["value"], day(8)["positions"]), (4 * D(92), 1))               # po sprzedazy A: tylko B
        self.assertEqual((day(15)["value"], day(15)["positions"]), (5 * D(115) + 4 * D(85), 2))  # A znow, cena z dnia (nie sprzed zamkniecia)
        self.assertEqual(s["estimated_days"], 0)


if __name__ == "__main__":
    unittest.main()
