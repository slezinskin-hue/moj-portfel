"""Zakladka Gotowka i ustawienia (migracja 0017, invapp/core/cash.py): konta bankowe, wolne srodki XTB, doliczanie do wartosci tylko po wlaczeniu."""
import os, sys, unittest
from datetime import date, timedelta
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.core import cash  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class TestCash(tm.Base):
    def test_settings_default_off_and_validated(self):
        self.assertEqual(cash.get_settings(self.cur), cash.DEFAULTS)
        self.assertFalse(cash.DEFAULTS["include_cash"]); self.assertFalse(cash.DEFAULTS["include_xtb_free"])     # domyslnie liczby bez zmian
        self.assertTrue(cash.set_setting(self.cur, "include_cash", True)["include_cash"])
        self.assertFalse(cash.set_setting(self.cur, "include_cash", False)["include_cash"])
        for k, v in (("include_cash", "tak"), ("include_cash", 1), ("nieznane", True), ("price_source", "GOOGLE"), ("price_source", True)):
            with self.assertRaises(cash.CashError, msg=(k, v)):
                cash.set_setting(self.cur, k, v)
        self.cur.execute("insert into app_settings(key, value) values ('fx_source', '\"ZLY\"')")         # zla wartosc w bazie -> domyslna
        self.assertEqual(cash.get_settings(self.cur)["fx_source"], "YAHOO")

    def test_accounts_latest_balance_currency_and_archive(self):
        a = cash.add_account(self.cur, "  mBank osobiste ", "PLN", "12 345,67", "2026-10-31")
        cash.set_balance(self.cur, a, "13000", "2026-11-10")
        cash.set_balance(self.cur, a, "12000", "2026-11-05")                        # starszy dzien wpisany pozniej - obowiazuje najnowszy DZIEN
        e = cash.add_account(self.cur, "Konto EUR", "EUR", "100,5")
        self.cur.execute("insert into fx_quotes(base, quote, rate, provider, quoted_at) values ('EUR', 'PLN', 4.25, 'YAHOO', now())")
        acc = {x["nazwa"]: x for x in cash.accounts(self.cur)}
        self.assertEqual((acc["mBank osobiste"]["saldo"], acc["mBank osobiste"]["na_dzien"], acc["mBank osobiste"]["wartosc_pln"]), (D("13000.00"), date(2026, 11, 10), D("13000.00")))
        self.assertEqual(acc["Konto EUR"]["wartosc_pln"], D("427.13"))               # 100,50 x 4,25 = 427,125 -> 427,13
        self.assertEqual(acc["Konto EUR"]["na_dzien"], date.today())
        cash.archive(self.cur, a)
        self.assertEqual([x["nazwa"] for x in cash.accounts(self.cur)], ["Konto EUR"])
        with self.assertRaises(cash.CashError):
            cash.set_balance(self.cur, a, "1")                                        # konto zarchiwizowane
        self.assertEqual(self.one("select count(*) from manual_cash_balances where cash_account_id = %s", (a,)), 3)   # historia zostaje

    def test_foreign_cash_without_rate_has_no_value(self):
        cash.add_account(self.cur, "Konto CHF", "CHF", "10")
        self.assertIsNone(cash.accounts(self.cur)[0]["wartosc_pln"])                  # brak kursu = brak wartosci, nie zgadujemy

    def test_input_validation(self):
        for args in (("", "PLN", "1"), ("x" * 81, "PLN", "1"), ("Bank", "XYZ", "1"), ("Bank", "PLN", "abc"), ("Bank", "PLN", "1e20"), ("Bank", "PLN", "nan"),
                     ("Bank", "PLN", "1", (date.today() + timedelta(days=1)).isoformat()), ("Bank", "PLN", "1", "31-12-2025")):
            with self.assertRaises(cash.CashError, msg=args):
                cash.add_account(self.cur, *args)
        self.assertEqual(self.one("select count(*) from manual_cash_accounts"), 0)

    def test_xtb_free_funds_and_value_only_when_enabled(self):
        x = self.account(self.xtb, "X1", scopes=("BROKER:XTB", "INVESTMENT_ALL"))
        t = self.account(self.tr, "T1", scopes=("BROKER:TR", "INVESTMENT_ALL"))
        self.tx(x, self.xtb, "CASH_DEPOSIT", D("1000"), "2026-01-02T10:00Z")
        self.tx(x, self.xtb, "CASH_WITHDRAWAL", D("-700.25"), "2026-01-03T10:00Z")
        self.tx(t, self.tr, "CASH_DEPOSIT", D("50"), "2026-01-02T10:00Z")
        self.assertEqual([w["saldo"] for w in cash.broker_free_funds(self.cur, "INVESTMENT_ALL")], [D("299.75")])     # tylko XTB
        cash.add_account(self.cur, "Bank", "PLN", "5000")
        self.assertEqual((cash.extra_value(self.cur, "all", "INVESTMENT_ALL")["gotowka"], cash.extra_value(self.cur, "all", "INVESTMENT_ALL")["wolne_xtb"]), (0, 0))
        cash.set_setting(self.cur, "include_cash", True); cash.set_setting(self.cur, "include_xtb_free", True)
        ex = cash.extra_value(self.cur, "all", "INVESTMENT_ALL")
        self.assertEqual((ex["gotowka"], ex["wolne_xtb"]), (D("5000.00"), D("299.75")))
        ex = cash.extra_value(self.cur, "xtb", "BROKER:XTB")
        self.assertEqual((ex["gotowka"], ex["wolne_xtb"]), (0, D("299.75")))          # konta bankowe tylko w zakladce WSZYSTKIE
        ex = cash.extra_value(self.cur, "tr", "BROKER:TR")
        self.assertEqual((ex["gotowka"], ex["wolne_xtb"]), (0, 0))

    def test_app_role_reads_only(self):
        priv = "select has_table_privilege('investing_app', %s, %s)"
        for t in ("manual_cash_accounts", "manual_cash_balances", "app_settings"):
            self.assertTrue(self.one(priv, (t, "SELECT")), t)
            for p in ("INSERT", "UPDATE", "DELETE"):
                self.assertFalse(self.one(priv, (t, p)), (t, p))


class TestCashApi(unittest.TestCase):
    def test_writes_need_header(self):
        from fastapi.testclient import TestClient
        from invapp.web.app import app
        c = TestClient(app, base_url="http://127.0.0.1")
        for url in ("/api/gotowka/konto", "/api/gotowka/konto/1/saldo", "/api/gotowka/konto/1/usun", "/api/ustawienia"):
            self.assertEqual(c.post(url, json={}).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
