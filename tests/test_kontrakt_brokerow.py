"""
Kontrakt brokera: wspolne wymagania sprawdzane AUTOMATYCZNIE dla kazdego brokera z listy invapp/brokers/__init__.BROKERS
(takze nowego, dodanego szkieletem `python -m invapp.tools.nowy_broker`). Opis interfejsu: invapp/brokers/base.py, docs/DODAWANIE_BROKERA.md.
"""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)
import baza_testowa as tm  # noqa: E402
from invapp import brokers  # noqa: E402
from invapp.brokers.base import QUESTION_CATEGORIES, Broker  # noqa: E402
from invapp.core.common import ImportFailed  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


class FakeProvider:
    """Dostawca cen bez sieci (kroki cen nie moga wymagac internetu, zeby sie zbudowac)."""
    code = "YAHOO"

    def __getattr__(self, name):
        raise AssertionError(f"krok cen wywolal dostawce ({name}) juz przy budowaniu listy krokow")


class TestBrokerContract(tm.Base):
    def setUp(self):
        super().setUp()
        brokers.ensure_registered(tm._conn)

    def test_identity_is_unique_and_well_formed(self):
        bs = brokers.BROKERS
        self.assertTrue(bs)
        for attr in ("code", "key", "name"):
            vals = [getattr(b, attr) for b in bs]
            self.assertEqual(len(vals), len(set(vals)), f"powtorzone {attr}")
        for b in bs:
            with self.subTest(broker=b.name):
                self.assertIsInstance(b, Broker)
                self.assertRegex(b.code, r"^[A-Z][A-Z0-9]{1,15}$")
                self.assertRegex(b.key, r"^[a-z][a-z0-9_]{1,30}$")
                self.assertTrue(b.name and b.file_help, "nazwa i opis plikow dla uzytkownika")
                self.assertTrue(b.accept and all(e.startswith(".") for e in b.accept))
                self.assertEqual(b.scope_code, f"BROKER:{b.code}")
                self.assertIs(brokers.by_key(b.key), b); self.assertIs(brokers.by_code(b.code), b)

    def test_registered_in_database(self):
        for b in brokers.BROKERS:
            with self.subTest(broker=b.name):
                self.assertEqual(self.one("select count(*) from financial_institutions where code = %s", (b.code,)), 1)
                self.assertEqual(self.one("select count(*) from scopes where code = %s", (b.scope_code,)), 1)

    def test_foreign_files_are_not_claimed_and_bad_uploads_rejected(self):
        for b in brokers.BROKERS:
            with self.subTest(broker=b.name):
                for name, head in (("zdjecie.jpg", b"\xff\xd8\xff"), ("notatki.txt", b"hello"), ("x.csv", b"a,b,c\n1,2,3\n"), ("x.xlsx", b"PK\x03\x04")):
                    self.assertFalse(b.matches(name, head), name)              # cudzy plik nie jest "rozpoznany"
                with self.assertRaises(ImportFailed):
                    b.validate_upload("pusty" + b.accept[0], b"")

    def test_empty_database_operations(self):
        for b in brokers.BROKERS:
            with self.subTest(broker=b.name):
                self.assertIsInstance(b.rebuild_derived(tm._conn), dict)        # przeliczenie bez danych nie psuje sie
                report = {"warnings": [], "info": []}
                b.relink(tm._conn, report)
                self.assertIsInstance(b.pending_questions(tm._conn), list)
                with self.assertRaises(ImportFailed):
                    b.answer_question(tm._conn, f"{b.key}-nieznane-pytanie", "tak", {})
                steps = b.pricing_steps(tm._conn, FakeProvider())
                for s in steps:
                    self.assertTrue(2 <= len(s) <= 3 and isinstance(s[0], str) and callable(s[1]), s)
                    if len(s) == 3:
                        self.assertIn(s[2], ("quick", "slow", "history"))
                self.assertIsNone(b.negative_state_fix(self.cur, -1))

    def test_question_format(self):
        """Pytania brokerow (gdy sa) maja format wymagany przez interfejs i diagnoze kontroli liczb."""
        def check(q):
            self.assertRegex(q["id"], r"^[a-z0-9-]+$")
            self.assertIn(q.get("kategoria"), QUESTION_CATEGORIES)
            self.assertTrue(q["tytul"] and q["tresc"])
            self.assertTrue(q["odpowiedzi"])
            for o in q["odpowiedzi"]:
                self.assertTrue(o["wartosc"] and o["etykieta"])
        for b in brokers.BROKERS:
            for q in b.pending_questions(tm._conn):
                with self.subTest(broker=b.name, pytanie=q.get("id")):
                    check(q)

    def test_no_broker_specific_code_outside_broker_folders(self):
        """Wspolny kod nie rozpoznaje brokerow po nazwie/kodzie - wszystko idzie przez interfejs (wyjatek: wartosci domyslne parametrow)."""
        bad = []
        for folder in ("core", "pricing", "web"):
            for dirpath, _, files in os.walk(os.path.join(ROOT, "invapp", folder)):
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    for i, line in enumerate(open(os.path.join(dirpath, f), encoding="utf-8"), 1):
                        if re.search(r"""(==|!=|\bin)\s*\(?\s*["'](XTB|TR)["']""", line) or re.search(r"""(?<![a-z_])code\s*==\s*["']""", line):
                            bad.append(f"{f}:{i}: {line.strip()[:90]}")
        self.assertEqual(bad, [])


