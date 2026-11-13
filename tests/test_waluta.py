"""
Waluta prezentacji - opcja 2 (pricing/currency.py, docs/WALUTA.md): kazdy przeplyw po kursie ze swojego dnia, wartosc biezaca po kursie teraz.
Wartosci oczekiwane liczone recznie (przyklad z dokumentu: 400 zl przy 4,00 = 100 $, dzis 396 zl przy 3,60 = 110 $ -> +10 $, +10%).
"""
import os, sys, unittest
from datetime import date, timedelta
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.core import cash  # noqa: E402
from invapp.pricing import audit, currency, history, metrics  # noqa: E402
from test_history import HistBase, span  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule

D0 = date(2025, 3, 3)


class CurBase(HistBase):
    def setUp(self):
        super().setUp()
        self.l = self.instrument("AAA.PL", "PLN")

    def live_fx(self, rate):
        self.cur.execute("insert into fx_quotes(base,quote,rate,provider,quoted_at) values ('USD','PLN',%s,'YAHOO',now())", (rate,))


class TestLotsAndIncome(CurBase):
    def test_users_example_cost_at_purchase_rate_value_at_rate_now(self):
        self.buy(self.l, D0, 1, 400)
        self.store_fx("USD", {D0: D("4.00")})
        r = currency.Rates(self.cur, "USD")
        lots = currency.lots_in(self.cur, [self.acct], r)[self.l]
        self.assertEqual((lots["purchases"], lots["cost_left"]), (D(100), D(100)))
        value_w = D(396) / D("3.60")
        self.assertEqual((value_w - lots["cost_left"]).quantize(D("0.01")), D("10.00"))           # +10 $ (w PLN: -4 zl)

    def test_realized_uses_sell_day_rate_for_proceeds_and_buy_day_rate_for_cost(self):
        lot = self.buy(self.l, D0, 10, 1000)
        self.sell(self.l, lot, D0 + timedelta(days=10), 4, 440, 400)
        self.cur.execute("update derived_lots set qty_remaining = 6 where lot_id = %s", (lot,))
        self.store_fx("USD", {D0: D("4.00"), D0 + timedelta(days=10): D("4.40")})
        lo = currency.lots_in(self.cur, [self.acct], currency.Rates(self.cur, "USD"))[self.l]
        self.assertEqual(lo["sales"], D(100))                      # 440 / 4,40
        self.assertEqual(lo["realized"], D(0))                     # 100 $ - 400 / 4,00 (w PLN +40 zl)
        self.assertEqual(lo["cost_left"], D(150))                  # 600 / 4,00
        self.assertEqual(lo["purchases"] - lo["sales"], lo["cost_left"] - lo["realized"])     # tozsamosc: zakupy - sprzedaze = koszt otwartych - zrealizowany

    def test_income_each_at_its_day_and_missing_rate_is_flagged(self):
        div = self.instrument("DIV.PL", "PLN")
        self.tx(self.acct, self.xtb, "DIVIDEND", D(100), f"{D0.isoformat()}T12:00Z", listing=div)
        self.tx(self.acct, self.xtb, "TAX_WITHHOLDING", D(-19), f"{D0.isoformat()}T12:00Z", listing=div)
        self.tx(self.acct, self.xtb, "INTEREST", D(50), f"{(D0 + timedelta(days=3)).isoformat()}T12:00Z")
        self.store_fx("USD", {D0: D("4.00"), D0 + timedelta(days=3): D("5.00")})
        inc = currency.income_in(self.cur, [self.acct], currency.Rates(self.cur, "USD"))
        self.assertEqual((inc["dividends_gross"], inc["withholding_tax"], inc["interest"], inc["missing"]), (D(25), D("-4.75"), D(10), False))
        self.tx(self.acct, self.xtb, "INTEREST", D(50), "2024-01-01T12:00Z")                          # brak kursu z tego dnia
        self.assertTrue(currency.income_in(self.cur, [self.acct], currency.Rates(self.cur, "USD"))["missing"])


    def test_rate_older_than_7_days_is_missing(self):
        self.store_fx("USD", {D0: D("4.00")})
        r = currency.Rates(self.cur, "USD")
        self.assertEqual(r.on(D0 + timedelta(days=7)), D("4.00"))
        self.assertIsNone(r.on(D0 + timedelta(days=8)))


