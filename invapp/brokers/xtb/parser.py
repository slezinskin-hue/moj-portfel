"""
Adapter XTB (.xlsx): rozpoznanie pliku, parsowanie arkuszy, normalizacja do kandydatow na transakcje (dokumenty 01, 02, 07).
Plik jest TYLKO CZYTANY. Nic tu nie zapisuje do bazy - zapisem zajmuje sie engine.py.
"""
import calendar, hashlib, os, re, warnings
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import openpyxl

from invapp.core.common import ImportFailed, as_utc, blank, canon, clean, dec, iso_ms, row_hash

warnings.filterwarnings("ignore", category=UserWarning)

FILE_RE = re.compile(r"^(?P<label>[A-Z]+)_(?P<acct>\d+)_(?P<from>\d{4}-\d{2}-\d{2})_(?P<to>\d{4}-\d{2}-\d{2})(?: \(\d+\))?\.xlsx$")   # " (2)" - kolejne pobranie tego samego pliku w Windows
PARSER_NAME, PARSER_VERSION = "xtb_xlsx", "1.0"

# --- wzorce komentarzy (dokument 2 §2.3)
RX_TRADE = re.compile(r"^(?P<dir>OPEN|CLOSE) BUY (?:(?P<ticker>\S+\.[A-Z]+) )?(?P<vol>\d+(?:\.\d+)?)(?:/(?P<order_vol>\d+(?:\.\d+)?))? @ (?P<price>\d+(?:\.\d+)?)$")
RX_DIV = re.compile(r"^(?P<corr>corr )?(?P<ticker>\S+) (?P<ccy>[A-Z]{3}) (?P<dps>\d+(?:\.\d+)?)/ SHR$")
RX_WHT = re.compile(r"^(?P<ticker>\S+) (?P<ccy>[A-Z]{3}) WHT (?P<rate>\d+(?:\.\d+)?)%$")
RX_INTEREST = re.compile(r"^Free-funds Interest(?P<tax> Tax)? (?P<ym>\d{4}-\d{2})$")
RX_SEC = re.compile(r"^(?P<corr>corr )?Sec Fee adj (?P<ticker>\S+) (?P<ymd>\d{8})$")
RX_IKE_TR = re.compile(r"^Transfer (?P<dir>in|out) operation on account with id (?P<acct>\d+)$")
RX_SUB_TR = re.compile(r"^Transfer from (?P<frm>\d+) to (?P<to>\d+)$")
RX_PAYMENT = re.compile(r"^(?P<channel>eWallet|XTB Card) OT\|DIR:(?P<dir>I2B|B2I)\|BA:(?P<ba>\d+)\|IA:(?P<ia>\d+)\|BR:(?P<br>\w+)\|CID:(?P<cid>[\w-]+)$")
RX_DEPOSIT = re.compile(r"^(?P<channel>.+?) deposit,")

# typ zrodlowy -> (typ wewnetrzny, dozwolony znak kwoty: '+', '-', None=dowolny)
CASH_TYPES = {
    "Deposit": "CASH_DEPOSIT", "Withdrawal": "CASH_WITHDRAWAL", "Stock purchase": "BUY", "Stock sell": "SELL",
    "Dividend": "DIVIDEND", "Withholding tax": "TAX_WITHHOLDING", "Free funds interest": "INTEREST",
    "Free funds interest tax": "TAX_INTEREST", "SEC fee": "FEE", "Correction": "FEE", "IKE deposit": "TRANSFER", "Subaccount transfer": "TRANSFER",
}


@dataclass
class ParsedFile:
    path: str
    name: str
    sha256: str
    label: str
    account_no: str                      # z nazwy pliku
    header_accounts: dict                # numer konta z naglowkow arkuszy
    period_from: datetime = None
    period_to: datetime = None
    report_generated_at: datetime = None
    cash: list = field(default_factory=list)          # {row, ...kolumny}
    cash_total: dict = None                           # {row, amount}
    closed: list = field(default_factory=list)
    closed_footer: dict = None
    open_rows: list = field(default_factory=list)     # pozycje i instrumenty
    open_summary: list = field(default_factory=list)  # {row, product, metric, amount, currency}
    open_notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect(path):
    """Czy to plik XTB? Zwraca dict (label, account_no) albo None. Tylko nazwa i arkusze - bez zapisu."""
    m = FILE_RE.match(os.path.basename(path))
    if not m:
        return None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if {"Closed Positions", "Cash Operations", "Open Positions"} - set(wb.sheetnames):
            return None
    finally:
        wb.close()
    return {"label": m["label"], "account_no": m["acct"], "from": m["from"], "to": m["to"]}


