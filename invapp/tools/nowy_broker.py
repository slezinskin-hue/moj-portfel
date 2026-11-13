"""
Szkielet nowego brokera (decyzja uzytkownika 2026-11-13: "dodawanie nowych brokerow ma byc proste").

    python -m invapp.tools.nowy_broker revolut RVL "Revolut"

Tworzy dzialajacy od razu adapter dla prostego eksportu CSV (do dopasowania do prawdziwej probki):
    invapp/brokers/<klucz>/__init__.py, parser.py (kolumny, mapa typow operacji), importer.py (wspolny silnik partii), broker.py (klasa brokera)
    tests/test_import_<klucz>.py (import, ponowny import bez duplikatow, nieznany typ, usuniecie pliku = stan jak bez niego)
i dopisuje brokera do listy w invapp/brokers/__init__.py. Nastepne kroki: docs/DODAWANIE_BROKERA.md.
"""
import argparse, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PARSER = '''"""
Parser eksportu {name} (CSV). Wygenerowane przez `python -m invapp.tools.nowy_broker` - DOPASUJ do prawdziwej probki eksportu:
COLUMNS (naglowek pliku), TYPES (typ operacji z pliku -> typ w ksiedze), format daty i kwot (docs/DODAWANIE_BROKERA.md).
Zasady: nieznany typ = UNMAPPED (bez zgadywania), kwoty jako Decimal, waluta inna niz PLN = UNMAPPED (bez przeliczania na sile),
dane osobowe (IBAN, nazwiska z opisow) nigdy w ksiedze ani w surowych wierszach - tylko skrot/maska (core/common.mask_identifier).
"""
import csv, hashlib, os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from invapp.core.common import ImportFailed, row_hash

PARSER_NAME, PARSER_VERSION = "{key}_csv", "0.1"
COLUMNS = ["date", "id", "type", "isin", "name", "quantity", "price", "amount", "currency"]
# typ operacji z pliku -> typ w ksiedze: BUY, SELL, DIVIDEND, TAX_WITHHOLDING, INTEREST, TAX_INTEREST, FEE, REWARD, CASH_DEPOSIT, CASH_WITHDRAWAL
TYPES = {{
    "BUY": "BUY", "SELL": "SELL", "DIVIDEND": "DIVIDEND", "TAX": "TAX_WITHHOLDING", "INTEREST": "INTEREST",
    "FEE": "FEE", "DEPOSIT": "CASH_DEPOSIT", "WITHDRAWAL": "CASH_WITHDRAWAL",
}}
NEEDS_INSTRUMENT = ("BUY", "SELL", "DIVIDEND", "TAX_WITHHOLDING")
SIGN = {{"BUY": -1, "CASH_WITHDRAWAL": -1, "TAX_WITHHOLDING": -1, "TAX_INTEREST": -1, "CASH_DEPOSIT": 1, "INTEREST": 1, "REWARD": 1, "SELL": 1}}


@dataclass
class Candidate:
    row: int
    dedup_key: str
    row_hash: str
    payload: dict
    txn_type: str = None
    occurred_at: datetime = None
    cash_amount: Decimal = None
    external_id: str = None
    isin: str = None
    name: str = None
    quantity: Decimal = None
    price: Decimal = None
    gross: Decimal = None
    unmapped_reason: str = None


@dataclass
class ParsedFile:
    path: str
    name: str
    sha256: str
    records: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def header_matches(first_line):
    return [c.strip().strip('"') for c in first_line.split(",")] == COLUMNS


def parse(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or [c.strip() for c in rows[0]] != COLUMNS:
        raise ImportFailed(f"Plik '{{os.path.basename(path)}}' nie ma nagłówka eksportu {name}.")
    pf = ParsedFile(path=path, name=os.path.basename(path), sha256=sha256_file(path))
    for i, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(COLUMNS):
            raise ImportFailed(f"Wiersz {{i}}: {{len(r)}} pól zamiast {{len(COLUMNS)}} – plik jest uszkodzony.")
        rec = dict(zip(COLUMNS, r)); rec["row"] = i
        pf.records.append(rec)
    return pf


def _dec(s):
    try:
        return Decimal(s.strip().replace(" ", "").replace(",", ".")) if s and s.strip() else None
    except InvalidOperation:
        raise ImportFailed(f"Nieczytelna liczba: {{s!r}}")


def normalize(rec):
    payload = {{c: rec[c] for c in COLUMNS}}
    ext = rec["id"].strip()
    c = Candidate(row=rec["row"], dedup_key=f"{key}:{{ext}}", row_hash=row_hash(payload), payload=payload, external_id=ext)
    typ = TYPES.get(rec["type"].strip().upper())
    if not ext:
        c.unmapped_reason = "brak identyfikatora operacji"; return c
    if typ is None:
        c.unmapped_reason = f"nieznany typ operacji '{{rec['type']}}'"; return c
    if rec["currency"].strip().upper() != "PLN":
        c.unmapped_reason = f"waluta {{rec['currency']}} – szablon obsługuje tylko PLN (bez przeliczania na siłę)"; return c
    c.txn_type = typ
    d = datetime.fromisoformat(rec["date"].strip())                     # data albo data z czasem (ISO); bez strefy = UTC
    c.occurred_at = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    c.cash_amount = _dec(rec["amount"])
    c.isin, c.name = (rec["isin"].strip() or None), (rec["name"].strip() or None)
    if typ in NEEDS_INSTRUMENT and not c.isin:
        c.unmapped_reason = "brak ISIN dla operacji na instrumencie"; return c
    if typ in ("BUY", "SELL"):
        c.quantity, c.price = _dec(rec["quantity"]), _dec(rec["price"])
        if not c.quantity or c.price is None:
            c.unmapped_reason = "brak ilości lub ceny"; return c
        c.gross = c.quantity * c.price
    if c.cash_amount is None or (SIGN.get(typ, 0) * c.cash_amount < 0):
        c.unmapped_reason = f"kwota {{rec['amount']}} ma zły znak dla {{typ}}"; return c
    return c
'''

