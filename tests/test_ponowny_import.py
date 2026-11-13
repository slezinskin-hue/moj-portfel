"""
Czysta instalacja i kolejne importy (dane SYNTETYCZNE - bez danych zadnego uzytkownika):
* pusta baza -> import eksportu Trade Republic zaklada konto i zapamietuje operacje;
* ponowny import tego samego pliku nie dodaje niczego (same duplikaty);
* nowszy eksport (stare operacje + nowe) dodaje TYLKO nowe operacje, a wartosci odpowiadaja sumie obu okresow;
* kolejnosc importu plikow nie ma znaczenia (wczesniejszy eksport wczytany po nowszym nie dubluje operacji).
"""
import csv, os, shutil, sys, tempfile, unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)
import baza_testowa as tm  # noqa: E402
from invapp.brokers.trade_republic import parser as tr  # noqa: E402
from invapp.web import importing  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


def row(n, when, category, typ, amount, **kw):
    r = {"datetime": when, "date": when[:10], "account_type": "DEFAULT", "category": category, "type": typ, "amount": amount, "currency": "PLN",
         "description": typ, "transaction_id": f"00000000-0000-4000-8000-{n:012d}"}
    r.update(kw)
    return r


def trade(n, when, typ, isin, shares, price, amount, fee="-1.00"):
    return row(n, when, "TRADING", typ, amount, asset_class="STOCK", name="Testowa SA", symbol=isin, shares=shares, price=price, fee=fee,
               description=f"{typ} trade {isin}")


JAN = [row(1, "2026-01-02T10:00:00.000Z", "CASH", "CUSTOMER_INPAYMENT", "1000.00"),
       trade(2, "2026-01-05T10:00:00.000Z", "BUY", "PL0000000001", "10", "50.00", "-500.00"),
       row(3, "2026-02-01T00:00:00.000Z", "CASH", "INTEREST_PAYMENT", "2.50", tax="-0.48")]
FEB = [trade(4, "2026-02-10T10:00:00.000Z", "BUY", "PL0000000001", "5", "60.00", "-300.00"),
       row(5, "2026-02-20T10:00:00.000Z", "CASH", "CUSTOMER_INPAYMENT", "200.00")]


