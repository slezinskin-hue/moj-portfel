"""
Testy liczb wersji 0.4 (pricing/positions.py, metrics.monthly_returns, kontrola w audit.py) - kazda liczba przeliczona INNA droga:
* "Dzis" pozycji: cena wczorajsza = cena tego dnia na wykresie portfela (history.build_series), zmiana = wartosc - ilosc x cena wczoraj;
* sredni koszt = koszt / ilosc;
* zamkniete pozycje: suma zysku = zysk zrealizowany z podsumowania zakresu (v_scope_summary), kazda = przychod - koszt;
* dochody 12 mies.: granice okna (365 dni) i skladniki;
* mapa miesiecznych zwrotow: iloczyn miesiecy = TWR roku z tabeli "Rok do roku", miesiac = lancuch dziennych stop liczony recznie.
"""
import os, sys, unittest
from datetime import date, timedelta
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.pricing import audit, history, metrics, positions  # noqa: E402
from test_history import HistBase, span  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class TestDayChangeAndAverage(HistBase):
    def test_previous_close_is_the_same_price_as_on_the_portfolio_chart(self):
        pl, us = self.instrument("AAA.PL", "PLN"), self.instrument("BBB.US", "USD")
        d0 = date(2025, 3, 3)
        self.buy(pl, d0, 10, 1000); self.buy(us, d0, 4, 1600)
        self.store_prices(pl, "PLN", span(d0, 10, lambda i: D(100) + i))
        self.store_prices(us, "USD", {d: p for d, p in span(d0, 10, lambda i: D(100) + 2 * i).items() if d.weekday() < 5})   # bez weekendow
        self.store_fx("USD", span(d0, 10, lambda i: D("4.00") + D("0.01") * i))
        for ref in (d0 + timedelta(days=4), d0 + timedelta(days=6)):                      # piatek i niedziela (zamkniecie z piatku, kurs z niedzieli)
            s = history.build_series(tm._conn, "BROKER:XTB", end=ref)
            chart_value = self.day(s, ref)["securities"]
            prev = positions.prev_close_pln(self.cur, [pl, us], ref)
            self.assertEqual((10 * prev[pl][0] + 4 * prev[us][0]).quantize(D("0.01")), chart_value, ref)
        self.assertEqual(prev[us][1], d0 + timedelta(days=4))                             # niedziela: zamkniecie z piatku

    def test_stale_or_missing_close_gives_no_day_change(self):
        a, b = self.instrument("OLD.PL", "PLN"), self.instrument("FX.US", "USD")
        self.store_prices(a, "PLN", {date(2025, 1, 1): D(10)})
        self.store_prices(b, "USD", {date(2025, 1, 9): D(10)})                           # brak kursu USD
        self.assertEqual(positions.prev_close_pln(self.cur, [a, b], date(2025, 1, 9)), {})
        self.assertIn(a, positions.prev_close_pln(self.cur, [a], date(2025, 1, 8)))       # 7 dni: jeszcze aktualne

    def test_day_change_average_cost_and_only_market_prices(self):
        l = self.instrument("CCC.PL", "PLN"); today = date(2025, 5, 7)
        self.store_prices(l, "PLN", {date(2025, 5, 6): D("50.00")})
        rows = [{"listing_id": l, "qty": D(3), "cost_left": D("130.00"), "value": D("156.30"), "price_source": "MARKET"},
                {"listing_id": l, "qty": D(3), "cost_left": D("130.00"), "value": D("156.30"), "price_source": "XTB_EXPORT"}]
        info = positions.add_day_and_average(self.cur, rows, today=today)
        self.assertEqual(info["ref_date"], "2025-05-06")
        m, x = rows
        self.assertEqual(m["day_change"], D("6.30"))                                       # 156,30 - 3 x 50
        self.assertEqual(m["day_change_pct"], D("6.30") / D("150.00"))
        self.assertEqual((m["avg_cost"] * 3).quantize(D("0.01")), D("130.00"))
        self.assertEqual(m["prev_date"], "2025-05-06")
        self.assertIsNone(x["day_change"])                                                 # wycena z eksportu XTB: bez "Dzis"
        self.assertEqual(x["avg_cost"], m["avg_cost"])