IMPORTER = '''"""
Import eksportu {name} do ksiegi - wspolny silnik partii (invapp/core/ledger_import.py): idempotencja, konflikty, surowe wiersze, kontrola G3.
Wygenerowane przez `python -m invapp.tools.nowy_broker`. Tu tylko to, co specyficzne dla brokera: konto, instrumenty, kolumny ksiegi.
"""
from invapp.core.ledger_import import ImportBatch, IsinResolver, ensure_account

from . import parser as p

CODE = "{code}"


def import_file(conn, path):
    pf = p.parse(path)
    cur = conn.cursor()
    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("import:" + CODE,))
    # Plik bez numeru rachunku: jedno konto. Jesli eksport ma numer konta (w nazwie pliku albo kolumnie) - uzyj go jako external_no.
    account, created = ensure_account(cur, CODE, CODE + "-DEFAULT", "{name}")
    b = ImportBatch(conn, CODE, pf.name, pf.sha256, p.PARSER_NAME, p.PARSER_VERSION, account_id=account, warnings=pf.warnings)
    b.report["created_accounts"] = ["{name}"] if created else []
    resolver = IsinResolver(cur, CODE, "eksport {name}")
    cands = [p.normalize(r) for r in pf.records]
    b.prefetch([c.dedup_key for c in cands if not c.unmapped_reason])
    for c in cands:
        if c.unmapped_reason:
            b.raw_row("CSV", c.row, "DATA", c.dedup_key, c.row_hash, c.payload, "UNMAPPED")
            b.report["warnings"].append(f"Wiersz {{c.row}}: {{c.unmapped_reason}} – wiersz nie trafia do księgi.")
            continue
        b.write_transaction({{
            "account_id": account, "txn_type": c.txn_type, "occurred_at": c.occurred_at, "cash_amount": c.cash_amount,
            "listing_id": resolver.listing(c.isin, c.name) if c.isin else None, "quantity": c.quantity, "price": c.price,
            "price_currency": "PLN" if c.txn_type in ("BUY", "SELL") else None, "gross_amount": c.gross,
            "external_id": c.external_id, "external_id_scheme": CODE + "_ID", "dedup_key": c.dedup_key, "row_hash": c.row_hash,
        }}, "CSV", c.row, c.payload)
    classify_flows(conn, b.report)
    return b.finish()


def classify_flows(conn, report):
    """Wplaty i wyplaty z banku = przeplywy zewnetrzne (wplacony kapital). Powiazania (np. podatek -> dywidenda) dopisz tutaj, jesli eksport je ma."""
    cur = conn.cursor()
    cur.execute("""insert into flow_classification(txn_id,flow_class,method,confidence,decided_by,classifier_version)
                   select t.txn_id, 'EXTERNAL', 'DEFAULT', 'MEDIUM', 'rule:import', %s from transactions t
                   join financial_institutions f using (broker_id)
                   where f.code = %s and t.txn_type in ('CASH_DEPOSIT','CASH_WITHDRAWAL')
                     and not exists (select 1 from flow_classification fc where fc.txn_id = t.txn_id and fc.is_current)""", ("import-" + CODE.lower() + "-0.1", CODE))
    report["flow_classes"] = {{"EXTERNAL": cur.rowcount}}
'''

