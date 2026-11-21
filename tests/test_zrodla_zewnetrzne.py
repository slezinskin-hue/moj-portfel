"""
Kontrola zewnetrznych zrodel danych (prawdziwa siec). Aplikacja zalezy od uslug, ktore moga sie zmienic bez uprzedzenia:
nieoficjalne API Yahoo Finance (ceny, historia, wyszukiwanie po ISIN, splity, sektory, indeksy), NBP (kursy), XTB (specyfikacja OMI)
i GitHub (aktualizacje). Ten test sprawdza KSZTALT odpowiedzi tymi samymi funkcjami, ktorych uzywa program - jesli usluga zmieni format,
test pada, zanim zauwazy to uzytkownik.

* Domyslnie pominiety (testy publiczne dzialaja bez internetu). Uruchomienie: INVAPP_LIVE=1 python -m unittest tests.test_zrodla_zewnetrzne
* GitHub Actions uruchamia go codziennie (.github/workflows/zrodla.yml); blad = e-mail z GitHuba do wlasciciela repozytorium.
* Wysylane sa tylko publiczne symbole przykladowych instrumentow (nie dane portfela).
"""
import os, sys, unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LIVE = os.environ.get("INVAPP_LIVE") == "1"
MAX_AGE = timedelta(days=7)          # notowanie starsze = zrodlo nie podaje swiezych cen (dluzsze swieta gield < 7 dni)


RETRY_WAITS = (30, 90)               # chwilowa awaria uslugi (5xx, brak polaczenia) != zmiana formatu: ponowna proba po 30 s i 90 s


def _retry(fn, *a, **kw):
    """Wywolanie z ponowieniem TYLKO przy bledzie serwera/sieci. Zly ksztalt odpowiedzi (ProviderError, KeyError) pada od razu."""
    import time, urllib.error
    for wait in RETRY_WAITS + (None,):
        try:
            return fn(*a, **kw)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or wait is None:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if wait is None:
                raise
        time.sleep(wait)


def _repo():
    """Repozytorium wydan: jak instalator (GITHUB_REPOSITORY w Actions), lokalnie z adresu origin - bez nazwy wpisanej w kod."""
    import re, subprocess
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("INVAPP_UPDATE_REPO")
    if repo:
        return repo
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(\.git)?$", url)
    return m.group(1) if m else None