class TestImportBatch(tm.Base):
    """Wspolny silnik partii importu (core/ledger_import.py) - uzywany przez Trade Republic i brokerow z generatora."""

    def setUp(self):
        super().setUp()
        brokers.ensure_registered(tm._conn)

    def values(self, acc, key, amount, h):
        from datetime import datetime, timezone
        return {"account_id": acc, "txn_type": "CASH_DEPOSIT", "occurred_at": datetime(2026, 1, 2, tzinfo=timezone.utc), "cash_amount": amount,
                "dedup_key": key, "row_hash": h, "external_id": key, "external_id_scheme": "T_ID"}

    def test_insert_duplicate_conflict_and_raw_rows(self):
        from decimal import Decimal as D
        from invapp.core.ledger_import import ImportBatch, IsinResolver, ensure_account
        acc, new = ensure_account(self.cur, "TR", "TR-TEST", "Test")
        self.assertTrue(new); self.assertEqual(ensure_account(self.cur, "TR", "TR-TEST", "Test"), (acc, False))
        self.assertEqual(self.one("select count(*) from scope_accounts where account_id = %s", (acc,)), 3)       # BROKER:TR, WSZYSTKIE, majatek
        b = ImportBatch(tm._conn, "TR", "a.csv", "a" * 64, "p", "1", account_id=acc)
        b.prefetch(["k1"])
        self.assertEqual(b.write_transaction(self.values(acc, "k1", D("10"), "h1"), "CSV", 2, {"x": 1})[1], "INSERTED")
        self.assertEqual(b.write_transaction(self.values(acc, "k1", D("10"), "h1"), "CSV", 3, {"x": 1})[1], "DUPLICATE")    # drugi raz w pliku
        b.raw_row("CSV", 4, "DATA", None, None, {"x": 2}, "UNMAPPED")
        r = b.finish()
        self.assertEqual((r["counters"]["INSERTED"], r["counters"]["DUPLICATE"], r["counters"]["UNMAPPED"], r["counters"]["read"]), (1, 1, 1, 3))
        b2 = ImportBatch(tm._conn, "TR", "b.csv", "b" * 64, "p", "1", account_id=acc)
        b2.prefetch(["k1", "k2"])
        tid, oc = b2.write_transaction(self.values(acc, "k1", D("11"), "h-zmieniony"), "CSV", 2, {"x": 3})
        self.assertEqual(oc, "CONFLICT")
        self.assertEqual(self.one("select cash_amount from transactions where txn_id = %s", (tid,)), D("10"))       # istniejacy wiersz zachowany
        b2.finish()
        self.assertEqual(self.one("select count(*) from import_conflicts where batch_id = %s", (b2.batch,)), 1)
        self.assertEqual(self.one("select count(*) from raw_rows where batch_id = %s", (b.batch,)), 3)
        with self.assertRaises(ImportFailed):
            ImportBatch(tm._conn, "TR", "c.csv", "c" * 64, "p", "1").write_transaction({"account_id": acc}, "CSV", 2, {})   # brak pol
        lid = IsinResolver(self.cur, "TR", "eksport testowy").listing("PL0000000077", "Testowa")
        self.assertEqual(IsinResolver(self.cur, "TR", "eksport testowy").listing("PL0000000077"), lid)
        self.assertIsNone(self.one("select trading_currency from listings where listing_id = %s", (lid,)))           # waluta nieznana (nie zgadujemy)

    def test_g3_counts_every_row(self):
        from invapp.core.ledger_import import ImportBatch
        b = ImportBatch(tm._conn, "TR", "d.csv", "d" * 64, "p", "1")
        b.raw_row("CSV", 2, "DATA", None, None, {}, "IGNORED")
        b.counters["read"] += 1                                                    # wiersz przeczytany, a bez wyniku
        with self.assertRaises(ImportFailed):
            b.finish()


if __name__ == "__main__":
    unittest.main()
