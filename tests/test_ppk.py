"""Zakladka PPK (invapp/core/ppk.py, baza z migracji 0001 sekcja 9a): rachunek, stany wpisywane recznie, liczby, wklejony tekst, ustawienie
wliczania do wartosci, niezaleznosc od wyniku portfela inwestycyjnego, kontrola liczb. Kwoty syntetyczne (nie dane uzytkownika)."""
import os, sys, unittest
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import baza_testowa as tm  # noqa: E402
from invapp.core import cash, ppk  # noqa: E402
from invapp.pricing import audit  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule

TODAY = date.today()
D1 = (TODAY - timedelta(days=40)).isoformat()
D2 = (TODAY - timedelta(days=10)).isoformat()
JOINED = "2023-05-10"
LINES = {"EMPLOYEE_BASIC": {"wplaty": "1 000,00", "wartosc": "1 200,00"}, "EMPLOYER_BASIC": {"wplaty": "750", "wartosc": "900,00"},
         "WELCOME": {"wplaty": "250,00", "wartosc": "300,00"}, "STATE_ANNUAL": {"wplaty": "240,00", "wartosc": "280,00"},
         "EMPLOYEE_ADDITIONAL": {"wplaty": "0,00", "wartosc": "0,00"}}
TOTAL = "2 680,00"


class PpkBase(tm.Base):
    def acc(self, name="PPK", inst="Przykładowe TFI"):
        return ppk.add_account(self.cur, name, inst, "Fundusz Przykład 2060", JOINED, "2060")

    def state(self, aid, day=D2, total=TOTAL, lines=None):
        return ppk.add_snapshot(self.cur, aid, day, total, LINES if lines is None else lines)

    def shown(self, aid):
        return next(a for a in ppk.overview(self.cur) if a["id"] == aid)


class TestAccount(PpkBase):
    def test_account_only_in_ppk_and_net_worth_scopes(self):
        a = self.acc()
        scopes = {r["code"] for r in self.rows("select s.code from scope_accounts sa join scopes s using (scope_id) where sa.account_id = %s", (a,))}
        self.assertEqual(scopes, {"PPK", "NET_WORTH"})                                    # nie INVESTMENT_ALL - wynik portfela bez zmian
        r = self.rows("select product_type, account_class, tracking_mode, tax_regime, external_no_source from accounts where account_id = %s", (a,))[0]
        self.assertEqual(tuple(r.values()), ("PPK", "RETIREMENT", "BALANCE_ONLY", "PPK", "USER"))
        self.assertEqual(self.one("select target_year from retirement_funds f join retirement_accounts ra using (fund_id) where ra.account_id = %s", (a,)), 2060)

    def test_institution_reused_and_second_account(self):
        a, b = self.acc("PPK 1", "Przykładowe TFI"), self.acc("PPK 2", "przykładowe tfi")
        self.assertEqual(self.one("select count(distinct broker_id) from accounts where account_id in (%s, %s)", (a, b)), 1)
        self.assertEqual(self.one("select count(*) from financial_institutions where institution_kind = 'TFI'"), 1)
        self.assertEqual(sorted(self.one("select array_agg(external_no) from accounts where account_id in (%s, %s)", (a, b))), ["PPK-1", "PPK-2"])
        self.assertEqual(ppk.add_account(self.cur, "", "Inne TFI", "F", JOINED) and self.one("select display_name from accounts order by account_id desc limit 1"), "PPK")

    def test_account_validation(self):
        future = (TODAY + timedelta(days=1)).isoformat()
        for args in (("PPK", "", "F", JOINED), ("PPK", "TFI", "", JOINED), ("PPK", "TFI", "F", ""), ("PPK", "TFI", "F", future), ("PPK", "TFI", "F", "10.05.2023"),
                     ("x" * 81, "TFI", "F", JOINED), ("PPK", "TFI", "F", JOINED, "55"), ("PPK", "TFI", "F", JOINED, "rok")):
            with self.assertRaises(ppk.PpkError, msg=args):
                ppk.add_account(self.cur, *args)
        self.assertEqual(self.one("select count(*) from retirement_accounts"), 0)


