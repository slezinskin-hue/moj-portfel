"""
Testy cen na zywo (migracja 0010 + pakiet pricing) BEZ internetu: dostawca jest podstawiany (fake). Baza jak w pozostalych testach.
"""
import json, os, sys, unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.pricing import service  # noqa: E402
from invapp.pricing.providers import NbpProvider, ProviderError, YahooProvider  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule

SNAP = datetime(2026, 11, 11, 12, 16, tzinfo=timezone.utc)


class FakeYahoo(YahooProvider):
    """Dostawca bez sieci: odpowiedzi wg slownika symbol -> (cena, waluta Yahoo, czas)."""
    def __init__(self, data):
        self.data = data
        super().__init__(fetch=self._fetch)

    def _fetch(self, url):
        sym = url.split("/chart/")[1].split("?")[0]
        if sym not in self.data:
            raise ProviderError("brak")
        price, ccy, t = self.data[sym]
        return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": ccy, "regularMarketTime": int(t.timestamp()),
                                                           "fullExchangeName": "TEST", "marketState": "REGULAR"}}]}})


class PricingBase(tm.Base):
    def setUp(self):
        super().setUp()
        self.acct = self.account(self.xtb, "PX1", scopes=("BROKER:XTB",))
        self.batch = self.one("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,account_id,status)
                                 values (%s,'f',repeat('a',64),'p','1',%s,'COMMITTED') returning batch_id""", (self.xtb, self.acct))

    def instrument(self, symbol, ccy, qty, cost, broker_price, value, buy_at="2026-01-05T10:00Z"):
        s = self.one("insert into securities(name,asset_class) values (%s,'STOCK') returning security_id", (symbol,))
        l = self.one("insert into listings(security_id,display_name,trading_currency) values (%s,%s,%s) returning listing_id", (s, symbol, ccy))
        self.cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id) values (%s,%s,%s)", (self.xtb, symbol, l))
        t = self.tx(self.acct, self.xtb, "BUY", -cost, buy_at, listing=l, qty=D(qty), price=D(cost) / D(qty))
        self.cur.execute("""insert into derived_lots(account_id,listing_id,open_txn_id,opened_at,qty_as_reported,price_as_reported,split_factor,qty_initial,price_adjusted,
                                                     qty_remaining,cost_cash_initial,method,engine_version) values (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,'POSITION_ID','t')""",
                         (self.acct, l, t, buy_at, qty, D(cost) / D(qty), qty, D(cost) / D(qty), qty, cost))
        for lvl, ref in (("INSTRUMENT", None), ("POSITION", "p" + symbol)):
            self.cur.execute("""insert into broker_position_snapshots(batch_id,account_id,as_of,listing_id,row_level,broker_position_ref,volume,current_price,value_cash,net_profit_cash)
                                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (self.batch, self.acct, SNAP, l, lvl, ref, qty, broker_price if lvl == "POSITION" else None, value, D(value) - D(cost)))
        return l

    def fails(self, sql, args=None):
        self.cur.execute("savepoint sp")
        try:
            self.cur.execute(sql, args)
        except psycopg2.Error as e:
            self.cur.execute("rollback to savepoint sp"); return e
        self.fail("powinno zostac odrzucone")


class TestMappingVerification(PricingBase):
    def test_verified_rejected_pending(self):
        ok = self.instrument("AAA.US", "USD", 2, 400, D("100"), 800)
        bad_ccy = self.instrument("BBB.UK", "USD", 1, 100, D("50"), 200)
        bad_px = self.instrument("CCC.DE", "EUR", 1, 100, D("10"), 43)
        unknown = self.instrument("DDD.PL", None, 1, 100, D("10"), 10)
        later = SNAP + timedelta(hours=3)
        prov = FakeYahoo({"AAA": (105, "USD", later), "BBB.L": (5000, "GBp", later), "CCC.DE": (25, "EUR", later)})
        res = {r["symbol"]: r for r in service.verify_mappings(tm._conn, prov, pause=0)}
        self.assertEqual({k: v["status"] for k, v in res.items()}, {"AAA.US": "VERIFIED", "BBB.UK": "REJECTED", "CCC.DE": "REJECTED", "DDD.PL": "PENDING"})
        self.assertIn("waluta dostawcy GBp", res["BBB.UK"]["reason"])                  # pensy (GBp) != USD: odrzucone, nie przeliczane na sile
        self.assertIn("odbiega", res["CCC.DE"]["reason"])
        self.assertIn("niezweryfikowana", res["DDD.PL"]["reason"])
        self.assertEqual(self.one("select count(*) from market_quotes"), 1)               # pierwsze notowanie tylko dla VERIFIED
        m = self.rows("select * from listing_provider_symbols where listing_id=%s", (ok,))[0]
        self.assertEqual((m["provider_symbol"], m["quote_currency"], m["price_unit"]), ("AAA", "USD", "MAJOR"))
        self.assertEqual(m["evidence"]["price_ratio"], "1.0500")
        self.assertIsNone(self.one("select exchange_mic from listings where listing_id=%s", (ok,)))   # linia referencyjna != gielda XTB

    def test_verified_requires_currency_and_unit(self):
        l = self.instrument("EEE.US", "USD", 1, 10, D("10"), 10)
        e = self.fails("insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,checked_at,evidence) values (%s,'YAHOO','EEE','VERIFIED',now(),'{}')", (l,))
        self.assertIn("ck_lps_verified", str(e))


