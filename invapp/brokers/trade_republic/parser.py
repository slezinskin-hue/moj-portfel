"""
Adapter Trade Republic (CSV, dokument 11): rozpoznanie pliku, parsowanie, normalizacja do kandydatow na transakcje.
Plik jest TYLKO CZYTANY. Nic tu nie zapisuje do bazy (zapisem zajmuje sie tr_engine.py).
Prywatnosc: imie/nazwisko kontrahenta nie jest nigdzie zapisywane, a IBAN tylko jako skrot SHA-256 i maska (PL12…3629), takze w opisach i surowych wierszach.
Zasada: nieznany typ operacji albo waluta inna niz PLN = UNMAPPED (nie trafia do ksiegi), nigdy zgadywanie.
"""
import csv, hashlib, io, os, re, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from invapp.core.common import ImportFailed, canon, clean, dec, iso_ms, mask_identifier, row_hash

PARSER_NAME, PARSER_VERSION = "tr_csv", "1.0"
COLUMNS = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol", "shares", "price", "amount", "fee", "tax", "currency",
           "original_amount", "original_currency", "fx_rate", "description", "transaction_id", "counterparty_name", "counterparty_iban", "payment_reference", "mcc_code"]
IBAN_RX = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
ISIN_RX = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
TYPES = {"CUSTOMER_INPAYMENT": "CASH_DEPOSIT", "VIBAN_TRANSFER_INBOUND": "CASH_DEPOSIT", "VIBAN_TRANSFER_OUTBOUND": "CASH_WITHDRAWAL",
         "BUY": "BUY", "SELL": "SELL", "STOCKPERK": "REWARD", "INTEREST_PAYMENT": "INTEREST"}


@dataclass
class ParsedFile:
    path: str
    name: str
    sha256: str
    records: list = field(default_factory=list)       # {row, ...kolumny jako teksty}
    account_types: set = field(default_factory=set)
    period_from: datetime = None
    period_to: datetime = None
    warnings: list = field(default_factory=list)


@dataclass
class Candidate:
    dedup_key: str
    row_hash: str
    row: int
    txn_type: str = None
    subtype: str = None
    occurred_at: datetime = None
    account_type: str = "DEFAULT"
    isin: str = None
    name: str = None
    asset_class: str = None
    quantity: Decimal = None
    price: Decimal = None
    gross: Decimal = None
    cash_amount: Decimal = None
    commission: Decimal = Decimal(0)
    external_id: str = None
    comment: str = None
    details: dict = field(default_factory=dict)
    unmapped_reason: str = None
    children: list = field(default_factory=list)        # potomne wiersze ksiegi (podatek od odsetek)
    payload: dict = field(default_factory=dict)         # zanonimizowany surowy wiersz


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mask_text(text):
    """Maskuje IBAN-y w tekscie (opisy transakcji)."""
    return IBAN_RX.sub(lambda m: mask_identifier(m.group(0)), text) if text else text


def iban_info(iban):
    iban = clean(iban)
    if not iban:
        return None
    iban = iban.replace(" ", "").upper()
    return {"hash": hashlib.sha256(iban.encode("utf-8")).hexdigest(), "mask": mask_identifier(iban)}


