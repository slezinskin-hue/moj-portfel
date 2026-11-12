"""Testy wskaznikow inwestorskich (pricing/metrics.py): czysta logika, bez bazy i internetu. Wartosci oczekiwane liczone recznie."""
import os, sys, unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from invapp.pricing import metrics  # noqa: E402


def mk(start, values, buys=None, sells=None):
    """Seria dzienna jak z build_series. values: lista wartosci papierow dzien po dniu od `start`; buys/sells: {indeks_dnia: kwota}."""
    buys, sells = buys or {}, sells or {}
    out, gross, sold = [], 0.0, 0.0
    for i, v in enumerate(values):
        gross += buys.get(i, 0.0); sold += sells.get(i, 0.0)
        out.append({"date": (start + timedelta(days=i)).isoformat(), "value": v, "invested_gross": gross, "sold": sold, "invested_net": gross - sold,
                    "gain": v - (gross - sold), "positions": 1, "estimated_positions": 0})
    return out


class TestReturns(unittest.TestCase):
    def test_simple_gain_and_percent(self):
        s = mk(date(2025, 1, 1), [1000.0] * 31 + [1200.0], buys={0: 1000.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["gain"], 200.0)
        self.assertAlmostEqual(m["gain_pct_on_purchases"], 0.20)
        self.assertAlmostEqual(m["twr_total"], 0.20)
        self.assertIsNone(m["xirr"])                                   # okres < 90 dni: stopa roczna zbyt myląca
        self.assertIsNone(m["twr_annualized"])

    def test_twr_ignores_timing_of_deposits_but_gain_does_not(self):
        # +10%, dokupienie 1000 (zwrot dnia 0%), -10% => TWR = 1,1 * 0,9 - 1 = -1%, a zysk w zl = 1890 - 2000 = -110
        s = mk(date(2025, 1, 1), [1000.0, 1100.0, 2100.0, 1890.0], buys={0: 1000.0, 2: 1000.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["twr_total"], -0.01, places=9)
        self.assertAlmostEqual(m["gain"], -110.0)
        self.assertAlmostEqual(m["gain_pct_on_purchases"], -110.0 / 2000.0)

    def test_sale_is_a_withdrawal_not_a_loss(self):
        s = mk(date(2025, 1, 1), [1000.0, 1000.0, 500.0, 500.0], buys={0: 1000.0}, sells={2: 500.0})       # sprzedano polowe po cenie wyceny
        m = metrics.compute(s)
        self.assertAlmostEqual(m["twr_total"], 0.0, places=12)
        self.assertAlmostEqual(m["gain"], 0.0)
        self.assertAlmostEqual(m["invested_net"], 500.0)

    def test_empty_portfolio_days_do_not_break(self):
        s = mk(date(2025, 1, 1), [1000.0, 1100.0, 0.0, 0.0, 500.0], buys={0: 1000.0, 4: 500.0}, sells={2: 1100.0})
        m = metrics.compute(s)
        self.assertTrue(m["available"])
        self.assertAlmostEqual(m["twr_total"], 0.10, places=9)           # po wyprzedaniu 0 dni, nowe wplaty bez zwrotu
        self.assertAlmostEqual(m["gain"], 100.0)                           # 500 - (zakupy 1500 - sprzedaz 1100)

    def test_too_short_history(self):
        self.assertFalse(metrics.compute(mk(date(2025, 1, 1), [100.0], buys={0: 100.0}))["available"])
        self.assertFalse(metrics.compute([])["available"])


class TestAnnualized(unittest.TestCase):
    def test_xirr_one_year_ten_percent(self):
        s = mk(date(2025, 1, 1), [1000.0] * 365 + [1100.0], buys={0: 1000.0})         # 365 dni miedzy pierwszym a ostatnim punktem
        m = metrics.compute(s)
        self.assertEqual(m["days"], 365)
        self.assertAlmostEqual(m["xirr"], 0.10, places=6)
        self.assertAlmostEqual(m["twr_annualized"], 0.10, places=6)

    def test_cagr_two_years(self):
        s = mk(date(2024, 1, 1), [1000.0] * 730 + [1210.0], buys={0: 1000.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["twr_total"], 0.21, places=9)
        self.assertAlmostEqual(m["twr_annualized"], 1.21 ** (365.0 / 730) - 1, places=9)

    def test_xirr_with_income_is_higher(self):
        s = mk(date(2025, 1, 1), [1000.0] * 365 + [1100.0], buys={0: 1000.0})
        inc = [(date(2025, 7, 1), 30.0)]
        m = metrics.compute(s, income=inc)
        self.assertGreater(m["xirr_with_income"], m["xirr"])
        self.assertAlmostEqual(m["income_12m"], 30.0)
        self.assertAlmostEqual(m["dividend_yield"], 30.0 / 1100.0)

    def test_xirr_needs_both_signs(self):
        self.assertIsNone(metrics.xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)]))
        self.assertIsNone(metrics.xirr([]))


