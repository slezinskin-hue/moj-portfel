"""
Silnik importu (dokument 7 + decyzje z dokumentow 14 i 17): atomowa partia, idempotencja po dedup_key + row_hash, surowe wiersze,
konflikty, rozwiazywanie instrumentow (bez zgadywania waluty), laczenie przelewow, klasyfikacja przeplywow, wykrywanie korekt (splitow),
kontrole i raport. Jedna partia = jedna transakcja bazy (G4): blad => wycofanie calosci.
"""
import json
from collections import defaultdict
from datetime import timedelta, timezone
from decimal import Decimal

from psycopg2.extras import Json, execute_values

from . import parser as xtb
from invapp.core.common import ImportFailed, as_utc, blank, canon, clean, dec, jsonable

OUTCOMES = ("INSERTED", "DUPLICATE", "CONFLICT", "UNMAPPED", "IGNORED")
CLASSIFIER_VERSION = "import-1.0"


def J(obj):
    return Json(jsonable(obj))


class Resolver:
    """Rozwiazywanie symboli XTB -> notowania. Zasada D-INSTR-1: waluta i gielda NIE wynikaja z sufiksu; ISIN i waluta tylko ze specyfikacji
    brokera (broker_instrument_specs) ze zrodlem i data; brak dopasowania = pola puste (NIEZNANY)."""

    def __init__(self, cur, broker_id):
        self.cur, self.broker_id, self.cache, self.created, self.matched, self.unmatched, self.matched_by_rule = cur, broker_id, {}, [], [], [], []
        self.ccy_cache = {}

    def _spec(self, ticker):
        self.cur.execute("""select instrument_name, isin, currency, instrument_type, source_doc_name, source_doc_sha256, valid_from
                            from broker_instrument_specs where broker_id=%s and symbol=%s order by valid_from desc, spec_id desc limit 1""", (self.broker_id, ticker))
        return self.cur.fetchone()

    def listing(self, ticker, instrument=None, category=None):
        if blank(ticker):
            return None
        if ticker in self.cache:
            return self.cache[ticker]
        cur = self.cur
        cur.execute("select listing_id from listing_aliases where broker_id=%s and broker_symbol=%s and valid_to is null", (self.broker_id, ticker))
        row = cur.fetchone()
        if row:
            self.cache[ticker] = row[0]; return row[0]
        spec = self._spec(ticker)
        asset_class = "ETF" if category == "ETF" else "STOCK"
        name = instrument or ticker
        if spec:
            spec_name, isin, ccy, typ, doc, sha, valid_from = spec
            cur.execute("select security_id from securities where isin=%s", (isin,))
            r = cur.fetchone()
            if r:
                sec = r[0]
            else:
                cur.execute("""insert into securities(isin,isin_status,isin_evidence,name,asset_class) values (%s,'ZGODNY_SPECYFIKACJA_BROKERA',%s,%s,%s) returning security_id""",
                            (isin, J({"source": f"XTB OMI {doc}", "sha256": sha, "valid_from": str(valid_from), "spec_name": spec_name, "symbol": ticker}), name, asset_class))
                sec = cur.fetchone()[0]
            src = f"XTB OMI ({doc}) wazna od {valid_from}, sha256 {sha[:8]}"
            cur.execute("""insert into listings(security_id,display_name,trading_currency,currency_status,currency_source,currency_checked_at,currency_evidence)
                           values (%s,%s,%s,'ZGODNY_SPECYFIKACJA_BROKERA',%s,now(),%s) returning listing_id""",
                        (sec, name, ccy, src, J({"source": src, "symbol": ticker, "spec_currency": ccy})))
            lst = cur.fetchone()[0]; verified_by = "external:xtb_omi"; self.matched.append(ticker)
        else:
            cur.execute("insert into securities(name,asset_class) values (%s,%s) returning security_id", (name, asset_class))
            sec = cur.fetchone()[0]
            suffix = ticker.rsplit(".", 1)[-1] if "." in ticker else None
            cur.execute("""select value, source, confirmed_at from broker_symbol_suffix_rules where broker_id=%s and symbol_suffix=%s and attribute='TRADING_CURRENCY'""", (self.broker_id, suffix))
            rule = cur.fetchone()
            if rule:        # jawna regula UZYTKOWNIKA (np. .PL = PLN), nie zgadywanie systemu
                ccy, src, at = rule
                cur.execute("""insert into listings(security_id,display_name,trading_currency,currency_status,currency_source,currency_checked_at,currency_evidence)
                               values (%s,%s,%s,'POTWIERDZONY_PRZEZ_UZYTKOWNIKA',%s,%s,%s) returning listing_id""",
                            (sec, name, ccy, src, at, J({"rule": "broker_symbol_suffix_rules", "suffix": suffix, "source": src, "note": "ISIN i gielda niezweryfikowane"})))
                lst = cur.fetchone()[0]; verified_by = "user_rule"; self.matched_by_rule.append(ticker)
            else:
                cur.execute("insert into listings(security_id,display_name,currency_evidence) values (%s,%s,%s) returning listing_id",
                            (sec, name, J({"note": "brak wpisu w specyfikacji brokera ani reguly uzytkownika; waluta i gielda niezweryfikowane (sufiks symbolu nie jest dowodem)"})))
                lst = cur.fetchone()[0]; verified_by = "system"; self.unmatched.append(ticker)
        cur.execute("""insert into listing_aliases(broker_id,broker_symbol,listing_id,market_suffix,verified_by) values (%s,%s,%s,%s,%s)""",
                    (self.broker_id, ticker, lst, ticker.rsplit(".", 1)[-1] if "." in ticker else None, verified_by))
        self.cache[ticker] = lst; self.created.append(ticker)
        return lst

    def currency(self, listing_id):
        # pamiec podreczna na czas jednego importu: waluta notowania nie zmienia sie w trakcie (reguly i specyfikacje stosowane sa po imporcie)
        if listing_id not in self.ccy_cache:
            self.cur.execute("select trading_currency from listings where listing_id=%s", (listing_id,))
            self.ccy_cache[listing_id] = self.cur.fetchone()[0]
        return self.ccy_cache[listing_id]