def _rows(ws):
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _find_header(rows, first_label, col=0):
    for i, r in enumerate(rows):
        if len(r) > col and r[col] == first_label:
            return i
    raise ImportFailed(f"Nie znaleziono nagłówka tabeli '{first_label}' – nierozpoznana struktura arkusza.")


def parse(path):
    d = detect(path)
    if not d:
        raise ImportFailed(f"Plik '{os.path.basename(path)}' nie jest rozpoznanym eksportem XTB (nazwa lub arkusze).")
    wb = openpyxl.load_workbook(path, data_only=True)
    pf = ParsedFile(path=path, name=os.path.basename(path), sha256=sha256_file(path), label=d["label"], account_no=d["account_no"], header_accounts={})

    # --- Cash Operations
    rows = _rows(wb["Cash Operations"])
    pf.header_accounts["Cash Operations"] = str(rows[0][1])
    pf.period_from, pf.period_to = rows[2][1], rows[3][1]
    hi = _find_header(rows, "Type"); hdr = rows[hi]
    for i, r in enumerate(rows[hi + 1:], start=hi + 2):
        rec = dict(zip(hdr, r)); rec["row"] = i
        if all(blank(v) for v in r):
            continue
        if rec["Type"] == "Total":
            pf.cash_total = {"row": i, "amount": rec["Amount"]}
        else:
            pf.cash.append(rec)

    # --- Closed Positions
    rows = _rows(wb["Closed Positions"])
    pf.header_accounts["Closed Positions"] = str(rows[0][1])
    hi = _find_header(rows, "Instrument"); hdr = rows[hi]
    for i, r in enumerate(rows[hi + 1:], start=hi + 2):
        rec = dict(zip(hdr, r)); rec["row"] = i
        if all(blank(v) for v in r):
            continue
        if rec["Instrument"] == "Profit/loss":
            pf.closed_footer = {"row": i, "profit_loss": rec.get("Profit/Loss"), "gross_profit": rec.get("Gross Profit")}
        else:
            pf.closed.append(rec)

    # --- Open Positions (numer konta w naglowku tego arkusza jest bledny - DQ-01; nie uzywamy go)
    rows = _rows(wb["Open Positions"])
    pf.header_accounts["Open Positions"] = str(rows[0][1])
    pf.report_generated_at = rows[2][1]
    hi = _find_header(rows, "Instrument/Position", col=1); hdr = rows[hi]
    sum_hi = _find_header(rows, "Product", col=0)
    for i, r in enumerate(rows[sum_hi + 1:hi], start=sum_hi + 2):
        if blank(r[0]):
            continue
        if r[0] == "Note":
            pf.open_notes.append({"row": i, "text": r[1]}); continue
        pf.open_summary.append({"row": i, "product": r[0], "metric": r[1], "amount": r[2], "currency": clean(r[3])})
    for i, r in enumerate(rows[hi + 1:], start=hi + 2):
        if all(blank(v) for v in r):
            continue
        rec = dict(zip(hdr, r)); rec["row"] = i
        pf.open_rows.append(rec)
    wb.close()
    if pf.header_accounts["Cash Operations"] != pf.account_no:
        pf.warnings.append(f"Numer konta w nagłówku arkusza gotówkowego ({pf.header_accounts['Cash Operations']}) różni się od numeru w nazwie pliku ({pf.account_no}).")
    return pf


def norm_product(p):
    """'Investment Plan' (liczba pojedyncza, DQ-02) -> 'Investment Plans'."""
    p = clean(p)
    return "Investment Plans" if p == "Investment Plan" else p


