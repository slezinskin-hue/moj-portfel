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

    def test_older_export_after_newer_adds_nothing(self):
        self.imp(self.csv("nowy.csv", JAN + FEB))
        ref = self.ledger()
        c = self.imp(self.csv("stary.csv", JAN))
        self.assertEqual((c["INSERTED"], c["DUPLICATE"]), (0, 4))
        self.assertEqual(self.ledger(), ref)


if __name__ == "__main__":
    unittest.main()