class TestNumbers(PpkBase):
    def test_current_state_numbers(self):
        a = self.acc(); r = self.state(a)
        self.assertEqual((r["wynik"], r["roznica"]), ("CREATED", D("0.00")))
        st = self.shown(a)["stan"]
        self.assertEqual(st["wartosc"], D("2680.00"))
        self.assertEqual(st["wplaty"], D("2240.00"))                                      # 1000 + 750 + 250 + 240 + 0
        self.assertEqual(st["zmiana"], D("440.00"))                                       # 2680 - 2240
        self.assertEqual(st["zmiana_pct"].quantize(D("0.000001")), D("0.196429"))         # 440 / 2240
        self.assertEqual((st["wplaty_pracownik"], st["wplaty_pracodawca"], st["wplaty_panstwo"]), (D("1000.00"), D("750.00"), D("490.00")))
        self.assertEqual((st["wplaty_wlasne"], st["ponad_wlasne"], st["ponad_wlasne_pct"]), (D("1000.00"), D("1680.00"), D("1.68")))
        self.assertEqual((st["kontrola"], st["nieaktualna"], st["dni_od_wyceny"]), ("EXACT", False, 10))
        cat = {c["kod"]: c for c in self.shown(a)["kategorie"]}
        self.assertEqual((cat["EMPLOYER_BASIC"]["zmiana"], cat["EMPLOYER_BASIC"]["zmiana_pct"]), (D("150.00"), D("0.2")))
        self.assertEqual((cat["EMPLOYEE_ADDITIONAL"]["wplaty"], cat["EMPLOYEE_ADDITIONAL"]["zmiana_pct"]), (D("0.00"), None))   # zero, a nie brak; % od zera - brak
        self.assertNotIn("EMPLOYER_ADDITIONAL", cat)                                      # nie wpisana = brak danych
        self.assertEqual(ppk.total_value(self.cur), D("2680.00"))

    def test_empty_field_is_missing_not_zero(self):
        a = self.acc()
        lines = dict(LINES, STATE_ANNUAL={"wplaty": "", "wartosc": "280,00"})
        self.state(a, lines=lines)
        st = self.shown(a)["stan"]
        self.assertEqual((st["wplaty"], st["zmiana"], st["wplaty_panstwo"]), (None, None, None))       # nieznane wplaty -> brak sumy, nie suma czesciowa
        self.assertEqual(st["ponad_wlasne"], D("1680.00"))                                # wplaty pracownika znane
        self.assertTrue(any("Nie wszystkie kategorie" in w for w in self.shown(a)["ostrzezenia"]))
        b = self.acc("Bez skladek")
        self.state(b, lines={})
        st = self.shown(b)["stan"]
        self.assertEqual((st["wartosc"], st["wplaty"], st["wplaty_wlasne"], st["kontrola"]), (D("2680.00"), None, None, "INCOMPLETE"))

    def test_mismatch_is_saved_and_flagged(self):
        a = self.acc(); r = self.state(a, total="2 690,00")
        self.assertEqual(r["roznica"], D("10.00"))
        self.assertEqual(self.shown(a)["stan"]["kontrola"], "MISMATCH")

    def test_duplicate_correction_and_history(self):
        a = self.acc()
        self.state(a, D1, "2 500,00", dict(LINES, EMPLOYEE_BASIC={"wplaty": "900", "wartosc": "1 020,00"}))
        self.assertEqual(self.state(a)["wynik"], "CREATED")
        self.assertEqual(self.state(a)["wynik"], "DUPLICATE")                             # te same liczby jeszcze raz
        self.assertEqual(self.state(a, total="2 681,00", lines=dict(LINES, WELCOME={"wplaty": "250", "wartosc": "301"}))["wynik"], "CORRECTION")
        acc = self.shown(a)
        self.assertEqual([(h["data_wyceny"].isoformat(), h["wartosc"], h["poprawka"]) for h in acc["historia"]],
                         [(D1, D("2500.00"), False), (D2, D("2681.00"), True)])
        self.assertEqual(acc["stan"]["wartosc"], D("2681.00"))
        self.assertEqual(self.one("select count(*) from ppk_snapshots where account_id = %s", (a,)), 3)       # poprzednia wersja zostaje
        ppk.add_snapshot(self.cur, a, (TODAY - timedelta(days=60)).isoformat(), "2 000,00", {})                # starszy stan wpisany pozniej
        self.assertEqual(self.shown(a)["stan"]["wartosc"], D("2681.00"))                  # obowiazuje najnowsza DATA wyceny

    def test_snapshot_validation(self):
        a = self.acc()
        bad = [(D2, "", LINES), ((TODAY + timedelta(days=1)).isoformat(), TOTAL, LINES), ("2023-05-09", TOTAL, LINES), ("17.11.2026", TOTAL, LINES),
               (D2, "1.234", LINES), (D2, "-5", LINES), (D2, "abc", LINES), (D2, TOTAL, {"NIEZNANA": {"wplaty": "1"}}),
               (D2, TOTAL, dict(LINES, WELCOME={"wplaty": "1,234.5.6", "wartosc": "1"})), (D2, TOTAL, dict(LINES, WELCOME={"wplaty": "-1", "wartosc": "1"})),
               (D2, TOTAL, dict(LINES, WELCOME="250"))]
        for d, t, l in bad:
            with self.assertRaises(ppk.PpkError, msg=(d, t, l)):
                ppk.add_snapshot(self.cur, a, d, t, l)
        with self.assertRaises(ppk.PpkError):
            ppk.add_snapshot(self.cur, 999999, D2, TOTAL, LINES)
        self.assertEqual(self.one("select count(*) from ppk_snapshots"), 0)

    def test_archive_hides_and_excludes(self):
        a, b = self.acc("A"), self.acc("B")
        self.state(a); self.state(b, total="100,00", lines={})
        self.assertEqual(ppk.total_value(self.cur), D("2780.00"))
        ppk.archive(self.cur, b)
        self.assertEqual([x["id"] for x in ppk.overview(self.cur)], [a])
        self.assertEqual(ppk.total_value(self.cur), D("2680.00"))
        with self.assertRaises(ppk.PpkError):
            self.state(b)
        self.assertEqual(self.one("select count(*) from ppk_snapshots where account_id = %s", (b,)), 1)       # historia zostaje