class TestLiveAndSeries(CurBase):
    def test_convert_live_identities(self):
        lot = self.buy(self.l, D0, 10, 1000)
        self.sell(self.l, lot, D0 + timedelta(days=5), 5, 600, 500)
        self.cur.execute("update derived_lots set qty_remaining = 5 where lot_id = %s", (lot,))
        div = self.instrument("DIV.PL", "PLN")
        self.tx(self.acct, self.xtb, "DIVIDEND", D(40), f"{(D0 + timedelta(days=6)).isoformat()}T12:00Z", listing=div)
        self.store_fx("USD", span(D0, 10, lambda i: D("4.00") + D("0.10") * i))
        self.live_fx(D("3.80"))
        live = {"scope": {"securities_value": D(700), "deposits_value": D(0), "assets_value": D(700), "total_value": D(700), "cash_value": 0,
                          "xtb_free_value": 0, "deposit_interest_unbooked": 0, "income_12m": {}},
                "positions": [{"listing_id": self.l, "qty": D(5), "value": D(700), "price_pln": D(140), "cost_left": D(500), "accounts": [],
                               "prev_price_pln": None}], "deposits": []}
        w = currency.convert_live(self.cur, live, "USD", [self.acct], today=D0 + timedelta(days=9))
        s = w["scope"]
        self.assertEqual(s["securities_value"], D(700) / D("3.80"))
        self.assertEqual(s["securities_cost"], D(500) / D("4.00"))
        self.assertEqual(s["realized_gain"], D(600) / D("4.50") - D(500) / D("4.00"))
        self.assertEqual(s["dividends_gross"], D(40) / D("4.60"))
        q = lambda v: v.quantize(D("1e-12"))  # noqa: E731
        self.assertEqual(q(s["investment_gain"]), q(s["securities_value"] - (s["total_purchases"] - s["total_sales"])))       # jak kontrola w audit.py
        self.assertEqual(q(s["total_result"]), q(s["investment_gain"] + s["additional_income"]))
        self.assertEqual(q(w["positions"][0]["avg_cost"] * 5), q(s["securities_cost"]))
        self.assertFalse(w["brak_kursu"])

    def test_series_flows_use_rate_of_their_day(self):
        rates = span(D0, 30, lambda i: D("4.00") if i < 20 else D("5.00"))
        self.buy(self.l, D0, 10, 400); self.buy(self.l, D0 + timedelta(days=20), 10, 500)
        self.store_prices(self.l, "PLN", span(D0, 30, lambda i: D(40) if i < 20 else D(50)))
        self.store_fx("USD", rates)
        s = history.build_series(tm._conn, "BROKER:XTB", end=D0 + timedelta(days=29))
        last = currency.convert_series(s["series"], currency.Rates(self.cur, "USD"))["series"][-1]
        self.assertEqual(last["invested_net"], D(100) + D(100))                   # 400 / 4 + 500 / 5

    def test_series_in_currency_flat_usd_price_gives_zero_twr(self):
        """Akcja o stalej cenie w USD kupiona w PLN: w PLN zwrot zalezy od kursu, w USD TWR = 0 i zysk = 0."""
        days = 40
        rates = span(D0, days, lambda i: D("4.00") + D("0.01") * i)
        self.buy(self.l, D0, 10, 400)                                             # 40 zl = 10 $ za sztuke
        self.store_prices(self.l, "PLN", {d: D(10) * r for d, r in rates.items()})
        self.store_fx("USD", rates)
        s = history.build_series(tm._conn, "BROKER:XTB", end=D0 + timedelta(days=days - 1))
        w = currency.convert_series(s["series"], currency.Rates(self.cur, "USD"))
        self.assertEqual(w["estimated_fx_days"], 0)
        last = w["series"][-1]
        self.assertEqual((last["value"].quantize(D("0.0001")), last["invested_net"]), (D("100.0000"), D(100)))
        m_w, m_pln = metrics.compute(w["series"]), metrics.compute(s["series"])
        self.assertAlmostEqual(m_w["twr_total"], 0.0, places=9)
        self.assertGreater(m_pln["twr_total"], 0.09)                              # w PLN +9,75% (sam kurs)


class TestSettingAndAudit(CurBase):
    def test_audit_flags_app_conversion_that_differs_from_sql(self):
        from unittest import mock
        lot = self.buy(self.l, D0, 10, 1000)
        self.sell(self.l, lot, D0 + timedelta(days=5), 5, 600, 500)
        self.store_fx("USD", span(D0, 10, lambda i: D("4.00") + D("0.10") * i)); self.live_fx(D("3.90"))
        cash.set_setting(self.cur, "display_currency", "USD")
        real = currency.lots_in
        def wrong(*a, **k):
            out = real(*a, **k)
            for v in out.values():
                v["realized"] += D(5)
            return out
        with mock.patch.object(currency, "lots_in", wrong):
            chk = next(c for c in audit.run(tm._conn, "BROKER:XTB", ("XTB",))["checks"] if c["kontrola"].startswith("Waluta USD: zakupy"))
        self.assertEqual(chk["status"], "BLAD", chk)

    def test_default_is_pln_and_only_known_currencies(self):
        self.assertEqual(currency.display_currency(self.cur), "PLN")
        self.assertEqual(cash.set_setting(self.cur, "display_currency", "USD")["display_currency"], "USD")
        with self.assertRaises(cash.CashError):
            cash.set_setting(self.cur, "display_currency", "BTC")

    def test_audit_checks_currency_identities_only_when_not_pln(self):
        lot = self.buy(self.l, D0, 10, 1000)
        self.sell(self.l, lot, D0 + timedelta(days=5), 5, 600, 500)
        self.cur.execute("update derived_lots set qty_remaining = 5 where lot_id = %s", (lot,))
        self.store_fx("USD", span(D0, 10, lambda i: D("4.00") + D("0.10") * i)); self.live_fx(D("3.90"))
        names = lambda: [c["kontrola"] for c in audit.run(tm._conn, "BROKER:XTB", ("XTB",))["checks"] if "USD" in c["kontrola"]]  # noqa: E731
        self.assertEqual(names(), [])
        cash.set_setting(self.cur, "display_currency", "USD")
        checks = [c for c in audit.run(tm._conn, "BROKER:XTB", ("XTB",))["checks"] if "USD" in c["kontrola"]]
        self.assertTrue(checks)
        self.assertTrue(all(c["status"] == "OK" for c in checks), checks)


if __name__ == "__main__":
    unittest.main()
