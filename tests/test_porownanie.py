"""Wykres "Zwrot %": indeksy do porownania (invapp/pricing/benchmarks.py, migracja 0016) i miary procentowe (metrics.return_series). Bez internetu."""
import os, sys, unittest
from datetime import date, timedelta
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.pricing import benchmarks, metrics  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class FakeDaily:
    def __init__(self, data, errors=()):
        self.data, self.errors, self.calls = data, set(errors), []

    def daily(self, symbol, start):
        self.calls.append((symbol, start))
        if symbol in self.errors:
            raise OSError("brak sieci")
        ccy, closes = self.data[symbol]
        return {"currency": ccy, "raw_currency": ccy, "unit": "MAJOR", "closes": [(d, c) for d, c in closes if d >= start], "exchange": "T"}


class TestBenchmarks(tm.Base):
    def data(self):
        d0 = date(2024, 1, 1)
        days = [d0 + timedelta(days=i) for i in range(10)]
        return {"^NDX": ("USD", [(d, D(100 + i)) for i, d in enumerate(days)] + [(days[-1], D(200))]),        # ostatni dzien podany dwa razy (sesja w toku)
                "^GSPC": ("USD", [(d, D(50)) for d in days]),
                "^990100-USD-STRD": (None, [(d, D(10)) for d in days]),                                       # Yahoo nie podaje waluty: waluta z konfiguracji
                "ETFBW20TR.WA": ("EUR", [(d, D(1)) for d in days]),                                           # zla waluta -> odrzucone, nie przeliczane
                "USDPLN=X": ("PLN", [(d, D("4.00")) for d in days if d.weekday() < 5])}

    def test_refresh_stores_dedupes_and_checks_currency(self):
        p = FakeDaily(self.data())
        res = benchmarks.refresh(tm._conn, p, start=date(2024, 1, 1))
        self.assertEqual(res["indeksy"], {"ndx": 10, "spx": 10, "msci": 10})
        self.assertEqual(res["errors"], ["WIG20: ValueError"])
        self.assertEqual(self.one("select close_price from benchmark_prices where symbol='^NDX' and price_date='2024-01-10'"), D(200))
        self.assertEqual(self.one("select count(*) from fx_rates where base='USD' and source='YAHOO_1D'"), 8)
        p2 = FakeDaily(self.data())                                                                         # drugi raz: przyrostowo (ostatnie dni ponownie)
        benchmarks.refresh(tm._conn, p2, start=date(2024, 1, 1))
        self.assertTrue(all(s >= date(2023, 12, 31) for _, s in p2.calls))
        self.assertGreater(min(s for sym, s in p2.calls if sym == "^NDX"), date(2024, 1, 1) - timedelta(days=1))

    def test_levels_in_pln_forward_filled_and_none_before_data(self):
        benchmarks.refresh(tm._conn, FakeDaily(self.data()), start=date(2024, 1, 1))
        days = [date(2023, 12, 30), date(2024, 1, 2), date(2024, 1, 6), date(2024, 1, 10), date(2024, 1, 25)]
        lv = benchmarks.levels(tm._conn, days)
        self.assertEqual([b["key"] for b in lv["indeksy"]], ["ndx", "spx", "msci", "wig20"])
        ndx = lv["poziomy"]["ndx"]
        self.assertIsNone(ndx[0])                                                    # przed pierwszym notowaniem: brak, nie zgadujemy
        self.assertEqual(ndx[1], 101 * 4.0)                                          # 101 USD x 4,00
        self.assertEqual(ndx[2], 105 * 4.0)                                          # sobota: zamkniecie z soboty x kurs z piatku (ostatni znany)
        self.assertEqual(ndx[3], 200 * 4.0)
        self.assertIsNone(ndx[4])                                                    # ponad 7 dni bez notowan: brak
        self.assertEqual(lv["poziomy"]["wig20"], [None] * 5)

    def test_app_role_reads_only(self):
        priv = "select has_table_privilege('investing_app', 'benchmark_prices', %s)"
        self.assertTrue(self.one(priv, ("SELECT",))); self.assertFalse(self.one(priv, ("INSERT",)))

    def test_zero_or_negative_close_rejected(self):
        import psycopg2
        for bad in (0, -1):
            self.cur.execute("savepoint sp")
            with self.assertRaises(psycopg2.errors.CheckViolation):
                self.cur.execute("insert into benchmark_prices(symbol, price_date, close_price, currency, source) values ('^X', '2024-01-02', %s, 'USD', 'T')", (bad,))
            self.cur.execute("rollback to savepoint sp")


class TestReturnSeries(unittest.TestCase):
    def series(self):
        out, d0 = [], date(2024, 1, 1)
        for i in range(400):
            gross = 1000.0 if i < 200 else 1500.0                         # dzien 200: wplata 500; dzien 300: wyplata 200 (suma wplat != wplaty netto)
            inv = gross - (200.0 if i >= 300 else 0.0)
            v = (1000.0 * (1.0005 ** i)) if i < 200 else (1000.0 * (1.0005 ** i) + 500.0 * (1.0005 ** (i - 200)))
            if i >= 300:
                v -= 200.0 * (1.0005 ** (i - 300))
            out.append({"date": d0 + timedelta(days=i), "value": v, "invested_net": inv, "invested_gross": gross})
        return out

    def test_measures_match_tiles_and_definitions(self):
        s = self.series()
        m = metrics.compute(s, income=[(date(2024, 3, 1), 20.0)])
        rs = m["return_series"]
        n = len(s)
        self.assertTrue(all(len(v) == n for v in rs.values()))
        self.assertAlmostEqual(rs["gain_net"][-1], m["gain_pct_on_net"], places=12)
        self.assertAlmostEqual(rs["gain_gross"][-1], m["gain_pct_on_purchases"], places=12)
        self.assertNotAlmostEqual(rs["gain_net"][-1], rs["gain_gross"][-1], places=4)        # po wyplacie dwie rozne miary
        g = s[350]["value"] - 1300.0
        self.assertAlmostEqual(rs["gain_net"][350], g / 1300.0, places=12); self.assertAlmostEqual(rs["gain_gross"][350], g / 1500.0, places=12)
        self.assertAlmostEqual(rs["xirr"][-1], m["xirr"], places=9)                   # ostatni punkt = kafelek XIRR
        self.assertAlmostEqual(rs["cagr"][-1], m["twr_annualized"], places=12)
        self.assertIsNone(rs["cagr"][300]); self.assertIsNone(rs["xirr"][50])          # CAGR od roku, XIRR od 90 dni
        self.assertTrue(any(x is not None for x in rs["xirr"][90:120]))
        self.assertAlmostEqual(rs["twr_inc"][-1], (1 + m["twr_total"]) * (1 + 20.0 / s[60]["value"]), places=9)   # dywidenda jako zwrot dnia
        self.assertAlmostEqual(m["index_series"][-1][1] - 1, m["twr_total"], places=12)

    def test_newton_agrees_with_bisection(self):
        flows = [(date(2024, 1, 1), -1000.0), (date(2024, 6, 1), -500.0), (date(2025, 1, 1), 1700.0)]
        self.assertAlmostEqual(metrics._xirr_newton(flows), metrics.xirr(flows), places=8)


if __name__ == "__main__":
    unittest.main()