@unittest.skipUnless(LIVE, "kontrola zrodel zewnetrznych tylko z INVAPP_LIVE=1 (wymaga internetu)")
class TestYahoo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from invapp.pricing.history import HistoryYahoo
        from invapp.pricing.providers import YahooProvider, http_get
        fetch = lambda url: _retry(http_get, url)
        cls.y = YahooProvider(fetch=fetch)
        cls.h = HistoryYahoo(fetch=fetch)

    def _fresh_quote(self, symbol, currency, unit="MAJOR"):
        q = self.y.quote(symbol)
        self.assertEqual(q["symbol"], symbol)
        self.assertIsInstance(q["price"], Decimal)
        self.assertGreater(q["price"], 0)
        self.assertEqual(q["currency"], currency, f"waluta {symbol}")
        self.assertEqual(q["price_unit"], unit, f"jednostka ceny {symbol}")
        self.assertLess(datetime.now(timezone.utc) - q["quoted_at"], MAX_AGE, f"stare notowanie {symbol}")
        return q

    def test_quotes_usa_gpw_lse(self):
        self._fresh_quote("AAPL", "USD")
        self._fresh_quote("PKN.WA", "PLN")
        self._fresh_quote("VUAA.L", "USD")                       # linia LSE notowana w USD (jak w portfelu)
        self._fresh_quote("VOD.L", "GBP", "MINOR_1_100")         # pensy (GBp) -> GBP z jednostka 1/100

    def test_fx(self):
        for base in ("USD", "EUR", "GBP", "CHF"):
            r = self.y.fx(base)
            self.assertTrue(Decimal("0.5") < r["rate"] < Decimal("10"), f"kurs {base}PLN {r['rate']}")
            self.assertLess(datetime.now(timezone.utc) - r["quoted_at"], MAX_AGE)

    def test_daily_history(self):
        d = self.h.daily("AAPL", date.today() - timedelta(days=400))
        self.assertEqual(d["currency"], "USD")
        self.assertGreater(len(d["closes"]), 200)
        days = [c[0] for c in d["closes"]]
        self.assertEqual(days, sorted(days))
        self.assertLess((date.today() - days[-1]).days, 7)
        self.assertTrue(all(c[1] > 0 for c in d["closes"]))
        g = self.h.daily("VOD.L", date.today() - timedelta(days=30))          # historia w pensach przeliczona na funty
        self.assertEqual((g["raw_currency"], g["currency"], g["unit"]), ("GBp", "GBP", "MINOR_1_100"))

    def test_benchmarks_have_history(self):
        from invapp.pricing.benchmarks import BENCHMARKS
        for b in BENCHMARKS:
            d = self.h.daily(b["symbol"], date.today() - timedelta(days=60))
            self.assertGreater(len(d["closes"]), 10, f"brak historii indeksu {b['symbol']}")
            if d["currency"]:
                self.assertEqual(d["currency"], b["currency"], f"waluta indeksu {b['symbol']}")

    def test_search_by_isin(self):
        found = [c["symbol"] for c in self.y.search("US0378331005")]          # ISIN Apple
        self.assertIn("AAPL", found)

    def test_profile_has_sector(self):
        p = self.y.search_profile("AAPL")
        self.assertIsNotNone(p)
        self.assertEqual(p["type"], "EQUITY")
        self.assertTrue(p["sector"], "brak sektora w profilu")

    def test_splits(self):
        s = self.y.splits("NVDA", start_ts=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIn((date(2024, 6, 10), Decimal("10"), Decimal("1")), s)


@unittest.skipUnless(LIVE, "kontrola zrodel zewnetrznych tylko z INVAPP_LIVE=1 (wymaga internetu)")
class TestNbp(unittest.TestCase):
    def test_table_a(self):
        from invapp.pricing.providers import NbpProvider, http_get
        t = NbpProvider(fetch=lambda url: _retry(http_get, url)).table_a()
        self.assertLess((date.today() - date.fromisoformat(t["date"])).days, 7)
        for c in ("USD", "EUR", "GBP", "CHF"):
            self.assertIn(c, t["rates"])
            self.assertTrue(Decimal("0.5") < t["rates"][c] < Decimal("10"))


@unittest.skipUnless(LIVE, "kontrola zrodel zewnetrznych tylko z INVAPP_LIVE=1 (wymaga internetu)")
class TestXtbOmi(unittest.TestCase):
    def test_omi_pdf_available(self):
        """Specyfikacja instrumentow XTB: plik pod tym samym adresem nadal jest PDF-em (pobierany tylko poczatek)."""
        import urllib.request
        from invapp.brokers.xtb.broker import OMI_URL
        req = urllib.request.Request(OMI_URL, headers={"User-Agent": "Mozilla/5.0 investing-app-local", "Range": "bytes=0-1023"})
        def get():
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(1024)
        head = _retry(get)
        self.assertTrue(head.startswith(b"%PDF"), "pod adresem OMI nie ma PDF-a")


@unittest.skipUnless(LIVE, "kontrola zrodel zewnetrznych tylko z INVAPP_LIVE=1 (wymaga internetu)")
class TestGithubRelease(unittest.TestCase):
    def test_latest_release_installable(self):
        """Najnowsze wydanie ma instalator i sume SHA-256 - inaczej aplikacje uzytkownikow nie zaproponuja aktualizacji."""
        from invapp import updates
        repo = _repo()
        if not repo:
            self.skipTest("nieznane repozytorium (GITHUB_REPOSITORY / INVAPP_UPDATE_REPO / git remote origin)")
        rel = updates.latest_release({"repo": repo}, fetch=lambda url: _retry(updates._get_json, url))
        self.assertIsNotNone(rel)
        self.assertTrue(rel["installer_name"].lower().endswith(".exe"))
        self.assertTrue(updates._allowed(rel["installer_url"], repo))
        self.assertRegex(rel["sha256"] or "", r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