class TestRepeatedImports(tm.Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="invapp_ponowny_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def csv(self, name, rows):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            w.writerow(tr.COLUMNS)
            for r in rows:
                w.writerow([r.get(c, "") for c in tr.COLUMNS])
        return path

    def imp(self, path):
        return importing.import_path(tm._conn, path, os.path.basename(path))["counters"]

    def ledger(self):
        return self.rows("""select t.txn_type, t.cash_amount, t.quantity from transactions t join financial_institutions f using (broker_id)
                            where f.code = 'TR' order by t.occurred_at, t.txn_type""")

    def test_clean_install_then_same_file_then_newer_export(self):
        self.assertEqual(self.one("select count(*) from accounts"), 0)                    # czysta instalacja: brak kont i operacji
        c = self.imp(self.csv("Eksport transakcji.csv", JAN))
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (4, 0))                         # 3 wiersze + wydzielony podatek od odsetek
        self.assertEqual(self.one("select count(*) from accounts"), 1)
        first = self.ledger()
        c = self.imp(self.csv("Eksport transakcji.csv", JAN))                            # ten sam plik jeszcze raz
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (0, 4))                         # 3 wiersze + wiersz podatku - wszystkie juz sa
        self.assertEqual(self.ledger(), first)
        c = self.imp(self.csv("Eksport transakcji (2).csv", JAN + FEB))                  # nowszy eksport: stare + nowe operacje
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (2, 4))
        last = self.one("select max(batch_id) from import_batches")
        self.assertEqual(self.one("select count(*) from transactions where last_seen_batch_id = %s", (last,)), 6)   # widziane w najnowszym pliku (takze duplikaty)
        self.assertEqual(self.one("select count(*) from transactions"), 6)
        self.assertEqual(self.one("select count(*) from accounts"), 1)                   # to samo konto, bez duplikatu
        self.assertEqual(self.one("select sum(qty_remaining) from derived_lots"), D("15"))  # 10 + 5 akcji
        self.assertEqual(self.one("select coalesce(sum(cash_amount), 0) from transactions"), D("1000.00") - 501 + D("2.50") - D("0.48") - 301 + 200)      # zakupy z oplata 1 zl

    def test_reward_is_profit_not_deposit_in_series(self):
        """Darmowa akcja na start (STOCKPERK): wplacone 400 zl wlasnych pieniedzy, akcja za 150,00 zl za darmo -> zysk, nie wplata."""
        from datetime import date
        from invapp.pricing import history
        rows = [row(11, "2026-04-06T10:00:00.000Z", "CASH", "CUSTOMER_INPAYMENT", "400.00"),
                row(12, "2026-04-07T14:15:16.000Z", "TRADING", "STOCKPERK", "150.00", asset_class="STOCK", name="Premia SA", symbol="US0000000002"),
                trade(13, "2026-04-07T14:15:17.000Z", "BUY", "US0000000002", "0.12", "1250.00", "-150.00", fee="")]
        self.imp(self.csv("premia.csv", rows))
        acc = self.one("select account_id from accounts")
        end = date(2026, 4, 10)
        s = history.build_series(tm._conn, "BROKER:TR", end=end)["series"][-1]
        self.assertEqual(s["invested_net"], D("0.00"))                   # konto bez lokaty: wplata = zakup za WLASNE pieniadze; ten byl z premii
        self.assertEqual(s["gain"], s["value"])                          # cala wartosc akcji to zysk
        self.cur.execute("""insert into cash_deposit_terms(account_id,treat_as_deposit,annual_rate,tax_rate,day_count,capitalization,rate_source,decided_by)
                            values (%s,true,0.06,0.19,365,'MONTHLY','test','test')""", (acc,))
        s = history.build_series(tm._conn, "BROKER:TR", end=end)["series"][-1]
        self.assertEqual(s["invested_net"], D("400.00"))                 # konto z lokata: wplata = tylko przelew 400 zl
        self.assertEqual(s["gain"], s["value"] - D("400.00"))            # premia jest w wartosci i w zysku
        self.assertEqual(s["deposits"], D("400.00"))                     # gotowka: 400 + 150 - 150

    def test_estimated_price_without_history_uses_last_trade_of_the_day(self):
        """Brak historii cen instrumentu: wykres szacuje cene z ostatniej transakcji DNIA (nie z najwyzszej ceny jednostkowej tego dnia)."""
        from datetime import date
        from invapp.pricing import history
        rows = [row(21, "2026-03-02T08:00:00.000Z", "CASH", "CUSTOMER_INPAYMENT", "200.00"),
                trade(22, "2026-03-02T08:30:00.000Z", "BUY", "PL0000000003", "0.001", "100.00", "-0.10", fee="-4.00"),   # koszt 4,10 zl za 0,001 szt.
                trade(23, "2026-03-02T14:00:00.000Z", "BUY", "PL0000000003", "1", "100.00", "-100.00", fee="-1.00")]    # 101 zl za 1 szt.
        self.imp(self.csv("szac.csv", rows))
        s = history.build_series(tm._conn, "BROKER:TR", end=date(2026, 3, 5))["series"][-1]
        self.assertEqual(s["estimated_positions"], 1)
        self.assertEqual(s["securities"], (D("1.001") * D("101")).quantize(D("0.01")))     # 1,001 szt. x 101 zl (ostatni zakup), nie x 4100 zl

    def batches(self):
        return [r["batch_id"] for r in self.rows("select batch_id from import_batches order by batch_id")]

    def test_remove_newer_export_returns_to_older_state(self):
        from invapp.core import removal
        self.imp(self.csv("styczen.csv", JAN)); only_jan = self.ledger()
        self.imp(self.csv("luty.csv", JAN + FEB))
        res = removal.remove_batch(tm._conn, self.batches()[-1])
        self.assertEqual(res["usuniete_operacje"], 2); self.assertEqual(res["usuniete_konta"], [])
        self.assertEqual(self.ledger(), only_jan)                                   # jak gdyby nowszego pliku nie wczytano
        self.assertEqual(self.one("select sum(qty_remaining) from derived_lots"), D("10"))
        self.assertEqual(self.one("select count(*) from raw_rows"), 4)              # surowe wiersze tylko pozostalego pliku
        c = self.imp(self.csv("luty.csv", JAN + FEB))                              # mozna wczytac ponownie
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (2, 4))

    def test_remove_older_export_keeps_operations_present_in_newer(self):
        from invapp.core import removal
        self.imp(self.csv("styczen.csv", JAN)); self.imp(self.csv("luty.csv", JAN + FEB)); both = self.ledger()
        first, second = self.batches()
        res = removal.remove_batch(tm._conn, first)
        self.assertEqual(res["usuniete_operacje"], 0); self.assertEqual(res["zachowane_wspolne"], 4)
        self.assertEqual(self.ledger(), both)
        self.assertEqual(self.one("select count(*) from transactions where first_batch_id <> %s or last_seen_batch_id <> %s", (second, second)), 0)

    def test_remove_only_file_removes_account_and_its_decisions(self):
        from invapp.core import removal
        self.imp(self.csv("styczen.csv", JAN))
        acc = self.one("select account_id from accounts")
        self.cur.execute("""insert into cash_deposit_terms(account_id,treat_as_deposit,annual_rate,tax_rate,day_count,capitalization,rate_source,decided_by)
                            values (%s,true,0.06,0.19,365,'MONTHLY','test','test')""", (acc,))
        res = removal.remove_batch(tm._conn, self.batches()[0])
        self.assertEqual(res["usuniete_konta"], ["Trade Republic"])
        for t in ("accounts", "transactions", "import_batches", "raw_rows", "derived_lots", "cash_deposit_terms", "flow_classification"):
            self.assertEqual(self.one(f"select count(*) from {t}"), 0, t)
        self.assertEqual(self.one("select count(*) from listings"), 1)              # instrument (bez danych konta) moze zostac - nie wplywa na wyniki
        with self.assertRaises(Exception):
            removal.remove_batch(tm._conn, 999999)
        self.cur.execute("update transactions set notes = 'x' where false")        # ochrona ksiegi znowu wlaczona:
        self.imp(self.csv("styczen.csv", JAN))
        self.cur.execute("savepoint s")
        with self.assertRaises(Exception):
            self.cur.execute("update transactions set occurred_at = occurred_at + interval '1 second'")      # rdzen ksiegi znowu chroniony wyzwalaczem
        self.cur.execute("rollback to savepoint s")

    def test_remove_whole_portfolio_removes_all_its_files(self):
        """Usun caly portfel (konto): wszystkie pliki konta znikaja, stan jak przed pierwszym importem; ponowny import dziala."""
        from invapp.core import removal
        from invapp.core.common import ImportFailed
        self.imp(self.csv("styczen.csv", JAN)); self.imp(self.csv("luty.csv", JAN + FEB))
        acc = self.one("select account_id from accounts")
        self.assertEqual(self.one("select count(*) from import_batches where account_id = %s", (acc,)), 2)
        res = removal.remove_account(tm._conn, acc)
        self.assertEqual((sorted(res["usuniete_pliki"]), res["usuniete_operacje"], res["usuniete_konta"], res["pozostale_operacje_konta"]),
                         (["luty.csv", "styczen.csv"], 6, ["Trade Republic"], 0))
        for t in ("accounts", "transactions", "import_batches", "raw_rows", "derived_lots", "flow_classification"):
            self.assertEqual(self.one(f"select count(*) from {t}"), 0, t)
        with self.assertRaises(ImportFailed):
            removal.remove_account(tm._conn, acc)                                  # konta juz nie ma
        c = self.imp(self.csv("luty.csv", JAN + FEB))
        self.assertEqual(c["INSERTED"], 6)

    def test_decisions_about_removed_data_are_forgotten_others_kept(self):
        """Po usunieciu danych: korekty instrumentow bez danych i reguly walut brokera bez plikow znikaja (pytania wroca); reszta zostaje."""
        from invapp.core import removal
        def listing(sym, ccy=None):
            s = self.one("insert into securities(name,asset_class) values (%s,'STOCK') returning security_id", (sym,))
            l = self.one("insert into listings(security_id,display_name,trading_currency) values (%s,%s,%s) returning listing_id", (s, sym, ccy))
            self.cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id) values (%s,%s,%s)", (self.xtb, sym, l))
            return l
        gone, kept = listing("AAA.PL"), listing("BBB.PL")
        self.cur.execute("""update listings set trading_currency = 'PLN', currency_status = 'POTWIERDZONY_PRZEZ_UZYTKOWNIKA', currency_source = 'regula .PL',
                            currency_checked_at = now() where listing_id = any(%s)""", ([gone, kept],))
        for l, st in ((gone, "REJECTED"), (kept, "CONFIRMED")):
            self.cur.execute("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,applies_to_lots_opened_before,ratio_new,ratio_old,source,status,evidence)
                                values (%s,'SPLIT',NULL,'UNKNOWN','2026-02-13T14:00Z',10,1,'INFERRED',%s,'{"r":1}')""", (l, st))
        self.cur.execute("insert into broker_symbol_suffix_rules(broker_id,symbol_suffix,value,source,confirmed_at) values (%s,'PL','PLN','test',now())", (self.xtb,))
        acc = self.account(self.xtb, "X9")
        self.tx(acc, self.xtb, "BUY", D(-100), "2026-01-05T10:00Z", listing=kept, qty=D(1), price=D(100))   # BBB.PL ma dane, AAA.PL nie
        b = self.one("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,status)
                        values (%s,'f',repeat('a',64),'p','1','COMMITTED') returning batch_id""", (self.xtb,))
        out = removal._forget_orphan_decisions(self.cur, self.xtb)
        self.assertEqual(out, {"korekty": 1, "reguly_walut": 0, "waluty_instrumentow": 0})                   # broker ma jeszcze plik: regula zostaje
        self.assertEqual(self.rows("select listing_id from corporate_actions"), [{"listing_id": kept}])
        self.cur.execute("delete from import_batches where batch_id = %s", (b,))
        out = removal._forget_orphan_decisions(self.cur, self.xtb)
        self.assertEqual(out, {"korekty": 0, "reguly_walut": 1, "waluty_instrumentow": 1})                   # brak plikow brokera: regula i waluta z reguly znikaja
        self.assertEqual(self.one("select currency_status from listings where listing_id = %s", (gone,)), "NIEZNANY")
        self.assertEqual(self.one("select trading_currency from listings where listing_id = %s", (kept,)), "PLN")   # instrument z danymi - bez zmian
        self.assertEqual(self.one("select count(*) from listing_attribute_history where listing_id = %s and attribute = 'TRADING_CURRENCY' and status = 'NIEZNANY'", (gone,)), 2)

    def test_remove_everything(self):
        from invapp.core import removal
        from invapp.core.common import ImportFailed
        self.imp(self.csv("styczen.csv", JAN)); self.imp(self.csv("luty.csv", JAN + FEB))
        res = removal.remove_all(tm._conn)
        self.assertEqual((len(res["usuniete_pliki"]), res["pozostale_operacje"]), (2, 0))
        for t in ("accounts", "transactions", "import_batches", "raw_rows"):
            self.assertEqual(self.one(f"select count(*) from {t}"), 0, t)
        with self.assertRaises(ImportFailed):
            removal.remove_all(tm._conn)

    def test_older_export_after_newer_adds_nothing(self):
        self.imp(self.csv("nowy.csv", JAN + FEB))
        ref = self.ledger()
        c = self.imp(self.csv("stary.csv", JAN))
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (0, 4))
        self.assertEqual(self.ledger(), ref)


if __name__ == "__main__":
    unittest.main()