class TestPeriods(unittest.TestCase):
    def test_calendar_years_and_ytd(self):
        # 2024-12-30 kupno 1000; wartosc 1000 do konca 2024; 2025-03-01 dokupienie 500, wartosc 1600; koniec 2025-03-02: 1650
        s = mk(date(2024, 12, 30), [1000.0] * 61 + [1600.0, 1650.0], buys={0: 1000.0, 61: 500.0})
        m = metrics.compute(s)
        y24, y25 = m["years"][0], m["years"][1]
        self.assertEqual((y24["year"], y24["start_value"], y24["end_value"], y24["net_contributions"], y24["gain"]), (2024, 0.0, 1000.0, 1000.0, 0.0))
        self.assertEqual((y25["year"], y25["start_value"], y25["end_value"], y25["net_contributions"]), (2025, 1000.0, 1650.0, 500.0))
        self.assertAlmostEqual(y25["gain"], 150.0)                       # 1650 - 1000 - 500
        self.assertTrue(y24["partial"] and y25["partial"])
        self.assertAlmostEqual(m["windows"]["YTD"], y25["twr"])
        self.assertAlmostEqual(y25["twr"], 0.10, places=9)                 # 1650 / (1000 + 500) - 1

    def test_ytd_when_history_starts_this_year_is_since_start(self):
        s = mk(date(2025, 2, 1), [1000.0, 1000.0, 1100.0], buys={0: 1000.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["windows"]["YTD"], 0.10)

    def test_trailing_windows_need_enough_history(self):
        s = mk(date(2025, 1, 1), [1000.0] * 40 + [1100.0], buys={0: 1000.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["windows"]["1M"], 0.10)
        self.assertIsNone(m["windows"]["3M"])
        self.assertIsNone(m["windows"]["12M"])


class TestRisk(unittest.TestCase):
    def test_max_drawdown(self):
        s = mk(date(2025, 1, 1), [100.0, 120.0, 90.0, 110.0], buys={0: 100.0})
        m = metrics.compute(s)
        self.assertAlmostEqual(m["drawdown"]["depth"], 90.0 / 120.0 - 1.0)
        self.assertEqual((m["drawdown"]["peak"], m["drawdown"]["trough"]), ("2025-01-02", "2025-01-03"))
        self.assertAlmostEqual(m["drawdown"]["current"], 110.0 / 120.0 - 1.0)

    def test_drawdown_not_caused_by_withdrawals(self):
        s = mk(date(2025, 1, 1), [1000.0, 1000.0, 400.0], buys={0: 1000.0}, sells={2: 600.0})      # wyplata 600 przy stalej cenie
        self.assertAlmostEqual(metrics.compute(s)["drawdown"]["depth"], 0.0)

    def test_drawdown_episodes_with_profit_change_and_recovery(self):
        s = mk(date(2025, 1, 1), [100.0, 120.0, 90.0, 110.0, 130.0, 100.0], buys={0: 100.0})
        eps = metrics.compute(s)["drawdown_episodes"]
        self.assertEqual(len(eps), 2)
        e1, e2 = eps                                                           # posortowane od najglebszego: -25% (zakonczone), -23,08% (trwa)
        self.assertAlmostEqual(e1["depth"], 90.0 / 120.0 - 1.0)
        self.assertEqual((e1["peak"], e1["trough"], e1["recovered"], e1["ongoing"]), ("2025-01-02", "2025-01-03", "2025-01-05", False))
        self.assertAlmostEqual(e1["profit_change"], -30.0)                      # zysk 20 -> -10
        self.assertAlmostEqual(e2["depth"], 100.0 / 130.0 - 1.0)
        self.assertEqual((e2["peak"], e2["trough"], e2["recovered"], e2["ongoing"]), ("2025-01-05", "2025-01-06", None, True))
        self.assertAlmostEqual(e2["profit_change"], -30.0)                      # zysk 30 -> 0

    def test_profit_drawdown_in_pln_ignores_deposits(self):
        # dzien 2: dokupienie 1000 po cenie wyceny nie zmienia zysku; zysk 0 -> 300 -> 100 -> 250
        s = mk(date(2025, 1, 1), [1000.0, 1300.0, 2100.0, 2250.0], buys={0: 1000.0, 2: 1000.0})          # zysk dnia 3: 2100 - 2000 = 100
        pd_ = metrics.compute(s)["profit_drawdown"]
        self.assertAlmostEqual(pd_["amount"], -200.0)
        self.assertEqual((pd_["peak"], pd_["trough"]), ("2025-01-02", "2025-01-03"))
        self.assertAlmostEqual(pd_["peak_gain"], 300.0)
        self.assertAlmostEqual(pd_["trough_gain"], 100.0)
        self.assertAlmostEqual(pd_["current"], (2250.0 - 2000.0) - 300.0)       # 250 - 300
    def test_volatility_zero_for_constant_growth_and_none_for_short_history(self):
        vals = [1000.0 * (1.001 ** i) for i in range(80)]
        s = mk(date(2025, 1, 6), vals, buys={0: 1000.0})
        m = metrics.compute(s)
        self.assertLess(m["volatility"], 1e-6)
        short = metrics.compute(mk(date(2025, 1, 6), vals[:10], buys={0: 1000.0}))
        self.assertIsNone(short["volatility"])

    def test_volatility_positive_for_alternating_returns(self):
        vals, v = [], 1000.0
        for i in range(120):
            v *= 1.01 if i % 2 else 0.99
            vals.append(v)
        m = metrics.compute(mk(date(2025, 1, 6), vals, buys={0: 1000.0}))
        self.assertGreater(m["volatility"], 0.1)

    def test_estimated_days_are_counted(self):
        s = mk(date(2025, 1, 1), [100.0, 110.0, 120.0], buys={0: 100.0}); s[1]["estimated_positions"] = 1
        self.assertEqual(metrics.compute(s)["estimated_days"], 1)


if __name__ == "__main__":
    unittest.main()
