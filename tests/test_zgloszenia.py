"""
Zgloszenie bledu (invapp/zgloszenia.py, POST /api/zglos): aplikacja tylko przygotowuje tresc i adres zgloszenia na GitHubie -
niczego nie wysyla, nie dolacza danych portfela, maskuje dane osobowe wpisane przez uzytkownika, adres miesci sie w limicie GitHuba.
"""
import os, sys, tempfile, unittest, urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("INVAPP_DATA_DIR", os.path.join(tempfile.gettempdir(), "invapp_app_testdata"))

from invapp import __version__, zgloszenia  # noqa: E402

REPO = "przyklad/moj-portfel"
OK = {"tytul": "Wykres nie pokazuje dywidend", "opis": "Po wczytaniu pliku TR wykres jest pusty.", "kroki": "1. Wczytaj plik\n2. Otwórz Analizę",
      "oczekiwane": "Wykres z dywidendami"}


def query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


class TestBuild(unittest.TestCase):
    def test_issue_url_with_title_body_and_label(self):
        r = zgloszenia.build(OK, REPO, {"tryb": "okno", "widok": "analiza"})
        self.assertEqual(r["bledy"], [])
        self.assertTrue(r["url"].startswith(f"https://github.com/{REPO}/issues/new?"))
        q = query(r["url"])
        self.assertEqual(q["title"], ["[Błąd] Wykres nie pokazuje dywidend"]); self.assertEqual(q["labels"], ["bug"])
        body = q["body"][0]
        self.assertEqual(body, r["tresc"])
        for part in ("### Co się stało\nPo wczytaniu pliku TR wykres jest pusty.", "### Jak to powtórzyć\n1. Wczytaj plik\n2. Otwórz Analizę",
                     "### Czego się spodziewałem\nWykres z dywidendami", f"- Wersja: {__version__}", "- Tryb: okno aplikacji", "- Zakładka: analiza"):
            self.assertIn(part, body)

    def test_without_technical_info_and_optional_fields(self):
        r = zgloszenia.build({"tytul": "X", "opis": "Y"}, REPO, None)
        self.assertNotIn("Informacje techniczne", r["tresc"]); self.assertNotIn("Jak to powtórzyć", r["tresc"]); self.assertNotIn(__version__, r["tresc"])

    def test_personal_data_is_masked_and_reported(self):
        r = zgloszenia.build({"tytul": "Błąd dla jan.kowalski@example.com", "opis": "Konto PL61 1090 1014 0000 0712 1981 2874, PESEL 90010112345, "
                              "plik C:\\Users\\JanKowalski\\Desktop\\export.xlsx. Wartość 166 432,15 zł, wersja 0.5.0, dzień 18.11.2026, 12 pozycji."}, REPO, None)
        text = r["tytul"] + r["tresc"] + urllib.parse.unquote(r["url"])
        for secret in ("jan.kowalski", "example.com", "1090 1014", "90010112345", "JanKowalski"):
            self.assertNotIn(secret, text)
        self.assertIn("C:\\Users\\<użytkownik>\\Desktop\\export.xlsx", r["tresc"])
        for kept in ("166 432,15 zł", "0.5.0", "18.11.2026", "12 pozycji"):                  # zwykle liczby zostaja
            self.assertIn(kept, r["tresc"])
        self.assertEqual(set(r["zamaskowano"]), {"ścieżka z nazwą użytkownika Windows", "adres e-mail", "długi numer (rachunek, IBAN, PESEL…)"})

    def test_required_fields_and_no_repository(self):
        r = zgloszenia.build({"tytul": " ", "opis": ""}, REPO, None)
        self.assertEqual(len(r["bledy"]), 2); self.assertIsNone(r["url"])
        for repo in ("", "zla nazwa/repo", "a/b/c", "https://evil.example/x"):
            r = zgloszenia.build(OK, repo, None)
            self.assertIsNone(r["url"], repo); self.assertTrue(r["tresc"])                   # tresc do skopiowania zostaje

    def test_long_description_fits_github_url_limit(self):
        r = zgloszenia.build({"tytul": "Długi", "opis": "żółć " * 1000, "kroki": "ą" * 2000}, REPO, {"tryb": "przegladarka", "widok": "x"})
        self.assertLessEqual(len(r["url"]), zgloszenia.MAX_URL); self.assertTrue(r["skrocono"])
        self.assertIn("(skrócono", query(r["url"])["body"][0])

    def test_view_name_is_whitelisted_and_control_chars_removed(self):
        r = zgloszenia.build({"tytul": "a\x00b", "opis": "c\x1bd"}, REPO, {"tryb": "okno", "widok": "<script>alert(1)</script>"})
        self.assertNotIn("<script>", r["tresc"]); self.assertNotIn("\x00", r["tytul"]); self.assertNotIn("\x1b", r["tresc"])
        self.assertNotIn(os.path.expanduser("~"), r["tresc"])                                  # bez sciezek uzytkownika w informacjach technicznych


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from invapp.web.app import app
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def test_requires_same_origin_header(self):
        self.assertEqual(self.client.post("/api/zglos", json=OK).status_code, 403)
        self.assertEqual(self.client.post("/api/zglos", json=OK, headers={"X-Requested-With": "invapp", "Origin": "https://evil.example"}).status_code, 403)

    def test_builds_report_for_configured_repository(self):
        with mock.patch("invapp.updates.build_info", lambda: {"repo": REPO}):
            r = self.client.post("/api/zglos", json=dict(OK, techniczne=True, widok="pozycje"), headers={"X-Requested-With": "invapp"})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertTrue(d["url"].startswith(f"https://github.com/{REPO}/issues/new?")); self.assertIn("- Tryb: przeglądarka", d["tresc"])
            bad = self.client.post("/api/zglos", json={"tytul": "", "opis": ""}, headers={"X-Requested-With": "invapp"})
            self.assertEqual(bad.status_code, 422); self.assertIsNone(bad.json()["url"])
        with mock.patch("invapp.updates.build_info", lambda: {"repo": ""}):
            d = self.client.post("/api/zglos", json=OK, headers={"X-Requested-With": "invapp"}).json()
            self.assertIsNone(d["url"]); self.assertIn("Wykres nie pokazuje dywidend", d["tytul"])


if __name__ == "__main__":
    unittest.main()