BROKER = '''"""
Broker {name}: eksport CSV. Wygenerowane przez `python -m invapp.tools.nowy_broker` - opis formatu i decyzje dopisz tutaj.
Partie liczone metoda FIFO (eksport bez numerow pozycji), instrumenty po ISIN, waluta notowania i symbol u dostawcy cen ustalane dla
otwartych pozycji ze zrodla zewnetrznego ze sprawdzeniem ceny (jak Trade Republic).
"""
import os

from invapp.brokers.base import Broker
from invapp.core import derive
from invapp.core.common import ImportFailed

from . import importer, parser as p

MAX_BYTES = 20_000_000


class {cls}(Broker):
    code, key, name = "{code}", "{key}", "{name}"
    accept = (".csv",)
    file_help = "<strong>{name}</strong>: plik <strong>.csv</strong> z historią operacji (oryginalny eksport, bez zapisywania w Excelu)."

    def matches(self, filename, head):
        if not filename.lower().endswith(".csv"):
            return False
        first = head[:1000].decode("utf-8-sig", errors="replace").splitlines()[:1]
        return bool(first) and p.header_matches(first[0])

    def validate_upload(self, filename, data):
        if len(data) > MAX_BYTES or len(data) < 20:
            raise ImportFailed("Plik ma nieprawidłowy rozmiar.")

    def import_path(self, conn, path, display_name=None):
        rep = importer.import_file(conn, path)
        rep["derived"] = self.rebuild_derived(conn)
        if display_name:
            rep["file"] = display_name
        return rep

    def rebuild_derived(self, conn):
        return derive.rebuild_fifo(conn, self.code)

    def relink(self, conn, report):
        importer.classify_flows(conn, report)

    def pricing_steps(self, conn, provider):
        from invapp.brokers.trade_republic import pricing
        return [(f"Symbole {{self.name}} (ISIN → dostawca cen)",
                 lambda: [r["status"] for r in pricing.resolve_isin_listings(conn, provider, broker_code=self.code)], "quick")]
'''

TEST = '''"""Import {name} (wygenerowany szablon - rozbuduj o prawdziwa probke eksportu i liczby z analizy). Dane SYNTETYCZNE."""
import csv, os, shutil, sys, tempfile, unittest
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for x in (ROOT, os.path.join(ROOT, "tests")):
    if x not in sys.path:
        sys.path.insert(0, x)
import baza_testowa as tm  # noqa: E402
from invapp import brokers  # noqa: E402
from invapp.brokers.{key} import parser as p  # noqa: E402
from invapp.web import importing  # noqa: E402

setUpModule = tm.setUpModule
tearDownModule = tm.tearDownModule
ROWS = [
    ["2026-01-02", "t1", "DEPOSIT", "", "", "", "", "1000.00", "PLN"],
    ["2026-01-05", "t2", "BUY", "PL0000000009", "Testowa SA", "10", "50.00", "-501.00", "PLN"],
    ["2026-02-01", "t3", "DIVIDEND", "PL0000000009", "Testowa SA", "", "", "12.00", "PLN"],
    ["2026-02-01", "t4", "TAX", "PL0000000009", "Testowa SA", "", "", "-2.28", "PLN"],
    ["2026-03-01", "t5", "SELL", "PL0000000009", "Testowa SA", "4", "60.00", "239.00", "PLN"],
    ["2026-03-02", "t6", "COŚ_NOWEGO", "", "", "", "", "1.00", "PLN"],
]


class Test{cls}Import(tm.Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="invapp_{key}_")
        brokers.ensure_registered(tm._conn)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def csv(self, name, rows):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f); w.writerow(p.COLUMNS); w.writerows(rows)
        return path

    def imp(self, path):
        return importing.import_path(tm._conn, path, os.path.basename(path))

    def test_import_reimport_unknown_type_and_removal(self):
        from invapp.core import removal
        b = brokers.by_code("{code}")
        path = self.csv("historia.csv", ROWS)
        with open(path, "rb") as f:
            self.assertTrue(b.matches("historia.csv", f.read(2000)))
        r = self.imp(path)
        self.assertEqual((r["counters"]["INSERTED"], r["counters"]["UNMAPPED"]), (5, 1))                # nieznany typ nie trafia do ksiegi
        self.assertEqual(self.one("select sum(qty_remaining) from derived_lots"), D("6"))              # FIFO: 10 - 4
        r = self.imp(self.csv("historia (2).csv", ROWS))
        self.assertEqual((r["counters"]["INSERTED"], r["counters"]["DUPLICATE"]), (0, 5))              # ponowny import niczego nie dubluje
        changed = [list(x) for x in ROWS]; changed[0][7] = "999.00"
        r = self.imp(self.csv("zmieniony.csv", changed))
        self.assertEqual(r["counters"]["CONFLICT"], 1)                                                  # ten sam id, inna tresc = konflikt
        for bid in [x["batch_id"] for x in self.rows("select batch_id from import_batches order by batch_id desc")]:
            removal.remove_batch(tm._conn, bid)
        self.assertEqual(self.one("select count(*) from transactions"), 0)                            # usuniecie = stan jak bez plikow
        self.assertEqual(self.one("select count(*) from accounts"), 0)
'''


