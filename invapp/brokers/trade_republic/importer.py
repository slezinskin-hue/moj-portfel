"""
Silnik importu Trade Republic (CSV): ta sama gwarancja co dla XTB - atomowa partia, idempotencja po dedup_key (UUID wiersza) + row_hash, surowe wiersze,
konflikty, bez zgadywania. Instrumenty po ISIN (z eksportu brokera); waluta i gielda notowania zostaja NIEZNANE, dopoki nie ustali ich zrodlo zewnetrzne.
Przeplywy gotowkowe: klasa EXTERNAL (domyslnie) - kontrahent (IBAN) jest zapisany tylko jako skrot i maska.
"""
from collections import defaultdict
from decimal import Decimal

from psycopg2.extras import Json

from . import parser as tr
from invapp.core.common import jsonable
from invapp.core.ledger_import import ImportBatch

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
        cur.execute("select pg_advisory_xact_lock(hashtext('import:TR'))")       # konta zakladane pod ta sama blokada co import
        accounts, created = {}, []
        for at in sorted(pf.account_types):
            acct, new = ensure_account(cur, self.broker_id, at)
            accounts[at] = acct
            if new:
                created.append(f"Trade Republic{'' if at == 'DEFAULT' else ' (' + at + ')'}")
        main = accounts[sorted(accounts)[0]] if accounts else None
        # wspolny silnik partii (core/ledger_import.py): idempotencja, konflikty, surowe wiersze, liczniki, kontrola G3
        b = ImportBatch(self.conn, "TR", pf.name, pf.sha256, tr.PARSER_NAME, tr.PARSER_VERSION, account_id=main,
                        period_from=pf.period_from, period_to=pf.period_to, warnings=pf.warnings)
        report = b.report
        report["created_accounts"] = created
        cands = [tr.normalize(rec) for rec in pf.records]
        b.prefetch([c.dedup_key for c in cands if not c.unmapped_reason] + [ch.dedup_key for c in cands if not c.unmapped_reason for ch in c.children])
        for cand in cands:
            if cand.unmapped_reason:
                b.raw_row("CSV", cand.row, "DATA", cand.dedup_key, cand.row_hash, cand.payload, "UNMAPPED")
                report["warnings"].append(f"Wiersz {cand.row}: {cand.unmapped_reason}.")
                continue
            b.write_transaction(self._values(cand, accounts[cand.account_type]), "CSV", cand.row, cand.payload)
            for ch in cand.children:
                b.write_transaction(self._values(ch, accounts[ch.account_type]), "CSV (podatek z wiersza)", cand.row, ch.payload)
        self._link_rewards(report)
        self._classify_flows(report)
        self._validate(pf, report, accounts)
        return b.finish()

    def _values(self, c, account):
        """Kandydat z parsera -> kolumny ksiegi (transactions)."""
        listing = self.resolver.listing(c.isin, c.name, c.asset_class) if c.isin else None
        return {"account_id": account, "txn_type": c.txn_type, "txn_subtype": c.subtype, "occurred_at": c.occurred_at, "listing_id": listing,
                "quantity": c.quantity, "price": c.price, "price_currency": "PLN" if c.txn_type in ("BUY", "SELL") else None,   # cena w PLN z kolumny currency pliku
                "gross_amount": c.gross, "cash_amount": c.cash_amount, "commission": c.commission, "external_id": c.external_id,
                "external_id_scheme": "TR_TXN_UUID", "dedup_key": c.dedup_key, "row_hash": c.row_hash, "raw_comment": c.comment, "details": c.details}

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
