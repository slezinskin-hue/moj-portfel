"""
Silnik importu Trade Republic (CSV): ta sama gwarancja co dla XTB - atomowa partia, idempotencja po dedup_key (UUID wiersza) + row_hash, surowe wiersze,
konflikty, bez zgadywania. Instrumenty po ISIN (z eksportu brokera); waluta i gielda notowania zostaja NIEZNANE, dopoki nie ustali ich zrodlo zewnetrzne.
Przeplywy gotowkowe: klasa EXTERNAL (domyslnie) - kontrahent (IBAN) jest zapisany tylko jako skrot i maska.
"""
from collections import defaultdict
from decimal import Decimal
from datetime import timedelta

from psycopg2.extras import Json, execute_values

from . import parser as tr
from invapp.core.common import ImportFailed, as_utc, jsonable

OUTCOMES = ("INSERTED", "DUPLICATE", "CONFLICT", "UNMAPPED", "IGNORED")
CLASSIFIER_VERSION = "import-tr-1.0"
SCOPES = ("BROKER:TR", "INVESTMENT_ALL", "NET_WORTH")


def J(obj):
    return Json(jsonable(obj))


def ensure_account(cur, broker_id, account_type):
    """Konto Trade Republic (plik nie ma numeru rachunku): jedno konto na wartosc account_type z pliku. Zwraca (account_id, utworzone?)."""
    ext = f"TR-{account_type}"
    cur.execute("select account_id from accounts where broker_id=%s and external_no=%s", (broker_id, ext))
    r = cur.fetchone()
    if r:
        return r[0], False
    display = "Trade Republic" if account_type == "DEFAULT" else f"Trade Republic ({account_type})"
    cur.execute("""insert into accounts(broker_id,external_no,external_no_source,display_name,product_type,account_class,tracking_mode,broker_product_label,tax_regime)
                   values (%s,%s,'FILE',%s,'STANDARD','INVESTMENT','FULL',%s,'PIT38') returning account_id""", (broker_id, ext, display, account_type))
    acct = cur.fetchone()[0]
    for code in SCOPES:
        cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = %s on conflict do nothing", (acct, code))
    return acct, True


class IsinResolver:
    """ISIN z eksportu brokera -> notowanie. Nowe notowanie ma NIEZNANA walute i gielde (D-INSTR-1); papier jest wspolny z innymi brokerami (ten sam ISIN)."""

    def __init__(self, cur, broker_id):
        self.cur, self.broker_id, self.cache, self.created = cur, broker_id, {}, []

    def listing(self, isin, name, asset_class):
        if isin in self.cache:
            return self.cache[isin]
        cur = self.cur
        cur.execute("select listing_id from listing_aliases where broker_id=%s and broker_symbol=%s and valid_to is null", (self.broker_id, isin))
        r = cur.fetchone()
        if r:
            self.cache[isin] = r[0]; return r[0]
        cur.execute("select security_id from securities where isin=%s", (isin,))
        r = cur.fetchone()
        if r:
            sec = r[0]
        else:
            cur.execute("""insert into securities(isin,isin_status,isin_evidence,name,asset_class) values (%s,'Z_EKSPORTU_BROKERA',%s,%s,%s) returning security_id""",
                        (isin, J({"source": "eksport Trade Republic (kolumna symbol)"}), name or isin, "ETF" if asset_class == "ETF" else "STOCK"))
            sec = cur.fetchone()[0]
        cur.execute("insert into listings(security_id,display_name,currency_evidence) values (%s,%s,%s) returning listing_id",
                    (sec, name or isin, J({"note": "Trade Republic podaje ISIN, ale nie giełdę ani walutę notowania; do ustalenia ze źródła zewnętrznego"})))
        lst = cur.fetchone()[0]
        cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id,verified_by) values (%s,%s,%s,'external:tr_export_isin')", (self.broker_id, isin, lst))
        self.cache[isin] = lst; self.created.append(isin)
        return lst