@dataclass
class Candidate:
    """Kandydat na transakcje ksiegi (jeszcze bez rozwiazanych identyfikatorow bazy)."""
    row: int
    source_type: str
    txn_type: str
    product: str
    occurred_at: datetime
    cash_amount: Decimal
    external_id: str
    dedup_key: str
    row_hash: str
    comment: str
    ticker: str = None
    instrument: str = None
    category: str = None
    position_ref: str = None
    subtype: str = None
    quantity: Decimal = None
    price: Decimal = None
    details: dict = field(default_factory=dict)
    unmapped_reason: str = None


def normalize_cash(rec):
    """Wiersz Cash Operations -> Candidate. Nieznany typ albo niezgodnosc znaku -> unmapped_reason (wiersz NIE trafia do ksiegi)."""
    typ = rec["Type"]
    comment = (clean(rec.get("Comment")) or "")
    product = norm_product(rec.get("Product"))
    ext_id = clean(rec.get("ID"))
    when = rec["Time"]
    amount = dec(rec["Amount"], 2)
    ticker = clean(rec.get("Ticker"))
    pid = clean(rec.get("Position ID"))
    base = dict(row=rec["row"], source_type=typ, product=product, occurred_at=as_utc(when), cash_amount=amount, external_id=ext_id,
                dedup_key=f"xtb:cash:{ext_id}", comment=comment, ticker=ticker, instrument=clean(rec.get("Instrument")),
                category=clean(rec.get("Category")), position_ref=pid)
    h = row_hash({"Type": typ, "Ticker": ticker, "Time": iso_ms(when), "Amount": canon(amount), "Comment": comment, "Product": product, "PositionID": pid})
    base["row_hash"] = h
    if typ not in CASH_TYPES:
        return Candidate(txn_type="OTHER", unmapped_reason=f"Nieznany typ operacji XTB: '{typ}'", **base)
    if not ext_id or when is None or amount is None:
        return Candidate(txn_type="OTHER", unmapped_reason="Wiersz bez ID, czasu lub kwoty", **base)
    t = CASH_TYPES[typ]; details = {}; subtype = None; qty = price = None

    if typ in ("Stock purchase", "Stock sell"):
        m = RX_TRADE.match(comment)
        if not m or (m["dir"] == "OPEN") != (typ == "Stock purchase"):
            return Candidate(txn_type=t, unmapped_reason=f"Komentarz transakcji nie pasuje do wzorca: '{comment}'", **base)
        qty, price = dec(m["vol"]), dec(m["price"])
        details = {"order_volume": m["order_vol"], "comment_ticker": m["ticker"]}
        if not ticker and m["ticker"]:
            base["ticker"] = m["ticker"]
    elif typ == "Dividend":
        m = RX_DIV.match(comment)
        if not m:
            return Candidate(txn_type=t, unmapped_reason=f"Komentarz dywidendy nie pasuje do wzorca: '{comment}'", **base)
        details = {"dividend_currency": m["ccy"], "dividend_per_share": m["dps"], "is_correction": bool(m["corr"])}
    elif typ == "Withholding tax":
        m = RX_WHT.match(comment)
        if not m:
            return Candidate(txn_type=t, unmapped_reason=f"Komentarz podatku u źródła nie pasuje do wzorca: '{comment}'", **base)
        details = {"dividend_currency": m["ccy"], "wht_rate_percent": m["rate"]}
    elif typ in ("Free funds interest", "Free funds interest tax"):
        m = RX_INTEREST.match(comment)
        if not m:
            return Candidate(txn_type=t, unmapped_reason=f"Komentarz odsetek nie pasuje do wzorca: '{comment}'", **base)
        details = {"interest_month": m["ym"]}
    elif typ in ("SEC fee", "Correction"):
        m = RX_SEC.match(comment)
        if not m:
            return Candidate(txn_type=t, unmapped_reason=f"Komentarz opłaty SEC nie pasuje do wzorca: '{comment}'", **base)
        subtype = "sec_fee"; details = {"is_correction": bool(m["corr"]), "fee_date": m["ymd"], "comment_ticker": m["ticker"]}
    elif typ == "IKE deposit":
        m = RX_IKE_TR.match(comment)
        if not m:
            return Candidate(txn_type="TRANSFER_IN", unmapped_reason=f"Komentarz przelewu IKE nie pasuje do wzorca: '{comment}'", **base)
        t = "TRANSFER_IN" if m["dir"] == "in" else "TRANSFER_OUT"; subtype = "ike_deposit"
        details = {"transfer_kind": "IKE_DEPOSIT", "transfer_direction": m["dir"], "comment_account": m["acct"]}
    elif typ == "Subaccount transfer":
        m = RX_SUB_TR.match(comment)
        if not m:
            return Candidate(txn_type="TRANSFER_IN", unmapped_reason=f"Komentarz przelewu między podkontami nie pasuje do wzorca: '{comment}'", **base)
        t = "TRANSFER_IN" if amount >= 0 else "TRANSFER_OUT"; subtype = "subaccount_transfer"
        details = {"transfer_kind": "SUBACCOUNT", "from_account": m["frm"], "to_account": m["to"]}
    elif typ in ("Deposit", "Withdrawal"):
        m = RX_PAYMENT.match(comment)
        if m:
            subtype = m["channel"].lower().replace(" ", "_")
            details = {"channel": m["channel"], "payment_direction": m["dir"], "counterparty_ref": {"scheme": "XTB_PAYMENT_BA", "value": m["ba"]},
                       "own_investment_account": m["ia"], "branch": m["br"], "cid": m["cid"]}
        else:
            m2 = RX_DEPOSIT.match(comment)
            subtype = (m2["channel"].lower().replace(" ", "_") if m2 else "other")
            details = {"channel": m2["channel"] if m2 else None}

    # znak kwoty zgodny z ograniczeniem ksiegi (ck_cash_sign); w razie niezgodnosci wiersz nie wchodzi do ksiegi, tylko do raportu
    plus, minus = {"CASH_DEPOSIT", "TRANSFER_IN", "INTEREST", "REWARD"}, {"CASH_WITHDRAWAL", "TRANSFER_OUT", "BUY", "TAX_WITHHOLDING", "TAX_INTEREST"}
    if (t in plus and amount < 0) or (t in minus and amount > 0):
        return Candidate(txn_type=t, unmapped_reason=f"Kwota {amount} ma znak niezgodny z typem {t}", **base)
    details["source_type"] = typ
    return Candidate(txn_type=t, subtype=subtype, quantity=qty, price=price, details=details, **{k: v for k, v in base.items()})


