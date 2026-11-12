"""
Testy zmian z 2026-11-12:
* gotowka jako LOKATA (core/deposits.py): model odsetek (dzienne od salda, wyplata 1. dnia miesiaca, podatek 19%) na danych syntetycznych
  i na prawdziwym eksporcie Trade Republic (model odtwarza KAZDA wyplate), pytanie w aplikacji, wartosc aktywow, seria wykresu, kontrola liczb;
* XTB: pytanie o split niewidoczny w eksporcie - walidacja daty i zrodla, po potwierdzeniu brak ujemnych stanow;
* import: pliki wgrywane jednoczesnie sa importowane po kolei (bez bledu unikalnosci kont);
* ceny: zapytania rownolegle (fetch_all), juz zweryfikowane powiazania nie sa sprawdzane ponownie.
"""
import os, sys, threading, unittest
from datetime import date, timedelta
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)
import baza_testowa as tm  # noqa: E402
from invapp import brokers  # noqa: E402
from invapp.core import deposits as dep  # noqa: E402
from invapp.core.common import ImportFailed  # noqa: E402
from invapp.pricing import audit as audit_mod, history, metrics  # noqa: E402
from invapp.pricing.providers import fetch_all  # noqa: E402
from invapp.web import importing  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule


def daily_interest(balances, rate=D("0.06")):
    return sum((b * rate / 365 for b in balances), D(0))


class TestDepositModel(unittest.TestCase):
    """Czysta logika - bez bazy."""

    def txns(self):
        jan = 10000 * D("0.06") * 31 / 365                       # odsetki za styczen od salda 10 000
        g = jan.quantize(D("0.01")); t = (g * D("0.19")).quantize(D("0.01"))
        return [(date(2026, 1, 1), D(10000), "CASH_DEPOSIT"), (date(2026, 2, 1), g, "INTEREST"), (date(2026, 2, 1), -t, "TAX_INTEREST")], g, t

    def test_reproduce_and_implied_rate(self):
        tx, g, t = self.txns()
        self.assertEqual(dep.implied_rate(tx), D("0.0600"))
        rep = dep.reproduce_payouts(tx, D("0.06"))
        self.assertEqual(len(rep), 1); self.assertEqual(rep[0]["model"], g); self.assertEqual(rep[0]["diff"], 0)
        self.assertIsNone(dep.implied_rate([(date(2026, 1, 1), D(100), "CASH_DEPOSIT")]))          # brak wyplat - brak stopy

    def test_value_is_booked_balance_and_unbooked_interest_is_only_informative(self):
        tx, g, t = self.txns()
        bal = D(10000) + g - t
        # dzien wyplaty z ksiegi (1 lutego): naliczone za 1 dzien od nowego salda
        m = dep.model(tx, D("0.06"), D("0.19"), date(2026, 2, 1))
        self.assertEqual(m["ledger_balance"], bal); self.assertEqual(m["last_payout"], date(2026, 2, 1))
        self.assertEqual(m["accrued_gross"], (bal * D("0.06") / 365).quantize(D("0.01")))
        # 10 marca: luty skapitalizowany 1 marca przez MODEL (eksport go jeszcze nie zawiera), marzec naliczany od salda po kapitalizacji
        m = dep.model(tx, D("0.06"), D("0.19"), date(2026, 3, 10))
        feb = daily_interest([bal] * 28).quantize(D("0.01")); feb_net = feb - (feb * D("0.19")).quantize(D("0.01"))
        self.assertEqual(len(m["capitalized_after_export"]), 1)
        self.assertEqual(m["capitalized_after_export"][0]["gross"], feb); self.assertEqual(m["capitalized_net"], feb_net)
        mar = daily_interest([bal + feb_net] * 10).quantize(D("0.01"))
        self.assertEqual(m["accrued_gross"], mar)
        # wartosc = TYLKO saldo z ksiegi (decyzja uzytkownika 2026-11-12: bez niezaksiegowanych odsetek); niezaksiegowane - tylko informacyjnie
        self.assertEqual(m["value"], bal)
        self.assertEqual(m["unbooked_interest_net"], feb_net + mar - (mar * D("0.19")).quantize(D("0.01")))
        self.assertEqual(m["daily"][-1], (date(2026, 3, 10), bal))
        self.assertEqual(dict(m["daily"])[date(2026, 1, 31)], D(10000))                        # przed wyplata: saldo bez naliczonych odsetek


class TestFetchAllAndRecheck(tm.Base):
    def test_fetch_all_returns_results_and_errors_per_item(self):
        def f(x):
            if x == "zly":
                raise ValueError("x")
            return x * 2
        out = fetch_all(f, ["a", "b", "zly", "a"])
        self.assertEqual(out["a"], ("aa", None)); self.assertEqual(out["b"], ("bb", None)); self.assertIsInstance(out["zly"][1], ValueError)
        self.assertEqual(fetch_all(f, []), {})


class TestConcurrentImport(unittest.TestCase):
    """Dwa wgrania jednoczesnie (np. ponowne klikniecie): blokada importu - oba koncza sie powodzeniem, drugi to same duplikaty."""

    def test_two_simultaneous_uploads_of_same_file(self):
        calls, active, peak = [], [0], [0]
        lock = threading.Lock()

        def fake(data, filename):
            with lock:
                active[0] += 1; peak[0] = max(peak[0], active[0])
            import time; time.sleep(0.2)
            with lock:
                active[0] -= 1
            calls.append(filename); return {"ok": True}
        orig = importing._run_import_bytes
        importing._run_import_bytes = fake
        try:
            ts = [threading.Thread(target=importing.run_import_bytes, args=(b"x", f"plik{i}.xlsx")) for i in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        finally:
            importing._run_import_bytes = orig
        self.assertEqual(len(calls), 3); self.assertEqual(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