class TestPortfolioUnchanged(PpkBase):
    def test_investment_numbers_do_not_change_and_value_only_when_enabled(self):
        x = self.account(self.xtb, "X1", scopes=("BROKER:XTB", "INVESTMENT_ALL", "NET_WORTH"))
        self.tx(x, self.xtb, "CASH_DEPOSIT", D("1000"), "2026-01-02T10:00Z")
        before = self.rows("select * from v_scope_summary where scope_code in ('INVESTMENT_ALL', 'BROKER:XTB') order by scope_code")
        a = self.acc(); self.state(a)
        self.assertEqual(self.rows("select * from v_scope_summary where scope_code in ('INVESTMENT_ALL', 'BROKER:XTB') order by scope_code"), before)
        self.assertEqual(cash.extra_value(self.cur, "all", "INVESTMENT_ALL")["ppk"], 0)   # domyslnie wylaczone
        self.assertFalse(cash.DEFAULTS["include_ppk"])
        cash.set_setting(self.cur, "include_ppk", True)
        self.assertEqual(cash.extra_value(self.cur, "all", "INVESTMENT_ALL")["ppk"], D("2680.00"))
        self.assertEqual(cash.extra_value(self.cur, "xtb", "BROKER:XTB")["ppk"], 0)       # tylko portfel WSZYSTKIE


class TestAudit(PpkBase):
    def run_checks(self):
        out = []
        audit._ppk_checks(self.cur, lambda name, ok, detail="", level="BLAD": out.append((name, "OK" if ok else level, detail)))
        return out

    def test_consistent_state_passes(self):
        a = self.acc(); self.state(a)
        res = self.run_checks()
        self.assertEqual(len(res), 3)
        self.assertTrue(all(s == "OK" for _, s, _ in res), res)

    def test_mismatch_and_stale_are_flagged(self):
        a = self.acc(); self.state(a, (TODAY - timedelta(days=50)).isoformat(), "2 690,00")
        res = {n.split(":", 1)[1].strip()[:12]: s for n, s, _ in self.run_checks()}
        self.assertEqual(sorted(res.values()), ["BLAD", "OK", "OSTRZEZENIE"])
        b = self.acc("B"); self.state(b, total="2 680,01")
        self.assertTrue(all(s == "OK" for n, s, _ in self.run_checks() if "„B”" in n))     # roznica zaokraglen (do 1 gr na wiersz) - zgodna

    def test_wrong_numbers_on_page_are_detected(self):
        a = self.acc(); self.state(a)
        real = ppk.overview

        def wrong(cur):
            out = real(cur)
            out[0]["stan"]["ponad_wlasne"] += D("1")
            return out
        with mock.patch.object(ppk, "overview", wrong):
            res = self.run_checks()
        self.assertIn("BLAD", [s for _, s, _ in res])