class TestClosedPositions(HistBase):
    def test_sum_equals_realized_gain_of_scope_and_flags_partial(self):
        a, b = self.instrument("SELL.PL", "PLN"), self.instrument("PART.PL", "PLN")
        d0 = date(2025, 2, 3)
        la = self.buy(a, d0, 10, 1000); lb = self.buy(b, d0, 10, 500)
        self.sell(a, la, d0 + timedelta(days=20), 10, 1300, 1000)
        self.sell(b, lb, d0 + timedelta(days=30), 4, 180, 200)
        self.cur.execute("update derived_lots set qty_remaining = 0 where lot_id = %s", (la,))
        self.cur.execute("update derived_lots set qty_remaining = 6 where lot_id = %s", (lb,))
        out = {c["ticker"]: c for c in positions.closed_positions(self.cur, [self.acct])}
        self.assertEqual((out["SELL.PL"]["realized"], out["SELL.PL"]["closed"]), (D(300), True))
        self.assertEqual((out["PART.PL"]["realized"], out["PART.PL"]["closed"], out["PART.PL"]["qty_open"]), (D(-20), False, D(6)))
        self.assertEqual(out["SELL.PL"]["realized_pct"], D("0.3"))
        realized = self.one("select realized_gain from v_scope_summary where scope_code = 'BROKER:XTB'")
        self.assertEqual(sum(c["realized"] for c in out.values()), realized)
        self.assertEqual([c["ticker"] for c in positions.closed_positions(self.cur, [self.acct])], ["PART.PL", "SELL.PL"])   # najnowsza sprzedaz pierwsza
        chk = next(c for c in audit.run(tm._conn, "BROKER:XTB", ("XTB",))["checks"] if c["kontrola"].startswith("Zamknięte pozycje"))
        self.assertEqual(chk["status"], "OK", chk)

    def test_audit_detects_inconsistent_closure(self):
        a = self.instrument("BAD.PL", "PLN"); d0 = date(2025, 2, 3)
        la = self.buy(a, d0, 10, 1000)
        t = self.tx(self.acct, self.xtb, "SELL", D(500), "2025-03-01T10:00Z", listing=a, qty=D(5), price=D(100))
        self.cur.execute("insert into derived_lot_closures(lot_id,sell_txn_id,qty_closed,cost_cash,proceeds_cash,realized_pnl) values (%s,%s,5,500,500,7)", (la, t))
        chk = next(c for c in audit.run(tm._conn, "BROKER:XTB", ("XTB",))["checks"] if c["kontrola"].startswith("Zamknięte pozycje"))
        self.assertEqual(chk["status"], "BLAD")
        self.assertIn("BAD.PL", chk["szczegoly"])

    def test_position_chart_has_prices_and_trades_of_scope_only(self):
        a = self.instrument("CH.PL", "PLN"); d0 = date(2025, 4, 1)
        la = self.buy(a, d0, 10, 1000); self.sell(a, la, d0 + timedelta(days=5), 2, 220, 200)
        other = self.account(self.xtb, "INNE", scopes=())
        self.tx(other, self.xtb, "BUY", D(-50), "2025-04-02T10:00Z", listing=a, qty=D(1), price=D(50))
        self.store_prices(a, "PLN", span(d0 - timedelta(days=40), 50, lambda i: D(100)))
        ch = positions.position_chart(self.cur, a, [self.acct])
        self.assertEqual([t["typ"] for t in ch["transakcje"]], ["BUY", "SELL"])
        self.assertEqual(ch["waluta"], "PLN")
        self.assertEqual(ch["ceny"][0][0], (d0 - timedelta(days=31)).isoformat())          # od miesiaca przed pierwsza transakcja


class TestIncome12m(HistBase):
    def test_window_is_last_365_days_and_sums_net_income(self):
        today = date(2025, 12, 31); l = self.instrument("DIV.PL", "PLN")
        for days, typ, amt in ((365, "DIVIDEND", 1000), (364, "DIVIDEND", 100), (364, "TAX_WITHHOLDING", -19), (10, "INTEREST", 50), (10, "TAX_INTEREST", "-9.50"),
                               (5, "REWARD", 20), (3, "FEE", -7), (-1, "DIVIDEND", 500)):
            self.tx(self.acct, self.xtb, typ, D(amt), f"{(today - timedelta(days=days)).isoformat()}T12:00Z",
                    listing=l if typ in ("DIVIDEND", "TAX_WITHHOLDING") else None)
        r = positions.income_12m(self.cur, [self.acct], today=today)
        self.assertEqual((r["dividends"], r["interest"], r["rewards"], r["total"]), (D(81), D("40.50"), D(20), D("141.50")))
        self.assertEqual((r["from"], r["to"]), ("2025-01-01", "2025-12-31"))
        # te same dywidendy co we wskazniku "stopa dywidendy (12 m)" (metrics: ostatnie 365 dni do dnia serii)
        inc = [(today - timedelta(days=364), 100.0), (today - timedelta(days=364), -19.0), (today - timedelta(days=365), 1000.0)]
        s = [{"date": (today - timedelta(days=1)).isoformat(), "value": 1, "invested_net": 1, "invested_gross": 1},
             {"date": today.isoformat(), "value": 1, "invested_net": 1, "invested_gross": 1}]
        self.assertAlmostEqual(metrics.compute(s, income=inc)["income_12m"], float(r["dividends"]))


class TestMonthlyReturns(unittest.TestCase):
    def test_months_multiply_to_year_and_match_manual_chain(self):
        start, vals, v = date(2024, 11, 20), [], 1000.0
        for i in range(500):
            v *= 1.0 + (0.003 if i % 7 < 4 else -0.004) + (0.02 if i == 100 else 0.0)
            vals.append(v)
        s = [{"date": (start + timedelta(days=i)).isoformat(), "value": x, "invested_gross": 1000.0, "invested_net": 1000.0} for i, x in enumerate(vals)]
        for p in s[200:]:                                                                   # wplata 500 w trakcie (TWR jej nie liczy jako zysk)
            p["invested_gross"] = p["invested_net"] = 1500.0; p["value"] += 500.0
        m = metrics.compute(s)
        by_year = {y["year"]: y for y in m["monthly"]}
        for y in m["years"]:
            self.assertAlmostEqual(by_year[y["year"]]["total"], y["twr"], places=12)
        # miesiac recznie: wartosc ostatniego dnia / (wartosc ostatniego dnia poprzedniego miesiaca + wplaty) przez lancuch dzienny
        pts = [(date.fromisoformat(p["date"]), p["value"], p["invested_net"]) for p in s]
        manual = 1.0
        for i in range(1, len(pts)):
            d, val, inv = pts[i]
            if (d.year, d.month) == (2025, 6):
                manual *= val / (pts[i - 1][1] + inv - pts[i - 1][2])
        self.assertAlmostEqual(by_year[2025]["months"][5], manual - 1.0, places=12)
        self.assertIsNone(by_year[2024]["months"][9])                                       # pazdziernik 2024: przed historia
        self.assertEqual(sorted(by_year), [2024, 2025, 2026])


if __name__ == "__main__":
    unittest.main()