class TrImporter:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.cur.execute("select broker_id from financial_institutions where code='TR'")
        self.broker_id = self.cur.fetchone()[0]
        self.resolver = IsinResolver(self.cur, self.broker_id)

    def import_file(self, path):
        cur = self.cur
        pf = tr.parse(path)
        cur.execute("select pg_advisory_xact_lock(hashtext('import:TR'))")
        accounts, created = {}, []
        for at in sorted(pf.account_types):
            acct, new = ensure_account(cur, self.broker_id, at)
            accounts[at] = acct
            if new:
                created.append(f"Trade Republic{'' if at == 'DEFAULT' else ' (' + at + ')'}")
        main = accounts[sorted(accounts)[0]] if accounts else None
        cur.execute("select count(*) from import_batches where source_file_sha256=%s and status='COMMITTED'", (pf.sha256,))
        same_before = cur.fetchone()[0]
        cur.execute("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,period_from,period_to,account_id,status)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING') returning batch_id""",
                    (self.broker_id, pf.name, pf.sha256, tr.PARSER_NAME, tr.PARSER_VERSION, as_utc(pf.period_from), as_utc(pf.period_to), main))
        batch = cur.fetchone()[0]
        report = {"file": pf.name, "sha256": pf.sha256, "batch_id": batch, "warnings": list(pf.warnings), "info": [], "checks": [], "created_accounts": created,
                  "same_file_imported_before": same_before, "broker": "TR"}
        if same_before:
            report["warnings"].append("Ten plik (ta sama suma SHA-256) był już zaimportowany; gwarancja idempotencji wynika z kluczy deduplikacji.")
        counters, raw = defaultdict(int), []

        def outcome(row, key, h, payload, oc, txn_id=None):
            counters[oc] += 1; counters["read"] += 1
            raw.append((batch, "CSV", row, "DATA", key, h, J(payload), oc, txn_id))

        for rec in pf.records:
            cand = tr.normalize(rec)
            if cand.unmapped_reason:
                outcome(cand.row, cand.dedup_key, cand.row_hash, cand.payload, "UNMAPPED")
                report["warnings"].append(f"Wiersz {cand.row}: {cand.unmapped_reason}.")
                continue
            tid = self._insert(cand, accounts[cand.account_type], batch, report)
            outcome(cand.row, cand.dedup_key, cand.row_hash, cand.payload, tid[1], tid[0])
            for ch in cand.children:
                t2 = self._insert(ch, accounts[ch.account_type], batch, report)
                counters[t2[1]] += 1; counters["read"] += 1
                raw.append((batch, "CSV (podatek z wiersza)", cand.row, "DATA", ch.dedup_key, ch.row_hash, J(ch.payload), t2[1], t2[0]))
        self._link_rewards(report)
        self._classify_flows(report)
        self._validate(pf, report, accounts)
        execute_values(cur, "insert into raw_rows(batch_id,sheet_name,row_number,row_kind,natural_key,row_hash,payload,outcome,txn_id) values %s", raw, page_size=500)
        total = sum(counters[o] for o in OUTCOMES)
        if total != counters["read"]:
            raise ImportFailed(f"Kontrola G3 nie przeszła: suma wyników ({total}) ≠ liczba wierszy ({counters['read']}).")
        report["counters"] = {o: counters[o] for o in OUTCOMES}; report["counters"]["read"] = counters["read"]
        cur.execute("""update import_batches set status='COMMITTED', rows_read=%s, rows_inserted=%s, rows_duplicate=%s, rows_conflict=%s, rows_unmapped=%s, rows_ignored=%s,
                       finished_at=now(), report=%s where batch_id=%s""",
                    (counters["read"], counters["INSERTED"], counters["DUPLICATE"], counters["CONFLICT"], counters["UNMAPPED"], counters["IGNORED"], J(report), batch))
        return report

    def _insert(self, c, account, batch, report):
        cur = self.cur
        listing = self.resolver.listing(c.isin, c.name, c.asset_class) if c.isin else None
        price_ccy = "PLN" if c.txn_type in ("BUY", "SELL") else None          # cena w PLN wynika wprost z kolumny currency pliku
        cur.execute("""insert into transactions(account_id,broker_id,txn_type,txn_subtype,occurred_at,listing_id,quantity,price,price_currency,gross_amount,cash_amount,cash_currency,
                                                commission,external_id,external_id_scheme,dedup_key,row_hash,source_kind,first_batch_id,last_seen_batch_id,raw_comment,details)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLN',%s,%s,'TR_TXN_UUID',%s,%s,'BROKER_IMPORT',%s,%s,%s,%s)
                       on conflict (dedup_key) do nothing returning txn_id""",
                    (account, self.broker_id, c.txn_type, c.subtype, c.occurred_at, listing, c.quantity, c.price, price_ccy, c.gross, c.cash_amount, c.commission,
                     c.external_id, c.dedup_key, c.row_hash, batch, batch, c.comment, J(c.details)))
        ins = cur.fetchone()
        if ins:
            return ins[0], "INSERTED"
        cur.execute("select txn_id,row_hash from transactions where dedup_key=%s", (c.dedup_key,))
        tid, old = cur.fetchone()
        if old == c.row_hash:
            cur.execute("update transactions set last_seen_batch_id=%s, updated_at=now() where txn_id=%s", (batch, tid))
            return tid, "DUPLICATE"
        cur.execute("insert into import_conflicts(batch_id,dedup_key,existing_hash,incoming_hash,incoming_payload) values (%s,%s,%s,%s,%s)", (batch, c.dedup_key, old, c.row_hash, J(c.payload)))
        report["warnings"].append(f"Wiersz {c.row}: ten sam klucz {c.dedup_key}, inna treść – zachowano istniejący wiersz (konflikt do decyzji).")
        return tid, "CONFLICT"

    def _link_rewards(self, report):
        """Stockperk: wplyw gotowkowy chwile przed zakupem akcji za te sama kwote (ten sam ISIN, < 1 s) - powiazanie nagrody z zakupem."""
        cur = self.cur
        cur.execute("select txn_id, account_id, listing_id, cash_amount, occurred_at from transactions where broker_id=%s and txn_type='REWARD' and related_txn_id is null", (self.broker_id,))
        for tid, acct, lst, amount, when in cur.fetchall():
            c2 = self.conn.cursor()
            c2.execute("""select txn_id from transactions where broker_id=%s and txn_type='BUY' and account_id=%s and listing_id=%s and gross_amount=%s
                          and abs(extract(epoch from (occurred_at - %s))) < 1 limit 1""", (self.broker_id, acct, lst, amount, when))
            r = c2.fetchone()
            if r:
                c2.execute("update transactions set related_txn_id=%s where txn_id=%s", (r[0], tid))
            else:
                report["warnings"].append(f"Nagroda (Stockperk) {amount} zł bez zakupu o tej samej kwocie – bez powiązania.")

    def _classify_flows(self, report):
        cur = self.cur
        cur.execute("""select t.txn_id from transactions t where t.broker_id=%s and t.txn_type in ('CASH_DEPOSIT','CASH_WITHDRAWAL')
                       and not exists (select 1 from flow_classification fc where fc.txn_id=t.txn_id and fc.is_current)""", (self.broker_id,))
        n = 0
        for (tid,) in cur.fetchall():
            self.conn.cursor().execute("""insert into flow_classification(txn_id,flow_class,method,confidence,decided_by,classifier_version)
                                          values (%s,'EXTERNAL','DEFAULT','MEDIUM','rule:import',%s)""", (tid, CLASSIFIER_VERSION))
            n += 1
        report["flow_classes"] = {"EXTERNAL": n}

    def _validate(self, pf, report, accounts):
        cur, chk = self.cur, report["checks"]

        def check(name, ok, note=""):
            chk.append({"kontrola": name, "status": "OK" if ok else "WARNING", "uwagi": note})
            if not ok:
                report["warnings"].append(f"Kontrola nieudana: {name}. {note}")

        uuids = [tr._tid(r, None) for r in pf.records]
        good = [u for u in uuids if u]
        check("Identyfikatory transakcji (UUID) są unikalne w pliku", len(good) == len(set(good)))
        cur.execute("""select count(*) from transactions where broker_id=%s and txn_type in ('BUY','SELL') and status='ACTIVE'
                       and abs(quantity*price - gross_amount) > 0.02""", (self.broker_id,))
        check("Ilość × cena ≈ kwota transakcji (do 0,02 zł)", cur.fetchone()[0] == 0)
        # saldo gotowki (rekonstrukcja) nigdy nie schodzi ponizej zera po czasie operacji
        cur.execute("""select account_id, occurred_at, txn_id, cash_amount from transactions where broker_id=%s and status='ACTIVE' order by account_id, occurred_at, txn_id""", (self.broker_id,))
        bal, neg = defaultdict(Decimal), 0
        for acct, when, tid, amt in cur.fetchall():
            bal[acct] += amt
            if bal[acct] < Decimal("-0.005"):
                neg += 1
        check("Saldo gotówki (rekonstrukcja z operacji) nigdy nie jest ujemne", neg == 0, f"{neg} operacji z ujemnym saldem" if neg else "")
        report["info"].append("Eksport Trade Republic nie zawiera salda ani pozycji – kontrole są wewnętrzne (spójność i rekonstrukcja).")