class TestModel(PpkBase):
    """PPK w portfelu (include_ppk): odtworzone dni wplat i wartosc miedzy stanami (core/ppk.model)."""
    J = date(2023, 5, 10)

    def two_states(self):
        a = self.acc()
        first = {"EMPLOYEE_BASIC": {"wplaty": "300,00", "wartosc": "310,00"}, "EMPLOYER_BASIC": {"wplaty": "225,00", "wartosc": "232,00"},
                 "WELCOME": {"wplaty": "250,00", "wartosc": "255,00"}}
        ppk.add_snapshot(self.cur, a, "2023-12-31", "797,00", first)                     # 7 skladek (15.06-15.12) + powitalna
        self.state(a)                                                                     # LINES na dzien D2
        return a

    def test_welcome_date_after_three_full_months_and_quarter(self):
        self.assertEqual(ppk.welcome_date(date(2023, 5, 10)), date(2023, 10, 30))        # pelne VI-VIII -> koniec III kw. + 30 dni
        self.assertEqual(ppk.welcome_date(date(2023, 7, 1)), date(2023, 10, 30))         # 1. dnia miesiaca: lipiec jest pelny
        self.assertEqual(ppk.welcome_date(date(2023, 11, 20)), date(2024, 4, 30))        # XII-II -> koniec I kw. 2024 + 30 dni

    def test_flows_follow_ppk_rules_and_sum_to_each_state(self):
        a = self.two_states()
        m = ppk.account_model(self.cur, a, self.J, TODAY)
        fl = dict(m["flows"])
        self.assertEqual(min(fl), date(2023, 6, 15))                                      # pierwsza skladka: 15. dnia miesiaca po przystapieniu
        self.assertEqual(fl[date(2023, 6, 15)], D("74.99"))                               # 7 skladek (15.06-15.12): 300/7 -> 42,85 + 225/7 -> 32,14 (reszta w ostatniej)
        self.assertEqual(fl[date(2023, 10, 30)], D("250.00"))                             # powitalna wg reguly
        self.assertEqual(sum((f for d, f in m["flows"] if d <= date(2023, 12, 31)), D(0)), D("775.00"))
        self.assertEqual(sum((f for _, f in m["flows"]), D(0)), D("2240.00"))            # = laczne wplaty stanu co do grosza
        april = [d for d in fl if d.month == 4 and d.day == 15]
        self.assertIn(date(TODAY.year if date(TODAY.year, 4, 15) <= TODAY - timedelta(days=10) else TODAY.year - 1, 4, 15), april)   # doplata 240 zl
        self.assertTrue(all(d.day == 15 or d == date(2023, 10, 30) for d in fl))

    def test_split_keeps_every_grosz(self):
        self.assertEqual(ppk._split(D("100.00"), 3), [D("33.33"), D("33.33"), D("33.34")])
        self.assertEqual(ppk._flows_between("STATE_ANNUAL", D("480.00"), date(2023, 5, 10), date(2026, 11, 1), date(2023, 5, 10)),
                         [(date(2025, 4, 15), D("240.00")), (date(2026, 4, 15), D("240.00"))])
        self.assertEqual(ppk._flows_between("EMPLOYEE_BASIC", D("-10.00"), date(2024, 1, 1), date(2024, 6, 1), date(2023, 5, 10)), [(date(2024, 6, 1), D("-10.00"))])

    def test_value_hits_every_state_and_grows_between(self):
        a = self.two_states()
        m = ppk.account_model(self.cur, a, self.J, TODAY)
        self.assertEqual((m["daily"][date(2023, 12, 31)], m["daily"][date.fromisoformat(D2)]), (D("797.00"), D("2680.00")))
        self.assertEqual(m["daily"][date(2023, 6, 14)], D("0.00"))
        self.assertEqual(m["daily"][TODAY], D("2680.00"))                                 # po ostatnim stanie bez zmian
        before = m["daily"][date.fromisoformat(D2) - timedelta(days=1)]
        self.assertTrue(D("2650") < before < D("2680"), before)                           # ciaglosc: dzien przed stanem blisko stanu
        self.assertTrue(all(r is not None and r > 0 for r in m["annual_rates"]))
        days = sorted(m["daily"])
        self.assertTrue(all(m["daily"][x] <= m["daily"][y] for x, y in zip(days, days[1:])))   # bez wyplat: wartosc nie maleje

    def test_portfolio_value_gain_and_no_double_count(self):
        from invapp.web import dashboard
        a = self.acc(); self.state(a)
        b = self.acc("Bez wplat"); self.state(b, total="100,00", lines={})               # stan bez wplat - tylko wartosc
        cash.set_setting(self.cur, "include_ppk", True)
        scope = {"assets_value": D("5000.00"), "total_result_live": D("700.00")}
        dashboard.add_cash(scope, tm._conn, "all", "INVESTMENT_ALL")
        self.assertEqual((scope["assets_value"], scope["total_result_live"]), (D("7680.00"), D("1140.00")))   # + 2680, + (2680 - 2240)
        self.assertEqual((scope["ppk_gain"], scope["ppk_paid"], scope["ppk_value"]), (D("440.00"), D("2240.00"), D("2780.00")))
        self.assertEqual(scope["total_value"], D("7780.00"))                              # rachunek bez wplat: tylko w wartosci, raz
        self.assertEqual(scope["ppk_unmodeled"], ["Bez wplat"])

    def test_history_series_includes_ppk_only_when_enabled(self):
        from invapp.pricing import metrics
        from invapp.web import dashboard
        a = self.two_states()
        day = (TODAY - timedelta(days=3)).isoformat()
        base = lambda: {"series": [{"date": day, "value": D(100), "securities": D(100), "deposits": D(0), "invested_gross": D(90), "sold": D(0),  # noqa: E731
                                    "invested_net": D(90), "gain": D(10), "positions": 1, "estimated_positions": 0}], "first_date": day}
        s = base(); dashboard._merge_ppk(self.cur, s)
        self.assertEqual(len(s["series"]), 1)                                             # wylaczone - bez zmian
        cash.set_setting(self.cur, "include_ppk", True)
        s = base(); dashboard._merge_ppk(self.cur, s)
        first, last = s["series"][0], s["series"][-1]
        self.assertEqual((first["date"], first["value"], s["first_date"]), (self.J.isoformat(), D(0), self.J.isoformat()))
        self.assertEqual((last["value"], last["invested_net"], last["gain"], last["ppk"]), (D("2780.00"), D("2330.00"), D("450.00"), D("2680.00")))
        jan = next(p for p in s["series"] if p["date"] == "2023-12-31")
        self.assertEqual((jan["value"], jan["invested_gross"]), (D("797.00"), D("775.00")))
        m = metrics.compute(s["series"])
        self.assertAlmostEqual(m["gain"], 450.0, places=6)

    def test_currency_ppk_paid_at_rate_of_its_day(self):
        from invapp.pricing import currency
        from invapp.web import dashboard
        a = self.acc(); self.state(a); cash.set_setting(self.cur, "include_ppk", True)
        d = self.J
        while d <= TODAY:
            self.cur.execute("insert into fx_rates(rate_date,base,quote,rate,source) values (%s,'USD','PLN',%s,'YAHOO_1D')", (d, D("4.00") if d < date(2025, 1, 1) else D("5.00")))
            d += timedelta(days=1)
        self.cur.execute("insert into fx_quotes(base,quote,rate,provider,quoted_at) values ('USD','PLN',5.00,'YAHOO',now())")
        scope = {"securities_value": D(0), "deposits_value": D(0), "assets_value": D(0), "total_value": D(0), "cash_value": 0, "xtb_free_value": 0,
                 "deposit_interest_unbooked": 0, "income_12m": {}, "total_result_live": D(0)}
        dashboard.add_cash(scope, tm._conn, "all", "INVESTMENT_ALL")
        w = currency.convert_live(self.cur, {"scope": scope, "positions": [], "deposits": []}, "USD", [])
        flows = ppk.model(self.cur)["flows"]
        paid = sum((f / (D("4.00") if dd < date(2025, 1, 1) else D("5.00")) for dd, f in flows), D(0))
        self.assertEqual(w["scope"]["ppk_paid"], paid)
        self.assertEqual(w["scope"]["ppk_gain"], D("2680.00") / D("5.00") - paid)
        self.assertEqual(w["scope"]["total_result_live"], w["scope"]["ppk_gain"])

    def test_audit_model_check(self):
        a = self.two_states(); cash.set_setting(self.cur, "include_ppk", True)
        out = []
        add = lambda name, ok, detail="", level="BLAD": out.append((name, "OK" if ok else level))  # noqa: E731
        audit._ppk_checks(self.cur, add)
        self.assertIn(("PPK „PPK” w portfelu: odtworzone wpłaty i wartość = każdy wpisany stan", "OK"), out)
        real = ppk.account_model

        def wrong(*args):
            m = real(*args); m["flows"] = m["flows"][1:]; return m
        out.clear()
        with mock.patch.object(ppk, "account_model", wrong):
            audit._ppk_checks(self.cur, add)
        self.assertIn(("PPK „PPK” w portfelu: odtworzone wpłaty i wartość = każdy wpisany stan", "BLAD"), out)