def parse_time(s):
    s = clean(s)
    if not s or not s.endswith("Z"):
        raise ImportFailed("Kolumna datetime ma nieoczekiwany format (oczekiwano znacznika UTC z sufiksem Z). Zaimportuj oryginalny plik z Trade Republic, nie zapisany ponownie w Excelu.")
    s = s[:-1]
    if "." in s:
        base, frac = s.split(".", 1)
        s = f"{base}.{(frac + '000000')[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def detect(path):
    """Czy to plik Trade Republic (CSV z naglowkiem 23 kolumn)? Bez zapisu."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            head = f.readline()
    except (OSError, UnicodeDecodeError):
        return False
    return [c.strip().strip('"') for c in head.strip().split(",")] == COLUMNS


def parse(path):
    name = os.path.basename(path)
    sha = sha256_file(path)
    try:
        text = open(path, "r", encoding="utf-8-sig", newline="").read()
    except UnicodeDecodeError as e:
        raise ImportFailed("Plik nie jest w kodowaniu UTF-8. Zaimportuj oryginalny plik z Trade Republic, nie zapisany ponownie w Excelu.") from e
    first = text.splitlines()[0] if text.strip() else ""
    if ";" in first and "," not in first:
        raise ImportFailed("Plik wygląda na zapisany ponownie w Excelu (separator średnik). Zaimportuj oryginalny plik CSV z Trade Republic.")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as e:
        raise ImportFailed("Plik CSV jest pusty.") from e
    if [h.strip() for h in header] != COLUMNS:
        raise ImportFailed("To nie jest eksport transakcji z Trade Republic (inny układ kolumn). Nie zgaduję formatu.")
    pf = ParsedFile(path=path, name=name, sha256=sha)
    for i, row in enumerate(reader, start=2):
        if not row or all(c.strip() == "" for c in row):
            continue
        if len(row) != len(COLUMNS):
            raise ImportFailed(f"Wiersz {i}: {len(row)} pól zamiast {len(COLUMNS)} – plik jest uszkodzony.")
        rec = dict(zip(COLUMNS, row)); rec["row"] = i
        pf.records.append(rec)
        pf.account_types.add(clean(rec["account_type"]) or "DEFAULT")
    times = [parse_time(r["datetime"]) for r in pf.records]
    if times:
        pf.period_from, pf.period_to = min(times), max(times)
    return pf


def _tid(rec, when):
    raw = clean(rec["transaction_id"])
    try:
        return str(uuid.UUID(raw)).lower() if raw else None
    except ValueError:
        return None


def normalize(rec):
    """Wiersz CSV -> Candidate (z ewentualnymi potomkami). Wiersze niezrozumiale maja unmapped_reason."""
    when = parse_time(rec["datetime"])
    typ = clean(rec["type"]) or ""
    amount, fee, tax = dec(rec["amount"]) or Decimal(0), dec(rec["fee"]) or Decimal(0), dec(rec["tax"]) or Decimal(0)
    shares, price = dec(rec["shares"]), dec(rec["price"])
    isin, iban = clean(rec["symbol"]), iban_info(rec["counterparty_iban"])
    desc = mask_text(clean(rec["description"]))
    if typ in ("VIBAN_TRANSFER_INBOUND", "VIBAN_TRANSFER_OUTBOUND"):      # opis zawiera imie i nazwisko kontrahenta - nie zapisujemy go (tylko rodzaj i maska IBAN)
        desc = ("Przelew przychodzący" if typ.endswith("INBOUND") else "Przelew wychodzący") + (f" (rachunek {iban['mask']})" if iban else "")
    tid = _tid(rec, when)
    fields = {"dt": iso_ms(when), "type": typ, "symbol": isin, "shares": canon(shares), "price": canon(price), "amount": canon(amount), "fee": canon(fee), "tax": canon(tax),
              "currency": clean(rec["currency"]), "desc": clean(rec["description"]), "iban": iban["hash"] if iban else None, "name": clean(rec["name"]) if clean(rec["asset_class"]) else None}
    h = row_hash(fields)
    if tid:
        key = f"tr:txn:{tid}"
    else:                                   # rezerwowy klucz (nie zaobserwowany: pusty/nieprawidlowy UUID) - tresc wiersza
        key = "tr:hash:" + h
    payload = {c: rec[c] for c in COLUMNS if c not in ("counterparty_name", "counterparty_iban", "description")}
    payload["description"] = desc
    if not clean(rec["asset_class"]):                                    # dla przelewow kolumna name to osoba (kontrahent), nie instrument
        payload["name"] = None
    payload["counterparty_iban"] = iban["mask"] if iban else None
    payload["counterparty_iban_sha256"] = iban["hash"] if iban else None
    payload["counterparty_name"] = "[ukryte]" if clean(rec["counterparty_name"]) else None
    c = Candidate(dedup_key=key, row_hash=h, row=rec["row"], occurred_at=when, account_type=clean(rec["account_type"]) or "DEFAULT", isin=isin, name=clean(rec["name"]),
                  asset_class=clean(rec["asset_class"]), external_id=tid, comment=desc, payload=payload)
    if tid is None:
        c.details["warning"] = "brak prawidlowego UUID transaction_id - klucz z tresci wiersza"
    if clean(rec["currency"]) != "PLN":
        c.unmapped_reason = f"waluta {clean(rec['currency'])!r} inna niż PLN – nieobsługiwana (nie zgaduję przeliczenia)"
        return c
    if any(clean(rec[k]) for k in ("original_amount", "original_currency", "fx_rate")):
        c.details["original"] = {k: clean(rec[k]) for k in ("original_amount", "original_currency", "fx_rate")}
    if typ not in TYPES:
        c.unmapped_reason = f"nieznany typ operacji {typ!r} – wiersz nie trafia do księgi"
        return c
    c.txn_type = TYPES[typ]
    c.details["tr_type"] = typ
    if typ in ("BUY", "SELL"):
        if not (isin and ISIN_RX.match(isin)) or shares is None or price is None:
            c.unmapped_reason = "transakcja bez ISIN, ilości lub ceny"; return c
        if (typ == "BUY") != (shares > 0) or (typ == "BUY" and amount > 0) or (typ == "SELL" and amount < 0):
            c.unmapped_reason = "niespójny znak ilości lub kwoty"; return c
        c.quantity, c.price, c.gross = abs(shares), price, abs(amount)
        c.commission = abs(fee) if fee else Decimal(0)
        c.cash_amount = amount + fee                           # pelny wplyw na gotowke, z oplata wbudowana w wiersz
        c.details["fee_separate"] = format(fee, "f")
        diff = abs(c.quantity * price - c.gross)
        c.details["amount_vs_qty_price_diff"] = format(diff.quantize(Decimal("0.00000001")), "f")
    elif typ == "STOCKPERK":
        if not (isin and ISIN_RX.match(isin)) or amount <= 0:
            c.unmapped_reason = "nagroda bez ISIN albo z nieprawidłową kwotą"; return c
        c.cash_amount = amount; c.subtype = "stockperk"
    elif typ == "INTEREST_PAYMENT":
        if amount < 0 or fee:
            c.unmapped_reason = "odsetki z ujemną kwotą lub opłatą – nie zgaduję"; return c
        c.cash_amount = amount
        if tax:
            if tax > 0:
                c.unmapped_reason = "podatek od odsetek ze znakiem dodatnim"; return c
            payload_child = dict(payload)
            c.children.append(Candidate(dedup_key=key + ":tax", row_hash=row_hash({**fields, "child": "tax"}), row=rec["row"], txn_type="TAX_INTEREST", occurred_at=when,
                                        account_type=c.account_type, cash_amount=tax, external_id=(tid + ":tax") if tid else None, comment=desc,
                                        details={"tr_type": typ, "derived_from": key}, payload=payload_child))
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", clean(rec["description"]) or "")
        if m:
            c.details["payout_collection_id"] = m.group(1)
    else:                                                       # wplaty i wyplaty
        if fee or tax:
            c.unmapped_reason = "przelew z opłatą lub podatkiem – nie zgaduję"; return c
        if (c.txn_type == "CASH_DEPOSIT" and amount <= 0) or (c.txn_type == "CASH_WITHDRAWAL" and amount >= 0):
            c.unmapped_reason = "niespójny znak kwoty przelewu"; return c
        c.cash_amount = amount
        if iban:
            c.details["counterparty_iban"] = iban["mask"]; c.details["counterparty_iban_sha256"] = iban["hash"]
        elif typ == "CUSTOMER_INPAYMENT":
            c.details["channel"] = clean(rec["description"])
    return c
