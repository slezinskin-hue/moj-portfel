"""
Testy historii cen i wykresu portfela (migracja 0011 + pricing/history.py) BEZ internetu: dostawca podstawiany.
Definicje: wplata = zakup, wyplata = sprzedaz (przychod); wolne srodki i dywidendy poza wykresem; wartosc = ilosc x zamkniecie x kurs.
"""
import json, os, sys, unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.pricing import history  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class FakeHistory(history.HistoryYahoo):
    """data: symbol -> (waluta Yahoo, {data: cena})."""
    def __init__(self, data):
        self.data = data
        super().__init__(fetch=self._fetch)

    def _fetch(self, url):
        sym = url.split("/chart/")[1].split("?")[0]
        if sym not in self.data:
            raise ValueError("brak")
        ccy, closes = self.data[sym]
        items = sorted(closes.items())
        ts = [int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp()) for d, _ in items]
        return json.dumps({"chart": {"result": [{"meta": {"currency": ccy, "gmtoffset": 0, "fullExchangeName": "TEST"}, "timestamp": ts,
                                                  "indicators": {"quote": [{"close": [float(p) for _, p in items]}]}}]}})


def span(start, days, fn):
    return {start + timedelta(days=i): fn(i) for i in range(days)}


class HistBase(tm.Base):
    def setUp(self):
        super().setUp()
        self.acct = self.account(self.xtb, "H1", scopes=("BROKER:XTB",))

    def instrument(self, symbol, ccy):
        s = self.one("insert into securities(name,asset_class) values (%s,'STOCK') returning security_id", (symbol,))
        l = self.one("insert into listings(security_id,display_name,trading_currency) values (%s,%s,%s) returning listing_id", (s, symbol, ccy))
        self.cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id) values (%s,%s,%s)", (self.xtb, symbol, l))
        return l

    def buy(self, l, day, qty, cost, adj=1):
        ts = f"{day.isoformat()}T10:00Z"
        t = self.tx(self.acct, self.xtb, "BUY", -D(cost), ts, listing=l, qty=D(qty), price=D(cost) / D(qty))
        ids = []
        if adj != 1:        # korekta CONFIRMED obejmujaca ten zakup (guard bazy wymaga rekordu korekty)
            ids = [self.one("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,ratio_new,ratio_old,source,status,evidence)
                               values (%s,'SPLIT',%s,'FROM_BROKER_DATA',%s,1,'BROKER_ARTIFACT','CONFIRMED','{"t":1}') returning action_id""", (l, day + timedelta(days=30), adj))]
        return self.one("""insert into derived_lots(account_id,listing_id,open_txn_id,opened_at,qty_as_reported,price_as_reported,split_factor,qty_initial,price_adjusted,qty_remaining,
                                                     cost_cash_initial,method,engine_version,adjustment_action_ids) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'POSITION_ID','t',%s) returning lot_id""",
                        (self.acct, l, t, ts, qty, D(cost) / D(qty), adj, D(qty) * adj, D(cost) / D(qty) / adj, D(qty) * adj, cost, ids))

    def sell(self, l, lot, day, qty, proceeds, cost_part):
        ts = f"{day.isoformat()}T10:00Z"
        t = self.tx(self.acct, self.xtb, "SELL", D(proceeds), ts, listing=l, qty=D(qty), price=D(proceeds) / D(qty))
        self.cur.execute("insert into derived_lot_closures(lot_id,sell_txn_id,qty_closed,cost_cash,proceeds_cash,realized_pnl) values (%s,%s,%s,%s,%s,%s)",
                         (lot, t, qty, cost_part, proceeds, D(proceeds) - D(cost_part)))

    def store_prices(self, l, ccy, closes):
        for d, p in closes.items():
            self.cur.execute("insert into prices(listing_id,price_date,close_price,currency,source) values (%s,%s,%s,%s,'YAHOO_1D')", (l, d, p, ccy))

    def store_fx(self, ccy, rates):
        for d, r in rates.items():
            self.cur.execute("insert into fx_rates(rate_date,base,quote,rate,source) values (%s,%s,'PLN',%s,'YAHOO_1D')", (d, ccy, r))

    def day(self, series, d):
        return next(x for x in series["series"] if x["date"] == d.isoformat())


class TestPortfolioSeries(HistBase):
    def test_users_example_buy_1000_then_worth_1200(self):
        l = self.instrument("AAA.PL", "PLN"); d0 = date(2025, 1, 1)
        self.buy(l, d0, 10, 1000)
        self.store_prices(l, "PLN", span(d0, 40, lambda i: D(100) + D(20) * min(i, 31) / 31))        # +20% po 31 dniach
        s = history.build_series(tm._conn, end=date(2025, 2, 9))
        self.assertEqual(s["first_date"], "2025-01-01")
        first = self.day(s, d0); feb1 = self.day(s, date(2025, 2, 1))
        self.assertEqual((first["invested_net"], first["value"]), (D("1000.00"), D("1000.00")))
        self.assertEqual((feb1["value"], feb1["invested_net"], feb1["gain"]), (D("1200.00"), D("1000.00"), D("200.00")))
        self.assertEqual(s["estimated_days"], 0)

    def test_series_starts_at_first_purchase_and_grows_with_next_purchases(self):
        l = self.instrument("AAA.PL", "PLN")
        self.buy(l, date(2025, 1, 1), 10, 1000); self.buy(l, date(2025, 1, 10), 5, 600)
        self.store_prices(l, "PLN", span(date(2025, 1, 1), 20, lambda i: D(100) + i))
        s = history.build_series(tm._conn, end=date(2025, 1, 15))
        self.assertEqual(s["series"][0]["date"], "2025-01-01")                                   # przed pierwszym zakupem brak punktow (portfel = 0)
        d9, d10 = self.day(s, date(2025, 1, 9)), self.day(s, date(2025, 1, 10))
        self.assertEqual((d9["invested_net"], d10["invested_net"], d10["invested_gross"]), (D("1000.00"), D("1600.00"), D("1600.00")))
        self.assertEqual((d9["positions"], d9["value"]), (1, D("1080.00")))                          # 10 x 108
        self.assertEqual(d10["value"], D("15") * D("109"))                                          # 15 szt. x 109

    def test_sale_reduces_net_invested_and_value(self):
        l = self.instrument("AAA.PL", "PLN"); lot = self.buy(l, date(2025, 1, 1), 10, 1000)
        self.sell(l, lot, date(2025, 1, 20), 4, 520, 400)                                         # sprzedaz 4 z 10 za 520 zl
        self.store_prices(l, "PLN", span(date(2025, 1, 1), 30, lambda i: D(100) + i))
        s = history.build_series(tm._conn, end=date(2025, 1, 25))
        d19, d20 = self.day(s, date(2025, 1, 19)), self.day(s, date(2025, 1, 20))
        self.assertEqual((d19["invested_net"], d19["value"]), (D("1000.00"), D("1180.00")))
        self.assertEqual((d20["sold"], d20["invested_net"], d20["invested_gross"]), (D("520.00"), D("480.00"), D("1000.00")))
        self.assertEqual(d20["value"], D("6") * D("119"))                                           # 6 szt. po 119
        self.assertEqual(d20["gain"], D("714.00") - D("480.00"))                                    # wynik uwzglednia zrealizowany zysk

    def test_foreign_currency_uses_daily_fx(self):
        l = self.instrument("US1.US", "USD"); d0 = date(2025, 3, 3)
        self.buy(l, d0, 2, 800)                                                                  # 2 szt. za 800 zl
        self.store_prices(l, "USD", {d0: D(100), d0 + timedelta(days=1): D(110)}); self.store_fx("USD", {d0: D("4.00"), d0 + timedelta(days=1): D("4.20")})
        s = history.build_series(tm._conn, end=d0 + timedelta(days=1))
        self.assertEqual((self.day(s, d0)["value"], self.day(s, d0 + timedelta(days=1))["value"]), (D("800.00"), D("924.00")))      # 2 x 110 x 4,20

    def test_weekend_carries_last_close_and_stale_prices_are_flagged(self):
        l = self.instrument("AAA.PL", "PLN"); self.buy(l, date(2025, 1, 6), 1, 100)
        self.store_prices(l, "PLN", {date(2025, 1, 6): D(100), date(2025, 1, 10): D(110)})              # pon i pt; sob/niedz bez sesji
        s = history.build_series(tm._conn, end=date(2025, 1, 25))
        self.assertEqual(self.day(s, date(2025, 1, 12))["value"], D("110.00"))                            # niedziela = piatkowe zamkniecie
        self.assertEqual(self.day(s, date(2025, 1, 12))["estimated_positions"], 0)
        self.assertEqual(self.day(s, date(2025, 1, 25))["estimated_positions"], 1)                        # >7 dni bez ceny = oszacowanie, jawnie oznaczone
        self.assertGreater(s["estimated_days"], 0)

    def test_missing_history_uses_last_trade_price_and_is_flagged(self):
        l = self.instrument("NOH.PL", "PLN"); self.buy(l, date(2025, 1, 1), 10, 1000)
        s = history.build_series(tm._conn, end=date(2025, 1, 5))
        d = self.day(s, date(2025, 1, 3))
        self.assertEqual((d["value"], d["estimated_positions"]), (D("1000.00"), 1))                        # po cenie zakupu, oznaczone jako oszacowanie
        self.assertEqual(s["no_history"], [l])

    def test_split_adjusted_quantity_matches_split_adjusted_prices(self):
        l = self.instrument("SPL.US", "USD")
        self.buy(l, date(2025, 1, 1), D("0.5"), 400, adj=10)                                          # po korekcie 5 szt.; ceny dostawcy juz skorygowane (80 zamiast 800)
        self.store_prices(l, "USD", {date(2025, 1, 1): D(80), date(2025, 1, 2): D(88)}); self.store_fx("USD", {date(2025, 1, 1): D(1), date(2025, 1, 2): D(1)})
        s = history.build_series(tm._conn, end=date(2025, 1, 2))
        self.assertEqual((self.day(s, date(2025, 1, 1))["value"], self.day(s, date(2025, 1, 2))["value"]), (D("400.00"), D("440.00")))

    def test_dividends_and_free_funds_are_not_part_of_the_chart(self):
        l = self.instrument("AAA.PL", "PLN"); self.buy(l, date(2025, 1, 1), 10, 1000)
        self.tx(self.acct, self.xtb, "CASH_DEPOSIT", 5000, "2024-12-20T10:00Z")
        self.tx(self.acct, self.xtb, "DIVIDEND", 50, "2025-01-10T10:00Z", listing=l)
        self.store_prices(l, "PLN", span(date(2025, 1, 1), 15, lambda i: D(100)))
        s = history.build_series(tm._conn, end=date(2025, 1, 12))
        d = self.day(s, date(2025, 1, 12))
        self.assertEqual((d["invested_net"], d["value"], d["gain"]), (D("1000.00"), D("1000.00"), D("0.00")))

    def test_empty_scope_gives_empty_series(self):
        self.assertEqual(history.build_series(tm._conn, scope_code="NET_WORTH")["series"], [])


class TestHistoryMappingAndRefresh(HistBase):
    def trades(self, l, prices):
        for d, p in prices:
            self.buy(l, d, 1, p)

    def test_verification_by_currency_and_trade_prices(self):
        ok = self.instrument("OKK.PL", "PLN"); badc = self.instrument("BAD.US", "USD"); badp = self.instrument("BAP.DE", "EUR"); unk = self.instrument("UNK.PL", None)
        for l, p in ((ok, 100), (badc, 100), (badp, 100), (unk, 100)):
            self.trades(l, [(date(2025, 1, 2), p), (date(2025, 1, 3), p), (date(2025, 1, 6), p), (date(2025, 1, 7), p)])
        flat = lambda v: span(date(2025, 1, 1), 10, lambda i: D(v))
        prov = FakeHistory({"OKK.WA": ("PLN", flat(101)), "BAD": ("EUR", flat(100)), "BAP.DE": ("EUR", flat(500)), "UNK.WA": ("PLN", flat(100))})
        res = {r["symbol"]: r for r in history.verify_history_mappings(tm._conn, prov, start=date(2025, 1, 1), pause=0)}
        self.assertEqual({k: v["status"] for k, v in res.items()}, {"OKK.PL": "VERIFIED", "BAD.US": "REJECTED", "BAP.DE": "REJECTED", "UNK.PL": "PENDING"})
        self.assertEqual(res["OKK.PL"]["matching"], "4/4")
        self.assertIn("waluta dostawcy", res["BAD.US"]["reason"]); self.assertIn("ceny zakupow", res["BAP.DE"]["reason"]); self.assertIn("niezweryfikowana", res["UNK.PL"]["reason"])

    def test_adjusted_lots_pass_and_undetected_split_is_rejected_with_hint(self):
        # NVDA-like: zakup po 800 (przed splitem 10:1), partia po korekcie CONFIRMED ma cene 80; dostawca podaje 80 -> VERIFIED
        l1 = self.instrument("SPL.US", "USD"); self.buy(l1, date(2025, 1, 2), D("1"), 800)
        self.buy(l1, date(2025, 1, 3), D("1"), 800, adj=10)
        # split niewykryty w ksiedze: zakupy po 500, dostawca (skorygowane) 100 -> REJECTED z podpowiedzia o splicie
        l2 = self.instrument("SON.US", "USD")
        for d in (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)):
            self.buy(l2, d, D("1"), 500)
        flat = lambda v: span(date(2025, 1, 1), 10, lambda i: D(v))
        res = {r["symbol"]: r for r in history.verify_history_mappings(tm._conn, FakeHistory({"SPL": ("USD", flat(80)), "SON": ("USD", flat(100))}), start=date(2025, 1, 1), pause=0)}
        self.assertEqual(res["SON.US"]["status"], "REJECTED"); self.assertIn("NIEWYKRYTY split", res["SON.US"]["reason"])
        self.assertEqual(res["SPL.US"]["status"], "REJECTED")        # jedna partia bez korekty (800 vs 80) i jedna z korekta: 1/2 < 75% - nie zgadujemy
        self.assertEqual(res["SPL.US"]["matching"], "1/2")

    def test_refresh_stores_prices_fx_and_revises_old_rows(self):
        l = self.instrument("OKK.US", "USD"); self.trades(l, [(date(2025, 1, 2), 100), (date(2025, 1, 3), 100)])
        base = span(date(2025, 1, 1), 5, lambda i: D(100))
        prov = FakeHistory({"OKK": ("USD", base), "USDPLN=X": ("PLN", span(date(2025, 1, 1), 5, lambda i: D("4.0")))})
        history.verify_history_mappings(tm._conn, prov, start=date(2025, 1, 1), pause=0)
        r = history.refresh_history(tm._conn, prov, start=date(2025, 1, 1), pause=0)
        self.assertEqual((r["listings"], r["price_rows"], r["fx"]["USD"], r["errors"]), (1, 5, 5, []))
        self.assertEqual(self.one("select count(*) from prices where source='YAHOO_1D'"), 5)
        prov2 = FakeHistory({"OKK": ("USD", span(date(2025, 1, 1), 5, lambda i: D(10))), "USDPLN=X": ("PLN", span(date(2025, 1, 1), 5, lambda i: D("4.0")))})     # dostawca skorygowal historie (np. split)
        history.refresh_history(tm._conn, prov2, start=date(2025, 1, 1), pause=0)
        self.assertEqual((self.one("select count(*) from prices where source='YAHOO_1D'"), self.one("select close_price from prices where price_date='2025-01-03'")), (5, D(10)))

    def test_pence_history_is_converted_to_pounds(self):
        l = self.instrument("GBP.UK", "GBP"); self.trades(l, [(date(2025, 1, 2), 2)])
        prov = FakeHistory({"GBP.L": ("GBp", span(date(2025, 1, 1), 4, lambda i: D(200)))})
        res = history.verify_history_mappings(tm._conn, prov, start=date(2025, 1, 1), pause=0)
        self.assertEqual(res[0]["status"], "VERIFIED")
        history.refresh_history(tm._conn, prov, start=date(2025, 1, 1), pause=0)
        self.assertEqual(self.one("select close_price from prices where listing_id=%s and price_date='2025-01-02'", (l,)), D(2))      # 200 pensow = 2 GBP

    def test_app_role_may_write_reference_prices_but_never_the_ledger(self):
        priv = "select has_table_privilege('investing_app', %s, %s)"
        for t in ("prices", "fx_rates"):
            self.assertTrue(self.one(priv, (t, "INSERT")), t); self.assertTrue(self.one(priv, (t, "UPDATE")), t); self.assertFalse(self.one(priv, (t, "DELETE")), t)
        for t in ("transactions", "derived_lots", "derived_lot_closures", "broker_closed_positions"):
            for p in ("INSERT", "UPDATE", "DELETE"):
                self.assertFalse(self.one(priv, (t, p)), f"{t} {p}")


if __name__ == "__main__":
    unittest.main()