from test_history import HistBase, span  # noqa: E402


class TestChartParts(HistBase):
    """Wykres "Portfel w czasie" (WSZYSTKIE): skladniki do wlaczania - kazdy broker i PPK; suma skladnikow = wartosc calego portfela."""
    def test_parts_sum_to_total_value_every_day(self):
        from invapp.pricing import history
        from invapp.web import dashboard
        self.cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = 'INVESTMENT_ALL'", (self.acct,))
        d0 = TODAY - timedelta(days=40)
        a = self.instrument("AAA.PL", "PLN"); self.buy(a, d0, 10, 1000)
        xtb, self.acct = self.acct, self.account(self.tr, "T9", scopes=("BROKER:TR", "INVESTMENT_ALL"))
        b = self.instrument("BBB.PL", "PLN"); self.buy(b, d0 + timedelta(days=5), 5, 500)
        self.acct = xtb
        self.store_prices(a, "PLN", span(d0, 41, lambda i: D(100) + i))
        self.store_prices(b, "PLN", span(d0, 41, lambda i: D(100) - i))
        acc = ppk.add_account(self.cur, "PPK", "Przykładowe TFI", "Fundusz", JOINED)
        ppk.add_snapshot(self.cur, acc, D2, TOTAL, LINES); cash.set_setting(self.cur, "include_ppk", True)
        s = history.build_series(tm._conn, "INVESTMENT_ALL")
        dashboard._merge_ppk(self.cur, s); dashboard._add_parts(tm._conn, s)
        self.assertEqual([p["label"] for p in s["skladniki"]], ["XTB", "Trade Republic", "PPK"])
        keys = [p["key"] for p in s["skladniki"]]
        for p in s["series"]:
            self.assertEqual(sum((p[k] for k in keys), D(0)), p["value"], p["date"])
        last = s["series"][-1]
        self.assertEqual((last["c_XTB"], last["c_TR"], last["c_PPK"]), (D(10) * D(140), D(5) * D(60), D("2680.00")))
        s = history.build_series(tm._conn, "BROKER:XTB")                                  # portfel jednego brokera: bez skladnikow
        self.assertNotIn("skladniki", s)

    def test_parts_converted_to_display_currency(self):
        from invapp.pricing import currency
        d = TODAY - timedelta(days=1)
        self.store_fx("USD", {d: D("4.00")})
        p = {"date": d.isoformat(), "value": D(1000), "securities": D(600), "deposits": D(0), "invested_gross": D(800), "sold": D(0), "invested_net": D(800),
             "gain": D(200), "ppk": D(400), "c_XTB": D(600), "c_PPK": D(400)}
        q = currency.convert_series([p], currency.Rates(self.cur, "USD"))["series"][0]
        self.assertEqual((q["value"], q["c_XTB"], q["c_PPK"], q["ppk"]), (D(250), D(150), D(100), D(100)))


