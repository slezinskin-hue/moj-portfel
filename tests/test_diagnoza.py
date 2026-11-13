"""Diagnoza uwag z kontroli liczb (invapp/pricing/diagnose.py): przyczyna wyznaczona z danych i rozwiazania z istniejacych mechanizmow."""
import os, sys, unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.pricing import diagnose  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule
Q_CCY = {"id": "xtb-currency-PL", "kategoria": "waluta", "broker": "xtb", "tytul": "Waluta instrumentów .PL (GPW)", "odpowiedzi": [{"wartosc": "tak", "etykieta": "Tak, w PLN"}, {"wartosc": "nie", "etykieta": "Nie"}]}
Q_SPLIT = {"id": "xtb-split-7", "kategoria": "split", "broker": "xtb", "tytul": "Podział akcji AAA.PL 10:1?", "odpowiedzi": [{"wartosc": "auto", "etykieta": "Zdecyduj za mnie"}]}
Q_DEP = {"id": "tr-deposit-3", "kategoria": "lokata", "broker": "tr", "tytul": "Zmiana oprocentowania", "odpowiedzi": [{"wartosc": "tak", "etykieta": "Tak, 5,50%"}, {"wartosc": "zostaw", "etykieta": "Zostaw"}]}


class TestDiagnose(tm.Base):
    def setUp(self):
        super().setUp()
        self.acc = self.account(self.xtb, "D1", scopes=("BROKER:XTB",))

    def listing(self, symbol, ccy):
        s = self.one("insert into securities(name,asset_class) values (%s,'STOCK') returning security_id", (symbol,))
        l = self.one("insert into listings(security_id,display_name,trading_currency) values (%s,%s,%s) returning listing_id", (s, symbol, ccy))
        self.cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id) values (%s,%s,%s)", (self.xtb, symbol, l))
        return l

    def buy_sell(self, l, bought, sold):
        t = self.tx(self.acc, self.xtb, "BUY", D(-100), "2025-01-02T10:00Z", listing=l, qty=D(bought), price=D(100) / D(bought))
        self.lot(self.acc, l, t, D(bought), D(0))
        self.tx(self.acc, self.xtb, "SELL", D(150), "2025-06-02T10:00Z", listing=l, qty=D(sold), price=D(150) / D(sold))

    def run_checks(self, checks, questions):
        res = {"ok": False, "checks": [dict(c) for c in checks]}
        return {c["kontrola"]: c for c in diagnose.annotate(self.cur, res, questions, "BROKER:XTB")["checks"]}

    def test_negative_state_with_split_hypothesis_offers_decide_for_me(self):
        self.buy_sell(self.listing("AAA.PL", "PLN"), 1, 10)
        c = self.run_checks([{"kontrola": "Brak ujemnych stanów papierów", "status": "OSTRZEZENIE"}], [Q_SPLIT, Q_CCY])["Brak ujemnych stanów papierów"]
        self.assertIn("podział akcji", c["przyczyna"]); self.assertIn("AAA.PL", c["przyczyna"])
        self.assertEqual(c["rozwiazania"][0], {"akcja": "odpowiedz", "etykieta": "Zdecyduj za mnie: split AAA.PL 10:1", "broker": "xtb", "pytanie": "xtb-split-7", "odpowiedz": "auto"})

    def test_negative_state_without_hypothesis_points_to_missing_file(self):
        self.buy_sell(self.listing("BBB.PL", "PLN"), 3, 5)
        c = self.run_checks([{"kontrola": "Brak ujemnych stanów papierów", "status": "OSTRZEZENIE"}], [Q_SPLIT])["Brak ujemnych stanów papierów"]
        self.assertIn("brakuje starszego pliku", c["przyczyna"])
        self.assertEqual([r["akcja"] for r in c["rozwiazania"]], ["import"])               # split innego instrumentu nie jest proponowany

    def test_missing_price_unknown_currency_offers_answer(self):
        l = self.listing("CCC.PL", None)
        t = self.tx(self.acc, self.xtb, "BUY", D(-100), "2025-01-02T10:00Z", listing=l, qty=D(1), price=D(100))
        self.lot(self.acc, l, t, D(1))
        c = self.run_checks([{"kontrola": "Wszystkie pozycje mają cenę rynkową z API", "status": "OSTRZEZENIE"}], [Q_CCY])["Wszystkie pozycje mają cenę rynkową z API"]
        self.assertIn("CCC.PL", c["przyczyna"]); self.assertIn("nie zgaduje", c["przyczyna"])
        self.assertEqual((c["rozwiazania"][0]["akcja"], c["rozwiazania"][0]["odpowiedz"], c["rozwiazania"][0]["pytanie"]), ("odpowiedz", "tak", "xtb-currency-PL"))

    def closed(self, l, ref, vol, bought, n):
        self.cur.execute("""insert into transactions(account_id,broker_id,txn_type,occurred_at,cash_amount,cash_currency,external_id,external_id_scheme,
                                                     dedup_key,source_kind,listing_id,quantity,price,price_currency,status,broker_position_ref)
                            values (%s,%s,'BUY','2025-01-02T10:00Z',-100,'PLN',%s,'T',%s,'BROKER_IMPORT',%s,%s,%s,'PLN','ACTIVE',%s)""",
                         (self.acc, self.xtb, f"B{n}", f"k:B:{n}", l, D(bought), D(100) / D(bought), ref))
        self.cur.execute("""insert into broker_closed_positions(account_id,listing_id,broker_position_ref,volume,open_price,close_price,opened_at,closed_at,
                                                                purchase_value,sale_value,profit_loss,close_origin,dedup_key,row_hash)
                            values (%s,%s,%s,%s,1,1,'2025-01-02T10:00Z','2026-02-13T14:00Z',100,150,50,'Android',%s,repeat('a',64))""", (self.acc, l, ref, vol, f"k{n}"))

    def test_split_evidence_from_positions_and_rejected_split_is_offered_again(self):
        from invapp import brokers
        x = brokers.by_code("XTB")
        l = self.listing("EEE.PL", "PLN")
        self.closed(l, "P1", 10, 1, 1); self.closed(l, "P2", 20, 2, 2)
        ev = x.split_evidence(self.cur, l)
        self.assertEqual((ev["k"], len(ev["positions"])), (10, 2))
        fix = x.negative_state_fix(self.cur, l)
        self.assertEqual((fix["pytanie"], fix["odpowiedz"], fix["odrzucony"]), (f"xtb-resolve-split-{l}", "auto", False))
        self.cur.execute("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,applies_to_lots_opened_before,ratio_new,ratio_old,source,status,evidence)
                            values (%s,'SPLIT',NULL,'UNKNOWN','2026-02-13T14:00Z',10,1,'INFERRED','REJECTED','{"r":1}')""", (l,))
        self.assertTrue(x.negative_state_fix(self.cur, l)["odrzucony"])               # wczesniej "to nie byl split" - nadal do rozwiazania
        self.cur.execute("update corporate_actions set status = 'CONFIRMED' where listing_id = %s", (l,))
        self.assertIsNone(x.negative_state_fix(self.cur, l))                             # juz przyjety
        m = self.listing("FFF.PL", "PLN")
        self.closed(m, "P3", 10, 1, 3); self.closed(m, "P4", 15, 3, 4)                   # ilorazy 10 i 5: niespojne - to nie jest jednoznaczny split
        self.assertIsNone(x.split_evidence(self.cur, m))
        self.assertIsNone(x.negative_state_fix(self.cur, m))

    def test_other_checks_and_ok_untouched(self):
        out = self.run_checks([{"kontrola": "Zysk całkowity = zysk + dochody", "status": "BLAD"}, {"kontrola": "Lokata „TR”: model odtwarza wypłaty", "status": "OSTRZEZENIE"},
                               {"kontrola": "Inna", "status": "OK"}], [Q_DEP])
        self.assertEqual(out["Zysk całkowity = zysk + dochody"]["rozwiazania"][0]["akcja"], "przelicz")
        self.assertEqual(out["Lokata „TR”: model odtwarza wypłaty"]["rozwiazania"][0]["odpowiedz"], "tak")
        self.assertNotIn("przyczyna", out["Inna"])
        res = {"ok": True, "checks": [{"kontrola": "x", "status": "OK"}]}
        self.assertEqual(diagnose.annotate(self.cur, res, [], "BROKER:XTB"), res)


class TestFixApi(unittest.TestCase):
    def test_fix_endpoints_need_header(self):
        from fastapi.testclient import TestClient
        from invapp.web.app import app
        c = TestClient(app, base_url="http://127.0.0.1")
        for url in ("/api/ceny/odswiez", "/api/przelicz", "/api/konto/1/usun", "/api/usun-wszystko"):
            self.assertEqual(c.post(url).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