def register(key, cls):
    path = os.path.join(ROOT, "invapp", "brokers", "__init__.py")
    src = open(path, encoding="utf-8").read()
    if f"invapp.brokers.{key}.broker" in src:
        return False
    src = src.replace("from invapp.brokers.xtb.broker import Xtb\n", f"from invapp.brokers.xtb.broker import Xtb\nfrom invapp.brokers.{key}.broker import {cls}\n", 1)
    src = re.sub(r"BROKERS = \[(.*?)\]", lambda m: f"BROKERS = [{m.group(1)}, {cls}()]", src, count=1)
    open(path, "w", encoding="utf-8", newline="").write(src)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m invapp.tools.nowy_broker", description="Szkielet nowego brokera (CSV).")
    ap.add_argument("klucz", help="klucz w adresie i nazwa folderu, np. revolut (male litery, cyfry, _)")
    ap.add_argument("kod", help="kod w bazie, np. RVL (wielkie litery, cyfry)")
    ap.add_argument("nazwa", help='nazwa w interfejsie, np. "Revolut"')
    ap.add_argument("--bez-rejestracji", action="store_true", help="nie dopisuj do listy BROKERS")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", a.klucz) or not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", a.kod):
        sys.exit("Klucz: małe litery/cyfry/_ (np. revolut); kod: wielkie litery/cyfry (np. RVL).")
    from invapp import brokers
    if any(b.key == a.klucz or b.code == a.kod for b in brokers.BROKERS):
        sys.exit("Broker o tym kluczu albo kodzie już istnieje.")
    cls = "".join(x.capitalize() for x in a.klucz.split("_"))
    folder = os.path.join(ROOT, "invapp", "brokers", a.klucz)
    if os.path.exists(folder):
        sys.exit(f"Folder {folder} już istnieje.")
    os.makedirs(folder)
    fmt = {"key": a.klucz, "code": a.kod, "name": a.nazwa, "cls": cls}
    files = {os.path.join(folder, "__init__.py"): "", os.path.join(folder, "parser.py"): PARSER.format(**fmt),
             os.path.join(folder, "importer.py"): IMPORTER.format(**fmt), os.path.join(folder, "broker.py"): BROKER.format(**fmt),
             os.path.join(ROOT, "tests", f"test_import_{a.klucz}.py"): TEST.format(**fmt)}
    for path, text in files.items():
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("utworzono", os.path.relpath(path, ROOT))
    if not a.bez_rejestracji and register(a.klucz, cls):
        print("dopisano do invapp/brokers/__init__.py:", f"{cls}()")
    print(f"""
Dalej (docs/DODAWANIE_BROKERA.md):
  1. Na prawdziwej próbce eksportu dopasuj COLUMNS, TYPES i format daty/kwot w invapp/brokers/{a.klucz}/parser.py.
  2. python -m unittest tests.test_import_{a.klucz} tests.test_kontrakt_brokerow
  3. Dopisz test na prawdziwym pliku (lokalnie, poza repozytorium) i mutacje w tests/mutation_check_import.py.""")


if __name__ == "__main__":
    main()
