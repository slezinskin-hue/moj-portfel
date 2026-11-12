"""
Parser specyfikacji XTB "Specification Table Organised Market Instruments (OMI)" (PDF) -> wiersze (symbol, nazwa, ISIN, waluta, typ).
Tylko odczyt pliku. Zadnych zgadniec: wiersz, ktorego nie da sie sparsowac, jest zglaszany (nie pomijany po cichu).
Kolumny tabeli w dokumencie: Symbol | Company Name | ISIN | Currency | [Type] | Minimum transaction value | Trading Hours | Trading Days.
Dokument NIE zawiera gieldy (MIC) ani jednostki ceny (GBP/GBp).
"""
import hashlib, re

LINE_RE = re.compile(
    r"^(?P<sym>[A-Z0-9][A-Z0-9&\-._]*\.[A-Z]{2,3})(?P<star>\*)? (?P<name>.+?) (?P<isin>[A-Z]{2}[A-Z0-9]{9}\d) (?P<cur>[A-Z]{3}) "
    r"(?:(?P<typ>ETF|ETN|ETC) )?(?P<minv>\d+(?:\.\d+)?) (?P<minc>[A-Z]{3}) (?P<rest>.+)$")
SYMBOL_START = re.compile(r"^[A-Z0-9][A-Z0-9&\-._]*\.[A-Z]{2,3}\*? ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_pages_text(path):
    from pypdf import PdfReader
    return [(p.extract_text() or "") for p in PdfReader(path).pages]


def parse_pages(pages):
    """Zwraca (rows, unparsed, stats). Tabela 'Fractional Rights' (od naglowka w srodku strony) jest pomijana: to inna tabela z tymi samymi symbolami.
    Symbol zdefiniowany sprzecznie (np. LB.US = LandBridge i L Brands) jest WYKLUCZONY ze zbioru i zgloszony w stats['conflicting_symbols']:
    nie da sie go jednoznacznie zweryfikowac, wiec nie zgadujemy."""
    if "Specification Table Organised Market Instruments (OMI)" not in pages[0]:
        raise ValueError("To nie jest specyfikacja OMI XTB (brak naglowka na pierwszej stronie).")
    rows, unparsed, seen, conflicting, done = [], [], {}, set(), False
    key = ("name", "isin", "currency", "type")
    for pno, text in enumerate(pages[1:], start=2):
        if done:
            break
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Specification Table Fractional Rights"):
                done = True; break
            if not SYMBOL_START.match(line):
                continue
            m = LINE_RE.match(line)
            if not m:
                unparsed.append((pno, line)); continue
            row = {"symbol": m["sym"], "name": m["name"].strip(), "isin": m["isin"], "currency": m["cur"],
                   "type": m["typ"] or "STOCK", "page": pno, "close_only": bool(m["star"])}
            if row["symbol"] in seen:
                if any(seen[row["symbol"]][k] != row[k] for k in key):
                    conflicting.add(row["symbol"])
                continue
            seen[row["symbol"]] = row; rows.append(row)
    rows = [r for r in rows if r["symbol"] not in conflicting]
    stats = {"rows": len(rows), "unparsed": len(unparsed), "conflicting_symbols": sorted(conflicting), "by_type": {}, "by_currency": {}}
    for r in rows:
        stats["by_type"][r["type"]] = stats["by_type"].get(r["type"], 0) + 1
        stats["by_currency"][r["currency"]] = stats["by_currency"].get(r["currency"], 0) + 1
    return rows, unparsed, stats