class TestLiveValuation(PricingBase):
    def setUp(self):
        super().setUp()
        self.us = self.instrument("AAA.US", "USD", 2, 400, D("100"), 800)          # 2 szt., migawka XTB 800 zl (kurs 4,00)
        self.pl = self.instrument("PPP.PL", "PLN", 10, 900, D("100"), 1000)         # 10 szt., migawka 1000 zl
        self.cur.execute("insert into fx_rates(rate_date,base,quote,rate,source) values ('2026-11-10','USD','PLN',3.90,'NBP_A')")

    def verify(self, data):
        service.verify_mappings(tm._conn, FakeYahoo(data), pause=0)

    def pos(self, l):
        return self.rows("select * from v_live_position_values where listing_id=%s", (l,))[0]

    def test_newer_quote_gives_live_value_with_live_fx(self):
        t = SNAP + timedelta(hours=4)
        self.verify({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)})
        service.refresh_quotes(tm._conn, FakeYahoo({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)}), pause=0)
        p = self.pos(self.us)
        self.assertEqual((p["value_source"], p["fx_source"]), ("LIVE", "YAHOO"))
        self.assertEqual(p["value_used"], D("902.0"))                                     # 2 x 110 x 4,10
        self.assertEqual(self.pos(self.pl)["value_used"], D(1010))
        r = self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]
        self.assertEqual((r["live_positions"], r["securities_value_live"]), (2, D("1912.0")))
        self.assertEqual(r["investment_gain_live"], D("1912.0") - D(1300))                # wartosc na zywo - koszt z gotowki
        self.assertEqual(r["residual_live"], D(0))

    def test_quote_older_than_broker_report_is_still_used_market_only_rule(self):
        old = SNAP - timedelta(days=1)                                                    # np. GPW zamknieta (swieto): ostatnia cena z API jest sprzed raportu XTB
        self.verify({"AAA": (101, "USD", old), "PPP.WA": (99, "PLN", old)})
        p = self.pos(self.pl)
        self.assertEqual((p["value_source"], p["value_used"], p["price_source"]), ("LIVE", D(990), "MARKET"))     # decyzja 2026-11-11: zawsze cena z API, nie z raportu XTB

    def test_nbp_rate_is_used_when_no_live_fx(self):
        t = SNAP + timedelta(hours=4)
        self.verify({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t)})
        p = self.pos(self.us)
        self.assertEqual((p["fx_source"], p["value_used"]), ("NBP_A", D("858.0")))      # 2 x 110 x 3,90

    def test_missing_mapping_falls_back_to_snapshot_and_is_counted(self):
        r0 = self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]
        self.assertEqual((r0["live_positions"], r0["snapshot_positions"], r0["securities_value_live"]), (0, 2, D(1800)))
        static = self.rows("select * from v_scope_result where scope_code='BROKER:XTB'")[0]
        self.assertEqual(r0["portfolio_value_live"], static["portfolio_value"])           # bez cen na zywo = wycena XTB co do grosza
        self.assertEqual(r0["investment_result_live"], static["investment_result"])

    def test_market_price_wins_even_when_report_is_newer_and_is_labelled(self):
        newer = SNAP + timedelta(hours=4); older = SNAP - timedelta(days=1)
        self.verify({"AAA": (110, "USD", newer), "PPP.WA": (99, "PLN", older)})
        us, pl = self.pos(self.us), self.pos(self.pl)
        self.assertEqual((us["price_source"], us["price_time_used"], us["price_used"]), ("MARKET", newer, D(110)))
        self.assertEqual((pl["price_source"], pl["price_time_used"], pl["price_used"]), ("MARKET", older, D(99)))        # raport XTB (nowszy) nie wypiera ceny z API

    def test_xtb_report_price_is_only_a_fallback_and_is_counted(self):
        self.verify({"AAA": (110, "USD", SNAP + timedelta(hours=4))})                    # PPP.PL bez notowania (brak symbolu/blad pobrania)
        pl = self.pos(self.pl)
        self.assertEqual((pl["price_source"], pl["value_source"], pl["value_used"]), ("XTB_REPORT", "SNAPSHOT", D(1000)))
        r = self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]
        self.assertEqual((r["live_positions"], r["snapshot_positions"]), (1, 1))

    def test_xtb_report_price_in_foreign_currency_uses_latest_live_fx(self):
        self.cur.execute("insert into fx_quotes(base,quote,rate,provider,quoted_at) values ('USD','PLN',4.10,'YAHOO',%s)", (SNAP + timedelta(hours=5),))
        p = self.pos(self.us)
        self.assertEqual((p["price_source"], p["value_source"], p["fx_used"], p["value_used"]), ("XTB_REPORT", "SNAPSHOT", D("4.10"), D("820.0")))     # 2 x 100 x 4,10: jedna metoda dla calego portfela
        r = self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]
        self.assertEqual(r["securities_value_live"], D(820) + D(1000))
        self.assertEqual(r["residual_live"], D(0))

    def setting(self, key, value):
        from invapp.core import cash
        cash.set_setting(self.cur, key, value)

    def test_setting_xtb_export_values_exactly_as_report_while_qty_matches(self):
        t = SNAP + timedelta(hours=4)
        self.verify({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)})
        service.refresh_quotes(tm._conn, FakeYahoo({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)}), pause=0)
        self.assertEqual(self.pos(self.pl)["value_used"], D(1010))                           # domyslnie: Yahoo
        self.setting("price_source", "XTB_EXPORT")
        us, pl = self.pos(self.us), self.pos(self.pl)
        self.assertEqual((us["value_source"], us["price_source"], us["value_used"]), ("BROKER", "XTB_EXPORT", D(800)))   # dokladnie jak w raporcie XTB
        self.assertEqual(pl["value_used"], D(1000))
        r = self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]
        self.assertEqual((r["securities_value_live"], r["snapshot_positions"], r["live_positions"]), (D(1800), 0, 0))     # wybrane ustawieniem != "brak ceny"
        self.assertEqual(r["residual_live"], D(0))
        # zakup po eksporcie (inna ilosc niz w raporcie) -> raport nieaktualny dla tej pozycji -> cena rynkowa
        self.cur.execute("update derived_lots set qty_as_reported = 3, qty_initial = 3, qty_remaining = 3 where listing_id = %s", (self.us,))
        us = self.pos(self.us)
        self.assertEqual((us["value_source"], us["value_used"]), ("LIVE", D("1353.0")))           # 3 x 110 x 4,10
        self.setting("price_source", "YAHOO")
        self.assertEqual(self.pos(self.pl)["value_used"], D(1010))

    def test_setting_nbp_fx(self):
        t = SNAP + timedelta(hours=4)
        self.verify({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)})
        service.refresh_quotes(tm._conn, FakeYahoo({"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)}), pause=0)
        self.assertEqual((self.pos(self.us)["fx_source"], self.pos(self.us)["value_used"]), ("YAHOO", D("902.0")))
        self.setting("fx_source", "NBP")
        p = self.pos(self.us)
        self.assertEqual((p["fx_source"], p["fx_used"], p["value_used"]), ("NBP_A", D("3.90"), D("858.0")))          # 2 x 110 x 3,90
        from invapp.pricing import audit
        a = audit.run(tm._conn, "BROKER:XTB", ("XTB",))
        self.assertEqual(a["checks"][0]["status"], "OK", a["checks"][0])                      # kontrola liczy wg tego samego ustawienia
        self.setting("price_source", "XTB_EXPORT")
        a = audit.run(tm._conn, "BROKER:XTB", ("XTB",))
        self.assertEqual(a["checks"][0]["status"], "OK", a["checks"][0]); self.assertIn("eksportu XTB", a["checks"][0]["kontrola"])

    def test_without_live_fx_xtb_report_value_is_exact(self):
        p = self.pos(self.us)                                                            # bez notowan i kursow na zywo (tylko awaryjnie raport XTB)
        self.assertEqual((p["price_source"], p["value_used"]), ("XTB_REPORT", D(800)))               # kurs z raportu XTB -> co do grosza jak w raporcie
        static = self.rows("select * from v_scope_result where scope_code='BROKER:XTB'")[0]
        self.assertEqual(self.rows("select * from v_scope_result_live where scope_code='BROKER:XTB'")[0]["portfolio_value_live"], static["portfolio_value"])

    def test_pence_quote_is_divided_by_100(self):
        l = self.instrument("GGG.UK", "GBP", 10, 100, D("2"), 100)
        t = SNAP + timedelta(hours=1)
        self.verify({"GGG.L": (210, "GBp", t), "GBPPLN=X": (5.0, "PLN", t)})
        service.refresh_quotes(tm._conn, FakeYahoo({"GGG.L": (210, "GBp", t), "GBPPLN=X": (5.0, "PLN", t)}), pause=0)
        p = self.pos(l)
        self.assertEqual((p["price_unit"], p["value_used"]), ("MINOR_1_100", D("105.0")))   # 10 x 2,10 GBP x 5,00

    def test_quotes_are_append_only_and_duplicates_ignored(self):
        t = SNAP + timedelta(hours=4)
        data = {"AAA": (110, "USD", t), "PPP.WA": (101, "PLN", t), "USDPLN=X": (4.10, "PLN", t)}
        self.verify(data)
        r1 = service.refresh_quotes(tm._conn, FakeYahoo(data), pause=0)
        r2 = service.refresh_quotes(tm._conn, FakeYahoo(data), pause=0)
        self.assertEqual((r1["quotes_new"], r2["quotes_new"], r2["quotes_same"]), (0, 0, 2))      # verify juz zapisal te notowania
        self.assertIn("tylko do dopisywania", str(self.fails("update market_quotes set price = 1")))
        self.assertIn("tylko do dopisywania", str(self.fails("delete from fx_quotes")))

    def test_rejected_mapping_is_never_used(self):
        t = SNAP + timedelta(hours=4)
        self.verify({"AAA": (500, "USD", t), "PPP.WA": (101, "PLN", t)})                # 500 vs 100: odrzucone
        self.cur.execute("""insert into market_quotes(listing_id,provider,provider_symbol,price,currency,price_unit,quoted_at) values (%s,'YAHOO','AAA',500,'USD','MAJOR',%s)""", (self.us, t))
        p = self.pos(self.us)
        self.assertEqual((p["value_source"], p["value_used"]), ("SNAPSHOT", D(800)))


class TestNbpAndPrivileges(PricingBase):
    def test_nbp_table_is_stored_once(self):
        body = json.dumps([{"no": "219/A/NBP/2026", "effectiveDate": "2026-11-10", "rates": [{"code": "USD", "mid": 3.9099}, {"code": "EUR", "mid": 4.3895}]}])
        nbp = NbpProvider(fetch=lambda url: body)
        self.assertEqual(service.refresh_nbp(tm._conn, nbp)["added"], 2)
        self.assertEqual(service.refresh_nbp(tm._conn, nbp)["added"], 0)
        self.assertEqual(self.one("select rate from fx_rates where base='USD' and source='NBP_A'"), D("3.9099"))

    def test_app_role_can_only_append_reference_prices(self):
        priv = "select has_table_privilege('investing_app', %s, %s)"
        for t in ("market_quotes", "fx_quotes"):                                    # notowania na zywo: tylko dopisywanie
            self.assertTrue(self.one(priv, (t, "INSERT")), t)
            self.assertFalse(self.one(priv, (t, "UPDATE")), t); self.assertFalse(self.one(priv, (t, "DELETE")), t)
        self.assertTrue(self.one(priv, ("fx_rates", "INSERT")))                       # kursy dzienne: upsert (migracja 0011), bez usuwania
        self.assertFalse(self.one(priv, ("fx_rates", "DELETE")))
        self.assertFalse(self.one(priv, ("listing_provider_symbols", "INSERT")))
        for v in ("v_live_position_values", "v_scope_result_live", "v_latest_quote", "v_fx_for_valuation"):
            self.assertTrue(self.one(priv, (v, "SELECT")), v)

    def test_latest_quote_tables_match_full_history_scan(self):
        """Migracja 0020: najnowsze notowanie/kurs z tabel wskazan = to samo, co DISTINCT ON po calej historii (takze gdy notowania przychodza nie po kolei)."""
        import random
        s = self.one("insert into securities(name,asset_class) values ('X','STOCK') returning security_id")
        l = self.one("insert into listings(security_id,display_name,trading_currency) values (%s,'X','USD') returning listing_id", (s,))
        for prov in ("YAHOO", "ALT"):
            self.cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,checked_at,evidence)
                                values (%s,%s,'X','VERIFIED','USD','MAJOR',now(),'{}')""", (l, prov))
        rnd = random.Random(7)
        stamps = [SNAP + timedelta(minutes=m) for m in range(60)]
        rnd.shuffle(stamps)                                                              # kolejnosc dopisywania inna niz kolejnosc czasu
        for i, t in enumerate(stamps):
            self.cur.execute("insert into market_quotes(listing_id,provider,provider_symbol,price,currency,price_unit,quoted_at) values (%s,%s,'X',%s,'USD','MAJOR',%s) on conflict do nothing",
                             (l, ("YAHOO", "ALT")[i % 2], 100 + i, t))
            self.cur.execute("insert into fx_quotes(base,quote,rate,provider,quoted_at) values ('USD','PLN',%s,%s,%s) on conflict do nothing", (D("3.9") + D(i) / 1000, ("YAHOO", "ALT")[i % 2], t))
        got = self.rows("select listing_id, provider, price, quoted_at from v_latest_quote where listing_id = %s", (l,))
        want = self.rows("""select distinct on (q.listing_id) q.listing_id, q.provider, q.price, q.quoted_at from market_quotes q
                            join listing_provider_symbols m on m.listing_id = q.listing_id and m.provider = q.provider and m.status = 'VERIFIED'
                            where q.listing_id = %s order by q.listing_id, q.quoted_at desc""", (l,))
        self.assertEqual(got, want); self.assertEqual(got[0]["quoted_at"], max(stamps))
        self.cur.execute("update listing_provider_symbols set status = 'REJECTED' where listing_id = %s and provider = %s", (l, got[0]["provider"]))
        other = self.rows("select provider, quoted_at from v_latest_quote where listing_id = %s", (l,))[0]           # najnowsze z pozostalego powiazania
        self.assertNotEqual(other["provider"], got[0]["provider"])
        self.assertEqual(other["quoted_at"], self.one("select max(quoted_at) from market_quotes where listing_id = %s and provider = %s", (l, other["provider"])))
        fx = self.rows("select rate, as_of from v_fx_for_valuation where base = 'USD' and quote = 'PLN'")[0]
        self.assertEqual((fx["rate"], fx["as_of"]), tuple(self.cur.execute("select rate, quoted_at from fx_quotes where base='USD' and quote='PLN' order by quoted_at desc limit 1") or self.cur.fetchone()))
        priv = "select has_table_privilege('investing_app', %s, %s)"
        for t in ("market_quote_latest", "fx_quote_latest"):
            self.assertTrue(self.one(priv, (t, "SELECT"))); self.assertFalse(self.one(priv, (t, "INSERT"))); self.assertFalse(self.one(priv, (t, "UPDATE")))

    def test_symbol_rules(self):
        y = YahooProvider(fetch=lambda u: "")
        self.assertEqual([y.symbol_for(s) for s in ("ABC.PL", "XYZ.NL", "ETF.DE", "FND.UK", "AAPL.US", "XYZ.ZZ", "NOSUFFIX")],
                         ["ABC.WA", "XYZ.AS", "ETF.DE", "FND.L", "AAPL", None, None])

    def test_split_events_are_parsed_sorted_and_bad_entries_skipped(self):
        t1, t2 = int(datetime(2024, 6, 10, 7, tzinfo=timezone.utc).timestamp()), int(datetime(2021, 3, 1, tzinfo=timezone.utc).timestamp())
        body = {"chart": {"result": [{"meta": {}, "events": {"splits": {str(t1): {"date": t1, "numerator": 10, "denominator": 1},
                                                                         str(t2): {"date": t2, "numerator": 1, "denominator": 4},
                                                                         "x": {"date": "zly"}}}}]}}
        urls = []
        y = YahooProvider(fetch=lambda u: urls.append(u) or json.dumps(body))
        from datetime import date
        self.assertEqual(y.splits("ABC.WA"), [(date(2021, 3, 1), D(1), D(4)), (date(2024, 6, 10), D(10), D(1))])
        self.assertIn("/chart/ABC.WA?", urls[0]); self.assertIn("events=split", urls[0])
        self.assertEqual(YahooProvider(fetch=lambda u: json.dumps({"chart": {"result": [{"meta": {}}]}})).splits("ABC.WA"), [])   # brak podzialow
        with self.assertRaises(ProviderError):
            YahooProvider(fetch=lambda u: "<html>").splits("ABC.WA")


if __name__ == "__main__":
    unittest.main()