def closed_dedup_keys(records):
    """Klucz zamkniecia: xtb:closed:{pid}:{czas_zamkniecia_ms}:{ilosc 4 m.}:{cena zamkniecia 4 m.}; kolizje w pliku -> sufiks #k (dokument 7 §7.3)."""
    seen, out = {}, []
    for r in records:
        pid = str(int(r["Position ID"])) if not blank(r.get("Position ID")) else ""
        ct = as_utc(r["Close Time (UTC)"]); ms = calendar.timegm(ct.utctimetuple()) * 1000 + ct.microsecond // 1000
        key = f"xtb:closed:{pid}:{ms}:{dec(r['Volume'], 4)}:{dec(r['Close Price'], 4)}"
        seen[key] = seen.get(key, 0) + 1
        out.append(key if seen[key] == 1 else f"{key}#{seen[key]}")
    return out


def closed_row_hash(r):
    pid = str(int(r["Position ID"])) if not blank(r.get("Position ID")) else None
    return row_hash({"Ticker": clean(r.get("Ticker")), "Volume": canon(dec(r["Volume"], 4)), "OpenPrice": canon(dec(r["Open Price"])),
                     "OpenTime": iso_ms(r["Open Time (UTC)"]), "ClosePrice": canon(dec(r["Close Price"])), "CloseTime": iso_ms(r["Close Time (UTC)"]),
                     "Product": norm_product(r.get("Product")), "ProfitLoss": canon(dec(r["Profit/Loss"], 2)), "PurchaseValue": canon(dec(r["Purchase Value"], 2)),
                     "SaleValue": canon(dec(r["Sale Value"], 2)), "CloseOrigin": clean(r.get("Close Origin")), "PositionID": pid, "Comment": clean(r.get("Comment"))})
