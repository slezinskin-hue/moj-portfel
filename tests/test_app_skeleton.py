"""Testy szkieletu aplikacji (krok 0): strona startowa po polsku, kontrola stanu, wylacznie lokalny adres."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile  # noqa: E402
os.environ.setdefault("INVAPP_DATA_DIR", os.path.join(tempfile.gettempdir(), "invapp_app_testdata"))

from fastapi.testclient import TestClient  # noqa: E402
from invapp.web.app import app  # noqa: E402


class TestSkeleton(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_redirects_to_portfolio_page(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/podglad")

    def test_error_page_is_polish_and_shows_state_when_database_unavailable(self):
        from invapp.web import app as m
        orig = m.preview.load
        m.preview.load = lambda: (_ for _ in ()).throw(RuntimeError("brak bazy"))
        try:
            r = self.client.get("/podglad")
        finally:
            m.preview.load = orig
        self.assertEqual(r.status_code, 503)
        self.assertIn('lang="pl"', r.text)
        self.assertIn("Mój Portfel", r.text)
        self.assertIn("Stan systemu", r.text)

    def test_health_is_json_and_has_no_secrets(self):
        r = self.client.get("/zdrowie")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["aplikacja"], body["status"]), ("Mój Portfel", "dziala"))
        self.assertIn("ok", body["baza"])
        self.assertNotIn("password", r.text.lower())

    def test_api_docs_disabled(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_server_binds_only_to_loopback(self):
        src = open(os.path.join(ROOT, "invapp", "config.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "invapp", "__main__.py"), encoding="utf-8").read()
        self.assertRegex(src, r'HOST = "127\.0\.0\.1"')
        self.assertIn("host=config.HOST", src)
        self.assertNotRegex(src, r'host="0\.0\.0\.0"|host=\'0\.0\.0\.0\'')

    def test_preview_page_is_read_only_polish_and_has_sections(self):
        r = self.client.get("/podglad")
        self.assertIn(r.status_code, (200, 503))                  # 503 tylko gdy baza podgladu niedostepna
        if r.status_code == 200:
            for fragment in ("Wczytaj eksporty", "Trade Republic", "WSZYSTKIE", "/static/app.js", "/static/charts.js"):
                self.assertIn(fragment, r.text)
            if "Portfel jest jeszcze pusty" not in r.text:                  # z danymi: wszystkie sekcje
                for fragment in ("Wartość portfela", "Kontrola liczb", "Wskaźniki inwestora", "Korekty splitów", "Instrumenty i weryfikacja waluty", "Raport importu", "Pozycje", "Alokacja", "Dywidendy i inne dochody"):
                    self.assertIn(fragment, r.text)
        for path in ("/static/app.js", "/static/charts.js", "/static/app.css"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
            self.assertNotIn("password", r.text.lower())
        self.assertEqual(self.client.post("/podglad").status_code, 405)    # brak operacji zapisu

    def test_scope_tabs_and_scoped_api_endpoints(self):
        for scope in ("xtb", "tr", "all", "nieznany"):
            r = self.client.get("/podglad", params={"scope": scope})
            self.assertIn(r.status_code, (200, 503), scope)
            if r.status_code == 200:
                self.assertIn("Wczytaj eksporty", r.text)
                self.assertNotIn("wolne środki:", r.text.lower().replace("wolne środki nie są", ""))      # wolne srodki nie sa pokazywane
            for path in ("/api/zywo", "/api/historia", "/api/wskazniki", "/api/kontrola", "/api/dochody"):
                self.assertIn(self.client.get(path, params={"scope": scope}).status_code, (200, 503), path + scope)

    def test_all_prices_are_shown_in_pln(self):
        r = self.client.get("/api/zywo", params={"scope": "all", "fast": "true"})
        self.assertIn(r.status_code, (200, 503))
        if r.status_code == 200:
            for p in r.json()["positions"]:
                if p["price_source"] == "MARKET":
                    self.assertIsNotNone(p["price_pln"], p["ticker"])                              # cena w PLN (kurs z API dla walut obcych)
                    self.assertAlmostEqual(p["qty"] * p["price_pln"], p["value"], delta=0.02, msg=p["ticker"])

    def test_metrics_endpoint_is_read_only_json(self):
        r = self.client.get("/api/wskazniki")
        self.assertIn(r.status_code, (200, 503))
        if r.status_code == 200:
            self.assertIn("available", r.json())
        self.assertEqual(self.client.post("/api/wskazniki").status_code, 405)


if __name__ == "__main__":
    unittest.main()