class TestParseText(PpkBase):
    def test_summary_text_with_spaces(self):
        text = """Podsumowanie PPK
Pracodawca: PRZYKŁADOWA FIRMA SP. Z O.O.
Instytucja finansowa: Przykładowe TFI
Wartość całkowita: 2 680,00 PLN
Data otwarcia: 10.05.2023
Data wyceny: 01.11.2026
Numer rejestru: 00-12345678-000-01
Rodzaj składki Wpłaty i wypłaty Wartość
Składka podstawowa pracownika 1 000,00 PLN 1 200,00 PLN
Składka podstawowa pracodawcy 750,00 PLN 900,00 PLN
Składka dodatkowa pracodawcy 0,00 PLN 0,00 PLN
Dopłata roczna 240,00 PLN 280,00 PLN
Składka powitalna 250,00 PLN 300,00 PLN"""
        d = ppk.parse_text(self.cur, text)
        self.assertEqual((d["wartosc_calkowita"], d["data_wyceny"], d["data_przystapienia"], d["instytucja"]), ("2680.00", "2026-11-01", "2023-05-10", "Przykładowe TFI"))
        self.assertEqual(d["linie"]["EMPLOYEE_BASIC"], {"wplaty": "1000.00", "wartosc": "1200.00"})
        self.assertEqual(d["linie"]["EMPLOYER_ADDITIONAL"], {"wplaty": "0.00", "wartosc": "0.00"})
        self.assertEqual(set(d["linie"]), {"EMPLOYEE_BASIC", "EMPLOYER_BASIC", "EMPLOYER_ADDITIONAL", "STATE_ANNUAL", "WELCOME"})
        self.assertEqual(d["uwagi"], [])
        self.assertNotIn("12345678", repr(d)); self.assertNotIn("FIRMA", repr(d))      # numer rejestru i pracodawca nie sa przenoszone

    def test_table_copied_with_tabs_and_value_below_label(self):
        text = "Wartość całkowita\n2 680,00 zł\nPrzykładowe TFI · wycena 01.11.2026\nSkładka podstawowa pracownika\t1 000,00 zł\t1 200,00 zł\nWpłata powitalna\t250,00\t300,00\n"
        d = ppk.parse_text(self.cur, text)
        self.assertEqual((d["wartosc_calkowita"], d["data_wyceny"]), ("2680.00", "2026-11-01"))
        self.assertEqual(d["linie"], {"EMPLOYEE_BASIC": {"wplaty": "1000.00", "wartosc": "1200.00"}, "WELCOME": {"wplaty": "250.00", "wartosc": "300.00"}})
        self.assertTrue(any("różni się" in u for u in d["uwagi"]))                        # 1500 != 2680 - brakuje wierszy

    def test_no_guessing(self):
        d = ppk.parse_text(self.cur, "Składka powitalna 250,00\nDopłata roczna 1.234 1.234\nCoś innego 5,00 6,00")
        self.assertEqual(d["linie"], {})
        self.assertEqual(len([u for u in d["uwagi"] if "wpisz wpłaty i wartość ręcznie" in u]), 2)   # jedna kwota; niejednoznaczne "1.234" odrzucone
        self.assertTrue(any("Nie znalazłem wierszy" in u for u in ppk.parse_text(self.cur, "bez danych")["uwagi"]))

    def test_parse_does_not_write(self):
        n = self.one("select count(*) from ppk_import_drafts") + self.one("select count(*) from ppk_snapshots")
        ppk.parse_text(self.cur, "Składka powitalna 250,00 300,00")
        self.assertEqual(self.one("select count(*) from ppk_import_drafts") + self.one("select count(*) from ppk_snapshots"), n)


class TestPrivileges(PpkBase):
    def test_app_role_reads_only(self):
        priv = "select has_table_privilege('investing_app', %s, %s)"
        for t in ("ppk_snapshots", "ppk_snapshot_lines", "retirement_accounts", "retirement_funds"):
            self.assertTrue(self.one(priv, (t, "SELECT")), t)
            for p in ("INSERT", "UPDATE", "DELETE"):
                self.assertFalse(self.one(priv, (t, p)), (t, p))


class TestPpkApi(unittest.TestCase):
    def test_writes_need_header(self):
        from fastapi.testclient import TestClient
        from invapp.web.app import app
        c = TestClient(app, base_url="http://127.0.0.1")
        for url in ("/api/ppk/konto", "/api/ppk/konto/1/stan", "/api/ppk/konto/1/usun", "/api/ppk/odczytaj"):
            self.assertEqual(c.post(url, json={}).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