class XtbImporter:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.cur.execute("select broker_id from financial_institutions where code='XTB'")
        self.broker_id = self.cur.fetchone()[0]
        self.resolver = Resolver(self.cur, self.broker_id)
        self._accounts = {}

    # ------------------------------------------------------------------ konta
    def _account_for_product(self, product, file_account_no):
        key = (product, file_account_no)                           # pamiec podreczna na czas importu (zamiast zapytania na kazdy wiersz)
        if key in self._accounts:
            return self._accounts[key]
        self.cur.execute("""select account_id from account_aliases where broker_id=%s and alias_kind='PRODUCT_LABEL' and alias_value=%s and context_account_no=%s""",
                         (self.broker_id, product, file_account_no))
        r = self.cur.fetchone()
        if not r:
            raise ImportFailed(f"Nowa etykieta produktu '{product}' w pliku konta {file_account_no}: wskaż konto (alias) przed importem – nie zgaduję.")
        self._accounts[key] = r[0]
        return r[0]

    def _file_account(self, account_no):
        self.cur.execute("select account_id from account_aliases where broker_id=%s and alias_kind='FILE_ACCOUNT_NO' and alias_value=%s", (self.broker_id, account_no))
        r = self.cur.fetchone()
        if not r:
            raise ImportFailed(f"Nieznany numer konta {account_no} z nazwy pliku: zarejestruj konto (ops\\seed_accounts.py) przed importem.")
        return r[0]

    # ------------------------------------------------------------------ import pliku
    def import_file(self, path, pf=None):
        cur = self.cur
        pf = pf or xtb.parse(path)                                 # plik juz odczytany przez wolajacego - bez ponownego czytania arkusza
        file_account = self._file_account(pf.account_no)
        cur.execute("select pg_advisory_xact_lock(hashtext('import:XTB'))")
        cur.execute("select count(*) from import_batches where source_file_sha256=%s and status='COMMITTED'", (pf.sha256,))
        same_file_before = cur.fetchone()[0]
        cur.execute("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,period_from,period_to,report_generated_at,account_id,status)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING') returning batch_id""",
                    (self.broker_id, pf.name, pf.sha256, xtb.PARSER_NAME, xtb.PARSER_VERSION, as_utc(pf.period_from), as_utc(pf.period_to),
                     as_utc(pf.report_generated_at), file_account))
        batch = cur.fetchone()[0]
        report = {"file": pf.name, "sha256": pf.sha256, "batch_id": batch, "account_no": pf.account_no, "warnings": list(pf.warnings), "info": [], "checks": [],
                  "sheets": {}, "same_file_imported_before": same_file_before}
        if same_file_before:
            report["warnings"].append("Ten plik (ta sama suma SHA-256) był już zaimportowany; gwarancja idempotencji wynika z kluczy deduplikacji.")
        counters = defaultdict(int)
        raw = []   # (sheet, row, kind, natural_key, row_hash, payload, outcome, txn_id)

        def outcome(sheet, row, kind, key, h, payload, oc, txn_id=None):
            counters[oc] += 1; counters["read"] += 1
            raw.append((batch, sheet, row, kind, key, h, Json(jsonable(payload)), oc, txn_id))
            report["sheets"].setdefault(sheet, defaultdict(int))[oc] += 1

        self._import_cash(pf, batch, file_account, report, outcome)
        self._import_closed(pf, batch, file_account, report, outcome)
        self._import_open(pf, batch, file_account, report, outcome)
        self._link_and_enrich(batch, report)
        self._validate_file(pf, file_account, report)

        execute_values(cur, """insert into raw_rows(batch_id,sheet_name,row_number,row_kind,natural_key,row_hash,payload,outcome,txn_id) values %s""", raw, page_size=500)
        report["sheets"] = {k: dict(v) for k, v in report["sheets"].items()}
        total = sum(counters[o] for o in OUTCOMES)
        if total != counters["read"]:
            raise ImportFailed(f"Kontrola G3 nie przeszła: suma wyników ({total}) ≠ liczba wierszy ({counters['read']}).")
        report["counters"] = {o: counters[o] for o in OUTCOMES}; report["counters"]["read"] = counters["read"]
        cur.execute("""update import_batches set status='COMMITTED', rows_read=%s, rows_inserted=%s, rows_duplicate=%s, rows_conflict=%s, rows_unmapped=%s, rows_ignored=%s,
                       finished_at=now(), report=%s where batch_id=%s""",
                    (counters["read"], counters["INSERTED"], counters["DUPLICATE"], counters["CONFLICT"], counters["UNMAPPED"], counters["IGNORED"], J(report), batch))
        return report

    # ------------------------------------------------------------------ Cash Operations
    def _import_cash(self, pf, batch, file_account, report, outcome):
        cur = self.cur
        cands = [(rec, xtb.normalize_cash(rec)) for rec in pf.cash]
        # operacje juz zapisane (ponowny import / nowszy eksport): jedno zapytanie na plik zamiast trzech na wiersz; wynik identyczny
        cur.execute("select dedup_key, txn_id, row_hash from transactions where dedup_key = any(%s)", ([c.dedup_key for _, c in cands if not c.unmapped_reason],))
        known = {k: (t, h) for k, t, h in cur.fetchall()}
        seen = []                                                  # DUPLICATE: last_seen_batch_id ustawiane zbiorczo po petli
        new_rows, waiting, pending = [], [], {}                    # nowe operacje: zapis jednym poleceniem, txn_id przypisywany po zapisie
        for rec, cand in cands:
            payload = {k: v for k, v in rec.items()}
            if cand.unmapped_reason:
                outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "UNMAPPED")
                report["warnings"].append(f"Cash Operations, wiersz {rec['row']}: {cand.unmapped_reason} – wiersz nie trafia do księgi.")
                continue
            account = self._account_for_product(cand.product, pf.account_no)
            listing = self.resolver.listing(cand.ticker, cand.instrument, cand.category) if cand.ticker else None
            price_ccy = fx = fx_kind = gross = None
            details = dict(cand.details)
            if cand.txn_type in ("BUY", "SELL"):
                gross = cand.quantity * cand.price
                ccy = self.resolver.currency(listing)
                eff = abs(cand.cash_amount) / gross if gross else None
                details["effective_cash_per_price_unit"] = format(eff.quantize(Decimal("0.00000001")), "f") if eff is not None else None
                if ccy:
                    price_ccy, fx, fx_kind = ccy, (eff.quantize(Decimal("0.0000000001")) if eff is not None else None), "EFFECTIVE_IMPLIED"
                else:
                    price_ccy = "XXX"          # waluta notowania niezweryfikowana (ISO 4217 XXX = brak waluty); kurs efektywny w details
            if cand.dedup_key in known:
                tid, old_hash = known[cand.dedup_key]
                if old_hash == cand.row_hash:
                    seen.append(tid)
                    outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "DUPLICATE", tid)
                else:
                    cur.execute("insert into import_conflicts(batch_id,dedup_key,existing_hash,incoming_hash,incoming_payload) values (%s,%s,%s,%s,%s)",
                                (batch, cand.dedup_key, old_hash, cand.row_hash, J(payload)))
                    outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "CONFLICT", tid)
                    report["warnings"].append(f"Cash Operations, wiersz {rec['row']}: ten sam klucz {cand.dedup_key}, inna treść – zachowano istniejący wiersz (konflikt do decyzji).")
                continue
            if cand.dedup_key in pending:                           # ten sam klucz drugi raz w tym samym pliku: duplikat albo konflikt (po zapisie pierwszego)
                waiting.append((rec, cand, payload, False))
                continue
            pending[cand.dedup_key] = cand.row_hash
            new_rows.append((account, self.broker_id, cand.txn_type, cand.subtype, cand.occurred_at, listing, cand.quantity, cand.price, price_ccy, gross, cand.cash_amount,
                             fx, fx_kind, cand.position_ref, cand.external_id, cand.dedup_key, cand.row_hash, batch, batch, cand.comment, J(details)))
            waiting.append((rec, cand, payload, True))
        ids = {}
        if new_rows:
            ids = dict(execute_values(cur, """insert into transactions(account_id,broker_id,txn_type,txn_subtype,occurred_at,listing_id,quantity,price,price_currency,gross_amount,
                                                                     cash_amount,cash_currency,fx_rate,fx_rate_kind,commission,broker_position_ref,external_id,external_id_scheme,
                                                                     dedup_key,row_hash,source_kind,first_batch_id,last_seen_batch_id,raw_comment,details)
                                              values %s on conflict (dedup_key) do nothing returning dedup_key, txn_id""", new_rows,
                                      template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLN',%s,%s,0,%s,%s,'XTB_CASH_ID',%s,%s,'BROKER_IMPORT',%s,%s,%s,%s)",
                                      page_size=500, fetch=True))
        for rec, cand, payload, first in waiting:
            tid = ids.get(cand.dedup_key)
            if tid is None:
                raise ImportFailed(f"Cash Operations, wiersz {rec['row']}: klucz {cand.dedup_key} pojawił się w trakcie importu – przerwano (spróbuj ponownie).")
            if first:
                outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "INSERTED", tid)
            elif pending[cand.dedup_key] == cand.row_hash:
                seen.append(tid)
                outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "DUPLICATE", tid)
            else:
                cur.execute("insert into import_conflicts(batch_id,dedup_key,existing_hash,incoming_hash,incoming_payload) values (%s,%s,%s,%s,%s)",
                            (batch, cand.dedup_key, pending[cand.dedup_key], cand.row_hash, J(payload)))
                outcome("Cash Operations", rec["row"], "DATA", cand.dedup_key, cand.row_hash, payload, "CONFLICT", tid)
                report["warnings"].append(f"Cash Operations, wiersz {rec['row']}: ten sam klucz {cand.dedup_key}, inna treść – zachowano istniejący wiersz (konflikt do decyzji).")
        if seen:
            cur.execute("update transactions set last_seen_batch_id=%s, updated_at=now() where txn_id = any(%s)", (batch, seen))
        if pf.cash_total is not None:
            outcome("Cash Operations", pf.cash_total["row"], "FOOTER", None, None, {"Total": pf.cash_total["amount"]}, "IGNORED")

    # ------------------------------------------------------------------ Closed Positions
    def _import_closed(self, pf, batch, file_account, report, outcome):
        cur = self.cur
        keys = xtb.closed_dedup_keys(pf.closed)
        cur.execute("select dedup_key, row_hash from broker_closed_positions where dedup_key = any(%s)", (list(keys),))
        known = dict(cur.fetchall())                               # pozycje juz zapisane: jedno zapytanie na plik
        for rec, key in zip(pf.closed, keys):
            payload = dict(rec)
            h = xtb.closed_row_hash(rec)
            product = xtb.norm_product(rec.get("Product"))
            account = self._account_for_product(product, pf.account_no)
            listing = self.resolver.listing(clean(rec["Ticker"]), clean(rec["Instrument"]), clean(rec["Category"]))
            pid = str(int(rec["Position ID"]))
            if key in known:
                if known[key] == h:
                    outcome("Closed Positions", rec["row"], "DATA", key, h, payload, "DUPLICATE")
                else:
                    cur.execute("insert into import_conflicts(batch_id,dedup_key,existing_hash,incoming_hash,incoming_payload) values (%s,%s,%s,%s,%s)",
                                (batch, key, known[key], h, J(payload)))
                    outcome("Closed Positions", rec["row"], "DATA", key, h, payload, "CONFLICT")
                    report["warnings"].append(f"Closed Positions, wiersz {rec['row']}: konflikt klucza {key}.")
                continue
            cur.execute("""insert into broker_closed_positions(account_id,listing_id,broker_position_ref,volume,open_price,close_price,opened_at,closed_at,purchase_value,sale_value,profit_loss,
                                                               open_fx_rate,close_fx_rate,close_origin,comment,dedup_key,row_hash,first_batch_id)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (dedup_key) do nothing returning bcp_id""",
                        (account, listing, pid, dec(rec["Volume"], 4), dec(rec["Open Price"]), dec(rec["Close Price"]), as_utc(rec["Open Time (UTC)"]), as_utc(rec["Close Time (UTC)"]),
                         dec(rec["Purchase Value"], 2), dec(rec["Sale Value"], 2), dec(rec["Profit/Loss"], 2), dec(rec.get("Open Conversion Rate")), dec(rec.get("Close Conversion Rate")),
                         clean(rec.get("Close Origin")), clean(rec.get("Comment")), key, h, batch))
            ins = cur.fetchone()
            if ins:
                known[key] = h
                outcome("Closed Positions", rec["row"], "DATA", key, h, payload, "INSERTED"); continue
            raise ImportFailed(f"Closed Positions, wiersz {rec['row']}: klucz {key} pojawił się w trakcie importu – przerwano (spróbuj ponownie).")
        if pf.closed_footer is not None:
            outcome("Closed Positions", pf.closed_footer["row"], "FOOTER", None, None, pf.closed_footer, "IGNORED")

    # ------------------------------------------------------------------ Open Positions (migawka)
    def _import_open(self, pf, batch, file_account, report, outcome):
        cur = self.cur
        as_of = as_utc(pf.report_generated_at)
        for s in pf.open_summary:
            outcome("Open Positions", s["row"], "SUMMARY", None, None, s, "IGNORED")
        for n in pf.open_notes:
            outcome("Open Positions", n["row"], "META", None, None, n, "IGNORED")
        for rec in pf.open_rows:
            payload = dict(rec)
            product = xtb.norm_product(rec.get("Product"))
            account = self._account_for_product(product, pf.account_no)
            ticker = clean(rec.get("Ticker"))
            is_pos = clean(rec.get("Type")) == "BUY"
            listing = self.resolver.listing(ticker, None if is_pos else clean(rec.get("Instrument/Position")), clean(rec.get("Category")))
            cur_price = dec(rec.get("Current price"))
            vol, value = dec(rec["Volume"], 4), dec(rec["Value"], 2)
            implied = (value / (vol * cur_price)).quantize(Decimal("0.0000000001")) if (is_pos and cur_price and vol) else None
            ref = str(rec["Instrument/Position"]) if is_pos else None
            h = None
            cur.execute("""insert into broker_position_snapshots(batch_id,account_id,as_of,listing_id,row_level,broker_position_ref,volume,open_price,current_price,opened_at,value_cash,
                                                                  net_profit_cash,net_profit_pct,implied_fx_rate)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning snapshot_id""",
                        (batch, account, as_of, listing, "POSITION" if is_pos else "INSTRUMENT", ref, vol, dec(rec.get("Open price")), cur_price,
                         as_utc(rec["Open time (UTC)"]) if not blank(rec.get("Open time (UTC)")) else None, value, dec(rec["Net Profit"], 2) or Decimal(0),
                         dec(rec.get("Net Profit %")), implied))
            ins = cur.fetchone()
            key = f"xtb:open:{account}:{as_of.isoformat()}:{'POSITION' if is_pos else 'INSTRUMENT'}:{ticker}:{ref or ''}"
            outcome("Open Positions", rec["row"], "DATA", key, h, payload, "INSERTED" if ins else "DUPLICATE")

    # ------------------------------------------------------------------ laczenie, wzbogacanie, klasyfikacja, korekty
    def _link_and_enrich(self, batch, report):
        self.link_wht(report)
        self.link_sec_corrections(report)
        self.group_orders(report)
        self.link_transfers(report)
        self.classify_flows(report)
        self.detect_corporate_actions(report)

    def link_wht(self, report):
        cur = self.cur
        cur.execute("""select w.txn_id, w.account_id, w.listing_id, w.broker_position_ref, w.occurred_at, w.external_id from transactions w
                       where w.broker_id=%s and w.txn_type='TAX_WITHHOLDING' and w.related_txn_id is null order by w.occurred_at""", (self.broker_id,))
        for tid, acct, lst, pid, when, ext in cur.fetchall():
            c2 = self.conn.cursor()
            c2.execute("""select txn_id, occurred_at, external_id from transactions where broker_id=%s and txn_type='DIVIDEND' and account_id=%s and listing_id=%s
                          and occurred_at <= %s and occurred_at >= %s and (%s::text is null or broker_position_ref is not distinct from %s)
                          and (details->>'is_correction') is distinct from 'true' order by occurred_at desc limit 1""",
                        (self.broker_id, acct, lst, when, when - timedelta(days=5), pid, pid))
            r = c2.fetchone()
            if r:
                gap = (when - r[1]).total_seconds() * 1000
                c2.execute("update transactions set related_txn_id=%s, details = details || %s where txn_id=%s", (r[0], J({"link_method": "wht_to_dividend", "link_gap_ms": round(gap)}), tid))
            else:
                report["warnings"].append(f"Podatek u źródła (ID {ext}) bez dywidendy do powiązania.")

    def link_sec_corrections(self, report):
        cur = self.cur
        cur.execute("""select c.txn_id, c.account_id, c.broker_position_ref, c.details->>'fee_date' from transactions c
                       where c.broker_id=%s and c.txn_type='FEE' and (c.details->>'is_correction')='true' and c.related_txn_id is null""", (self.broker_id,))
        for tid, acct, pid, fdate in cur.fetchall():
            c2 = self.conn.cursor()
            c2.execute("""select txn_id from transactions where broker_id=%s and txn_type='FEE' and account_id=%s and broker_position_ref is not distinct from %s
                          and details->>'fee_date'=%s and (details->>'is_correction') is distinct from 'true' limit 1""", (self.broker_id, acct, pid, fdate))
            r = c2.fetchone()
            if r:
                c2.execute("update transactions set related_txn_id=%s where txn_id=%s", (r[0], tid))

    def group_orders(self, report):
        """Wypelnienia jednego zlecenia (komentarz 'a/b': a = to wypelnienie, b = ilosc zlecenia). Ulamkowa i calkowita czesc zlecenia sa wykonywane po
        roznych cenach i w roznych chwilach (do kilkudziesieciu sekund), wiec cena NIE jest kluczem. Grupa: to samo konto, notowanie, kierunek i b;
        kolejne wypelnienia w odstepach do 10 minut, dopoki suma nie osiagnie b. Wynik jest funkcja samej ksiegi (liczony od nowa), wiec nie zalezy od kolejnosci importu."""
        cur = self.cur
        cur.execute("""select txn_id, account_id, listing_id, txn_type, (details->>'order_volume')::numeric, quantity, occurred_at, external_id, order_ref from transactions
                       where broker_id=%s and txn_type in ('BUY','SELL') and (details->>'order_volume') is not null
                       order by account_id, listing_id, txn_type, (details->>'order_volume')::numeric, occurred_at, external_id""", (self.broker_id,))
        group, prev_key, last_at, filled = None, None, None, Decimal(0)
        eps = Decimal("0.00005")
        for tid, acct, lst, typ, b, qty, when, ext, old in cur.fetchall():
            key = (acct, lst, typ, b)
            if key != prev_key or last_at is None or (when - last_at).total_seconds() > 600 or filled >= b - eps:
                group, filled = f"xtb:order:{ext}", Decimal(0)
            filled += qty; last_at, prev_key = when, key
            if group != old:
                self.conn.cursor().execute("update transactions set order_ref=%s where txn_id=%s", (group, tid))
    def link_transfers(self, report):
        """Parowanie nog przelewow wewnetrznych: IKE (dwa pliki; L2: rowna i przeciwna kwota, dt <= 2 s) i podkont (jeden plik; L1: komentarz wskazuje oba konta)."""
        cur = self.cur
        cur.execute("""select txn_id, account_id, txn_type, cash_amount, occurred_at, external_id, details->>'transfer_kind' from transactions
                       where broker_id=%s and txn_type in ('TRANSFER_IN','TRANSFER_OUT') and transfer_group_id is null and details->>'transfer_kind' is not null
                       order by occurred_at, txn_id""", (self.broker_id,))
        legs = cur.fetchall()
        by_kind = defaultdict(list)
        for l in legs:
            by_kind[l[6]].append(l)
        pairs_total = defaultdict(int)
        for kind, items in by_kind.items():
            outs = [l for l in items if l[2] == "TRANSFER_OUT"]; ins = [l for l in items if l[2] == "TRANSFER_IN"]
            cand_out = {o[0]: [i for i in ins if i[3] == -o[3] and i[1] != o[1] and abs((i[4] - o[4]).total_seconds()) <= 2] for o in outs}
            cand_in = defaultdict(list)
            for oid, lst in cand_out.items():
                for i in lst:
                    cand_in[i[0]].append(oid)
            for o in outs:
                c = cand_out[o[0]]
                if len(c) == 1 and len(cand_in[c[0][0]]) == 1:
                    i = c[0]
                    gid = f"xtb:xfer:{o[5]}-{i[5]}"
                    method = "L2_LEG_MATCH" if kind == "IKE_DEPOSIT" else "L1_EXPLICIT_REF"
                    cur.execute("insert into transfer_groups(transfer_group_id,status,method) values (%s,'MATCHED',%s) on conflict do nothing", (gid, method))
                    cur.execute("update transactions set transfer_group_id=%s where txn_id in (%s,%s)", (gid, o[0], i[0]))
                    for leg, other in ((o, i), (i, o)):
                        cur.execute("""insert into flow_classification(txn_id,counterparty_account_id,transfer_group_id,flow_class,method,confidence,decided_by,classifier_version)
                                       values (%s,%s,%s,'INTERNAL_TRANSFER',%s,'HIGH','rule:import',%s)""", (leg[0], other[1], gid, method, CLASSIFIER_VERSION))
                    pairs_total[kind] += 1
                elif len(c) > 1:
                    report["warnings"].append(f"Przelew {o[5]} ({kind}): niejednoznaczne parowanie nóg ({len(c)} kandydatów) – bez klasyfikacji (do decyzji).")
        cur.execute("""select count(*) from transactions where broker_id=%s and txn_type in ('TRANSFER_IN','TRANSFER_OUT') and transfer_group_id is null and details->>'transfer_kind' is not null""", (self.broker_id,))
        lone = cur.fetchone()[0]
        report["transfer_pairs"] = dict(pairs_total)
        if lone:
            report["info"].append(f"{lone} przelewów wewnętrznych bez drugiej nogi (zaimportuj drugi plik) – na razie bez klasyfikacji.")

    def classify_flows(self, report):
        cur = self.cur
        cur.execute("""select t.txn_id, t.txn_type, t.details from transactions t
                       where t.broker_id=%s and t.txn_type in ('CASH_DEPOSIT','CASH_WITHDRAWAL') and t.details->>'transfer_kind' is null
                         and not exists (select 1 from flow_classification fc where fc.txn_id=t.txn_id and fc.is_current) order by t.occurred_at, t.txn_id""", (self.broker_id,))
        counts = defaultdict(int)
        for tid, typ, details in cur.fetchall():
            ref = (details or {}).get("counterparty_ref")
            c2 = self.conn.cursor()
            if ref:
                c2.execute("select counterparty_id, ownership_status, mapped_account_id from counterparties where scheme=%s and value=%s", (ref["scheme"], ref["value"]))
                cp = c2.fetchone()
                if not cp:
                    c2.execute("""insert into counterparties(scheme,value,ownership_status,evidence) values (%s,%s,'OWN_CANDIDATE',%s) returning counterparty_id, ownership_status, mapped_account_id""",
                               (ref["scheme"], ref["value"], J({"proposed_by": "rule:import", "channel": details.get("channel")})))
                    cp = c2.fetchone()
                cid, status, mapped = cp
                if status == "OWN_CONFIRMED" and mapped:
                    cls, acct, method, conf, by = "OWN_TRANSFER_UNTRACKED", mapped, "L3_COUNTERPARTY_REGISTRY", "HIGH", "user (rejestr kontrahentów)"
                elif status == "EXTERNAL_CONFIRMED":
                    cls, acct, method, conf, by = "EXTERNAL", None, "L3_COUNTERPARTY_REGISTRY", "HIGH", "user (rejestr kontrahentów)"
                else:
                    cls, acct, method, conf, by = "UNCLASSIFIED", None, "DEFAULT", "LOW", "rule:import"
                c2.execute("""insert into flow_classification(txn_id,counterparty_id,counterparty_account_id,flow_class,method,confidence,decided_by,classifier_version)
                              values (%s,%s,%s,%s,%s,%s,%s,%s)""", (tid, cid, acct, cls, method, conf, by, CLASSIFIER_VERSION))
            else:
                cls = "EXTERNAL"
                c2.execute("""insert into flow_classification(txn_id,flow_class,method,confidence,decided_by,classifier_version) values (%s,'EXTERNAL','DEFAULT','MEDIUM','rule:import',%s)""",
                           (tid, CLASSIFIER_VERSION))
            counts[cls] += 1
        report["flow_classes"] = dict(counts)

    def detect_corporate_actions(self, report):
        """Split wykonany przez brokera: wzorzec (Correction/STC Transfer Out + nowe pozycje bez zakupu) -> korekta CONFIRMED (BROKER_ARTIFACT, zrodlo oficjalne: niesprawdzone).
        Split niewidoczny w eksporcie: wzorzec niejawny (ilosc zamknieta = k x ilosc kupiona) -> korekta HYPOTHESIS, NIESTOSOWANA (dokument 14 §14.2.3)."""
        cur = self.cur
        # --- (a) artefakty brokera
        cur.execute("""select bcp_id, account_id, listing_id, broker_position_ref, volume, closed_at, purchase_value from broker_closed_positions
                       where close_origin='Correction' and comment='STC Transfer Out' and corporate_action_id is null order by listing_id, closed_at, bcp_id""")
        old_rows = cur.fetchall(); by_listing = defaultdict(list)
        for r in old_rows:
            by_listing[(r[1], r[2])].append(r)
        for (acct, lst), olds in by_listing.items():
            cur.execute("""select max(as_of) from broker_position_snapshots where account_id=%s and row_level='POSITION'""", (acct,))
            as_of = cur.fetchone()[0]
            cur.execute("""select s.broker_position_ref, s.volume, s.open_price, s.value_cash, s.net_profit_cash from broker_position_snapshots s
                           where s.account_id=%s and s.listing_id=%s and s.row_level='POSITION' and s.as_of=%s
                             and not exists (select 1 from transactions t where t.txn_type='BUY' and t.account_id=s.account_id and t.broker_position_ref=s.broker_position_ref)
                           order by s.broker_position_ref""", (acct, lst, as_of))
            news = cur.fetchall(); used, pairs, ratios = set(), [], set()
            for o in olds:
                match = None
                for k in range(2, 101):
                    cands = [n for n in news if n[0] not in used and abs(n[1] - o[4] * k) <= Decimal("0.00015")]
                    if len(cands) == 1:
                        match = (cands[0], k); break
                if not match:
                    pairs = None; break
                used.add(match[0][0]); pairs.append((o, match[0])); ratios.add(match[1])
            if not pairs or len(ratios) != 1 or len(pairs) != len(olds):
                report["warnings"].append(f"Wykryto zamknięcia 'Correction' dla notowania {lst}, ale nie udało się jednoznacznie sparować ich z nowymi pozycjami – bez korekty (do weryfikacji).")
                continue
            k = ratios.pop()
            missing = [str(o[3]) for o in olds if not self._buy_exists(acct, o[3])]
            if missing:
                report["info"].append(f"Korekta splitu dla notowania {lst} odroczona: w księdze brak zakupów pozycji {missing} (zaimportuj pełny plik gotówkowy); zostanie wykryta przy kolejnym imporcie.")
                continue
            first_closed = min(o[5] for o in olds)
            cur.execute("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,ratio_new,ratio_old,source,status,evidence)
                           values (%s,'SPLIT',%s,'FROM_BROKER_DATA',%s,1,'BROKER_ARTIFACT','CONFIRMED',%s) returning action_id""",
                        (lst, first_closed.astimezone(timezone.utc).date(), k,
                         J({"pattern": "Correction/STC Transfer Out + nowe pozycje bez zakupu", "pairs": len(pairs), "ratio": k, "closed_positions": [str(o[3]) for o in olds],
                            "new_positions": [str(p[1][0]) for p in pairs],
                            "note": "Data = dzień wykonania korekty przez XTB (dane brokera); data efektywna rynku wg źródeł wtórnych: 2024-06-10; komunikat oficjalny niesprawdzony."})))
            aid = cur.fetchone()[0]
            for o, n in pairs:
                cur.execute("""select quantity, price, -cash_amount from transactions where txn_type='BUY' and account_id=%s and broker_position_ref=%s order by occurred_at limit 1""", (acct, o[3]))
                bq, bp, cash_cost = cur.fetchone()
                new_cost = (n[3] - n[4]).quantize(Decimal("0.01"))
                diff = new_cost - cash_cost
                implied = (new_cost / (n[1] * n[2])).quantize(Decimal("0.0000000001")) if n[2] else None
                reason = "BROKER_FX_REVALUATION_AT_SPLIT" if diff != 0 else None
                cur.execute("""insert into corporate_action_position_map(action_id,account_id,old_position_ref,new_position_ref,old_quantity,new_quantity,old_price,new_price,
                                                                         old_cost_cash,new_cost_cash_broker,cost_diff_cash,broker_implied_fx,reason_code)
                               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (aid, acct, o[3], n[0], bq, n[1], bp, n[2], cash_cost, new_cost, diff, implied, reason))
                cur.execute("update broker_closed_positions set corporate_action_id=%s where bcp_id=%s", (aid, o[0]))
            cur.execute("select coalesce(sum(cost_diff_cash),0), count(*) from corporate_action_position_map where action_id=%s", (aid,))
            total_diff, n_pairs = cur.fetchone()
            cur.execute("""insert into data_quality_issues(batch_id,code,severity,entity_ref,message,details) values (%s,'DQ-05_SPLIT_ARTIFACT','WARNING',%s,%s,%s)""",
                        (None, f"listing:{lst}", f"Split {k}:1 wykonany przez XTB jako zamknięcie i ponowne otwarcie {n_pairs} pozycji; koszt nowych pozycji różni się od gotówki o {total_diff} zł (BROKER_FX_REVALUATION_AT_SPLIT).",
                         J({"action_id": aid, "cost_diff_cash": total_diff})))
            report["info"].append(f"Korekta splitu {k}:1 (notowanie {lst}), status CONFIRMED (artefakt brokera), {n_pairs} par, różnica kosztu {total_diff} zł.")
        # --- (b) split niejawny: HYPOTHESIS
        cur.execute("""select b.listing_id, b.account_id, b.broker_position_ref, b.volume, b.closed_at,
                              (select sum(t.quantity) from transactions t where t.txn_type='BUY' and t.account_id=b.account_id and t.broker_position_ref=b.broker_position_ref) as bought
                       from broker_closed_positions b where b.close_origin is distinct from 'Correction'""")
        ev = defaultdict(list)
        for lst, acct, pid, vol, closed, bought in cur.fetchall():
            if bought and bought > 0:
                ratio = vol / bought
                if ratio >= 2 and abs(ratio - ratio.to_integral_value()) < Decimal("0.0001"):
                    ev[lst].append((int(ratio.to_integral_value()), acct, pid, vol, bought, closed))
        for lst, items in ev.items():
            ks = {i[0] for i in items}
            if len(ks) != 1:
                report["warnings"].append(f"Notowanie {lst}: niespójne ilorazy ilości zamkniętej do kupionej {sorted(ks)} – bez hipotezy splitu (do weryfikacji).")
                continue
            k = ks.pop(); cutoff = min(i[5] for i in items)
            cur.execute("select 1 from corporate_actions where listing_id=%s and action_type='SPLIT' and ratio_new=%s and ratio_old=1", (lst, k))
            if cur.fetchone():           # korekta o tym wspolczynniku juz istnieje (dowolny status, takze CONFIRMED z zrodla oficjalnego lub REJECTED): nie odtwarzamy hipotezy
                continue
            cur.execute("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,applies_to_lots_opened_before,ratio_new,ratio_old,source,status,evidence)
                           values (%s,'SPLIT',NULL,'UNKNOWN',%s,%s,1,'INFERRED','HYPOTHESIS',%s) returning action_id""",
                        (lst, cutoff, k, J({"pattern": "ilość zamknięta = k × ilość kupiona (wg komentarzy księgi)", "ratio": k,
                                            "positions": [{"position": str(i[2]), "closed_volume": i[3], "bought_quantity": i[4]} for i in items],
                                            "official_source": "niesprawdzone (decyzja użytkownika 2026-11-11: hipoteza, niestosowana)"})))
            aid = cur.fetchone()[0]
            cur.execute("""insert into data_quality_issues(batch_id,code,severity,entity_ref,message,details) values (%s,'DQ-06_IMPLICIT_SPLIT','WARNING',%s,%s,%s)""",
                        (None, f"listing:{lst}", f"Niejawny split {k}:1 (hipoteza, NIESTOSOWANA): ilość zamknięta = {k} × ilość kupiona; surowa księga daje ujemny stan.", J({"action_id": aid})))
            report["warnings"].append(f"Notowanie {lst}: hipoteza splitu {k}:1 (status HYPOTHESIS, niestosowana; niepotwierdzona oficjalnym źródłem).")

    def _buy_exists(self, acct, pid):
        c = self.conn.cursor()
        c.execute("select 1 from transactions where txn_type='BUY' and account_id=%s and broker_position_ref=%s limit 1", (acct, pid))
        return c.fetchone() is not None

    # ------------------------------------------------------------------ kontrole pliku
    def _validate_file(self, pf, file_account, report):
        cur = self.cur
        chk = report["checks"]

        def check(name, expected, actual, tol=Decimal("0.011"), level="WARNING", note=""):
            ok = expected is not None and abs(Decimal(str(expected)) - Decimal(str(actual))) <= tol
            chk.append({"kontrola": name, "oczekiwane": None if expected is None else format(Decimal(str(expected)).quantize(Decimal('0.0001')), 'f'),
                        "uzyskane": format(Decimal(str(actual)).quantize(Decimal('0.0001')), 'f'), "status": "OK" if ok else level, "uwagi": note})
            if not ok:
                report["warnings"].append(f"Kontrola nieudana: {name}: oczekiwano {expected}, uzyskano {actual}.")

        if pf.cash_total is not None and pf.cash_total["amount"] is not None:
            check("Saldo gotówki (wiersz Total) = suma Amount z pliku", dec(pf.cash_total["amount"], 2), sum((dec(r["Amount"], 2) for r in pf.cash), Decimal(0)))
        by_product = defaultdict(lambda: Decimal(0))
        for s in pf.open_summary:
            by_product[(xtb.norm_product(s["product"]), s["metric"])] += dec(s["amount"], 2)
        pos_val, pos_pl = defaultdict(lambda: Decimal(0)), defaultdict(lambda: Decimal(0))
        for r in pf.open_rows:
            if clean(r.get("Type")) == "BUY":
                p = xtb.norm_product(r["Product"]); pos_val[p] += dec(r["Value"], 2); pos_pl[p] += dec(r["Net Profit"], 2)
        for (p, metric), amt in by_product.items():
            if metric == "Open position value":
                check(f"Wartość otwartych pozycji: podsumowanie = suma pozycji | {p}", amt, pos_val[p])
            elif metric == "Open position profit":
                check(f"Niezrealizowany wynik: podsumowanie = suma pozycji | {p}", amt, pos_pl[p])
        if pf.closed_footer and pf.closed_footer["profit_loss"] is not None:
            check("Zrealizowany wynik: stopka Profit/loss = suma wierszy", dec(pf.closed_footer["profit_loss"], 2), sum((dec(r["Profit/Loss"], 2) for r in pf.closed), Decimal(0)))
