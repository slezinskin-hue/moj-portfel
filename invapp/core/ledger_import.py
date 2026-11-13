"""
Wspolny silnik partii importu - to, co jest takie samo dla kazdego brokera (docs/DODAWANIE_BROKERA.md):

* jedna partia (import_batches) = jeden plik = jedna transakcja bazy; blad = wycofanie calosci (wywolujacy nie robi commitu przy bledzie);
* idempotencja: klucz deduplikacji (dedup_key) + skrot tresci (row_hash). Ten sam klucz i ta sama tresc = DUPLICATE (operacja juz jest),
  ten sam klucz i inna tresc = CONFLICT (zachowany istniejacy wiersz, wpis w import_conflicts do decyzji), nowy klucz = INSERTED;
* kazdy wiersz pliku trafia do raw_rows z wynikiem (INSERTED / DUPLICATE / CONFLICT / UNMAPPED / IGNORED) - nic nie znika po cichu;
* kontrola G3: suma wynikow = liczba przeczytanych wierszy;
* wydajnosc (dlugie eksporty): istniejace klucze pobierane jednym zapytaniem (prefetch), "widziano w tym pliku" ustawiane zbiorczo,
  surowe wiersze zapisywane wsadowo.

Uzycie (szkic - pelny przyklad: invapp/brokers/trade_republic/importer.py albo szablon z `python -m invapp.tools.nowy_broker`):

    b = ImportBatch(conn, "RVL", file_name, sha256, "revolut_csv", "1.0", account_id=konto)
    b.prefetch([c.dedup_key for c in kandydaci])
    for c in kandydaci:
        if c.nieznany_typ:
            b.raw_row("CSV", c.row, "DATA", c.dedup_key, c.row_hash, c.payload, "UNMAPPED"); continue
        b.write_transaction({"account_id": konto, "txn_type": c.typ, "occurred_at": c.kiedy, "cash_amount": c.kwota, ..., "dedup_key": c.dedup_key,
                             "row_hash": c.row_hash, "external_id": c.id, "external_id_scheme": "RVL_TXN_ID"}, "CSV", c.row, c.payload)
    report = b.finish()
"""
from collections import defaultdict

from psycopg2.extras import Json, execute_values

from invapp.core.common import ImportFailed, as_utc, jsonable

OUTCOMES = ("INSERTED", "DUPLICATE", "CONFLICT", "UNMAPPED", "IGNORED")
REQUIRED = ("account_id", "txn_type", "occurred_at", "cash_amount", "dedup_key", "row_hash", "external_id", "external_id_scheme")


def J(obj):
    return Json(jsonable(obj))


class ImportBatch:
    def __init__(self, conn, broker_code, file_name, sha256, parser_name, parser_version, account_id=None, period_from=None, period_to=None,
                 report_generated_at=None, warnings=None):
        self.conn, self.cur = conn, conn.cursor()
        cur = self.cur
        cur.execute("select broker_id from financial_institutions where code = %s", (broker_code,))
        row = cur.fetchone()
        if not row:
            raise ImportFailed(f"Broker {broker_code} nie jest zarejestrowany.")
        self.broker_id, self.broker_code = row[0], broker_code
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"import:{broker_code}",))      # importy tego brokera po kolei
        cur.execute("select count(*) from import_batches where source_file_sha256 = %s and status = 'COMMITTED'", (sha256,))
        same_before = cur.fetchone()[0]
        cur.execute("""insert into import_batches(broker_id,source_file_name,source_file_sha256,parser_name,parser_version,period_from,period_to,report_generated_at,
                                                  account_id,status)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING') returning batch_id""",
                    (self.broker_id, file_name, sha256, parser_name, parser_version, as_utc(period_from), as_utc(period_to), as_utc(report_generated_at), account_id))
        self.batch = cur.fetchone()[0]
        self.report = {"file": file_name, "sha256": sha256, "batch_id": self.batch, "warnings": list(warnings or []), "info": [], "checks": [], "same_file_imported_before": same_before,
                       "broker": broker_code}
        if same_before:
            self.report["warnings"].append("Ten plik (ta sama suma SHA-256) był już zaimportowany; gwarancja idempotencji wynika z kluczy deduplikacji.")
        self.counters = defaultdict(int)
        self._raw, self._known, self._seen = [], {}, []

    # ---- surowe wiersze i liczniki
    def raw_row(self, sheet, row, kind, key, row_hash, payload, outcome, txn_id=None):
        """Zapis wyniku wiersza pliku (kazdy wiersz - takze pominiety - musi tu trafic dokladnie raz)."""
        if outcome not in OUTCOMES:
            raise ValueError(outcome)
        self.counters[outcome] += 1; self.counters["read"] += 1
        self._raw.append((self.batch, sheet, row, kind, key, row_hash, J(payload), outcome, txn_id))

    # ---- operacje ksiegi
    def prefetch(self, dedup_keys):
        """Operacje juz zapisane (ponowny import / nowszy eksport): jedno zapytanie na plik zamiast kilku na wiersz."""
        keys = [k for k in dedup_keys if k]
        if keys:
            self.cur.execute("select dedup_key, txn_id, row_hash from transactions where dedup_key = any(%s)", (keys,))
            self._known.update({k: (t, h) for k, t, h in self.cur.fetchall()})

    def write_transaction(self, values, sheet, row, payload, kind="DATA"):
        """Zapis operacji do ksiegi (albo DUPLICATE / CONFLICT). values: kolumny tabeli transactions; broker_id, waluta gotowki (PLN),
        partia i zrodlo uzupelniane tu. Zwraca (txn_id, wynik)."""
        missing = [c for c in REQUIRED if values.get(c) in (None, "")]
        if missing:
            raise ImportFailed(f"Wiersz {row}: brak pól {', '.join(missing)} – błąd adaptera brokera.")
        key, h = values["dedup_key"], values["row_hash"]
        if key in self._known:
            tid, old = self._known[key]
            if old == h:
                self._seen.append(tid)
                self.raw_row(sheet, row, kind, key, h, payload, "DUPLICATE", tid)
                return tid, "DUPLICATE"
            self.cur.execute("insert into import_conflicts(batch_id,dedup_key,existing_hash,incoming_hash,incoming_payload) values (%s,%s,%s,%s,%s)",
                             (self.batch, key, old, h, J(payload)))
            self.report["warnings"].append(f"Wiersz {row}: ten sam klucz {key}, inna treść – zachowano istniejący wiersz (konflikt do decyzji).")
            self.raw_row(sheet, row, kind, key, h, payload, "CONFLICT", tid)
            return tid, "CONFLICT"
        cols = dict(values)
        cols.setdefault("cash_currency", "PLN"); cols.setdefault("source_kind", "BROKER_IMPORT")
        cols["broker_id"], cols["first_batch_id"], cols["last_seen_batch_id"] = self.broker_id, self.batch, self.batch
        if isinstance(cols.get("details"), dict):
            cols["details"] = J(cols["details"])
        names = list(cols)
        self.cur.execute(f"insert into transactions({','.join(names)}) values ({','.join(['%s'] * len(names))}) on conflict (dedup_key) do nothing returning txn_id",
                         [cols[n] for n in names])
        ins = self.cur.fetchone()
        if not ins:
            raise ImportFailed(f"Wiersz {row}: klucz {key} pojawił się w trakcie importu – przerwano (spróbuj ponownie).")
        self._known[key] = (ins[0], h)                        # ten sam klucz drugi raz w pliku = duplikat albo konflikt
        self.raw_row(sheet, row, kind, key, h, payload, "INSERTED", ins[0])
        return ins[0], "INSERTED"

    # ---- zakonczenie
    def finish(self):
        """Zbiorcze "widziano w tym pliku", surowe wiersze, kontrola G3, partia COMMITTED. Zwraca raport z licznikami."""
        cur = self.cur
        if self._seen:
            cur.execute("update transactions set last_seen_batch_id = %s, updated_at = now() where txn_id = any(%s)", (self.batch, self._seen))
        execute_values(cur, "insert into raw_rows(batch_id,sheet_name,row_number,row_kind,natural_key,row_hash,payload,outcome,txn_id) values %s", self._raw, page_size=500)
        c = self.counters
        total = sum(c[o] for o in OUTCOMES)
        if total != c["read"]:
            raise ImportFailed(f"Kontrola G3 nie przeszła: suma wyników ({total}) ≠ liczba wierszy ({c['read']}).")
        self.report["counters"] = {o: c[o] for o in OUTCOMES}; self.report["counters"]["read"] = c["read"]
        cur.execute("""update import_batches set status='COMMITTED', rows_read=%s, rows_inserted=%s, rows_duplicate=%s, rows_conflict=%s, rows_unmapped=%s, rows_ignored=%s,
                       finished_at=now(), report=%s where batch_id=%s""",
                    (c["read"], c["INSERTED"], c["DUPLICATE"], c["CONFLICT"], c["UNMAPPED"], c["IGNORED"], J(self.report), self.batch))
        return self.report


# ---------------------------------------------------------------- konta i instrumenty (wspolne dla nowych brokerow)
DEFAULT_SCOPES = ("INVESTMENT_ALL", "NET_WORTH")


def ensure_account(cur, broker_code, external_no, display_name, product_type="STANDARD", tax_regime="PIT38", source="FILE"):
    """Konto brokera (jedno na numer/identyfikator z pliku). Nowe konto trafia do zakresow BROKER:<kod>, WSZYSTKIE i majatku. Zwraca (account_id, utworzone?)."""
    cur.execute("select broker_id from financial_institutions where code = %s", (broker_code,))
    broker_id = cur.fetchone()[0]
    cur.execute("select account_id from accounts where broker_id = %s and external_no = %s", (broker_id, external_no))
    r = cur.fetchone()
    if r:
        return r[0], False
    cur.execute("""insert into accounts(broker_id,external_no,external_no_source,display_name,product_type,account_class,tracking_mode,tax_regime)
                   values (%s,%s,%s,%s,%s,'INVESTMENT','FULL',%s) returning account_id""", (broker_id, external_no, source, display_name, product_type, tax_regime))
    acct = cur.fetchone()[0]
    for code in (f"BROKER:{broker_code}",) + DEFAULT_SCOPES:
        cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = %s on conflict do nothing", (acct, code))
    return acct, True


class IsinResolver:
    """ISIN z eksportu brokera -> notowanie (wspolny papier dla wszystkich brokerow o tym samym ISIN). Nowe notowanie ma NIEZNANA walute
    i gielde (zasada D-INSTR-1: nie zgadujemy) - ustala je potem krok cen (np. resolve_isin_listings: ISIN -> symbol Yahoo ze sprawdzeniem ceny)."""

    def __init__(self, cur, broker_code, source_label):
        cur.execute("select broker_id from financial_institutions where code = %s", (broker_code,))
        self.cur, self.broker_id, self.source_label, self.cache, self.created = cur, cur.fetchone()[0], source_label, {}, []

    def listing(self, isin, name=None, asset_class="STOCK"):
        if not isin:
            return None
        if isin in self.cache:
            return self.cache[isin]
        cur = self.cur
        cur.execute("select listing_id from listing_aliases where broker_id = %s and broker_symbol = %s and valid_to is null", (self.broker_id, isin))
        r = cur.fetchone()
        if r:
            self.cache[isin] = r[0]; return r[0]
        cur.execute("select security_id from securities where isin = %s", (isin,))
        r = cur.fetchone()
        if r:
            sec = r[0]
        else:
            cur.execute("""insert into securities(isin,isin_status,isin_evidence,name,asset_class) values (%s,'Z_EKSPORTU_BROKERA',%s,%s,%s) returning security_id""",
                        (isin, J({"source": self.source_label}), name or isin, "ETF" if asset_class == "ETF" else "STOCK"))
            sec = cur.fetchone()[0]
        cur.execute("insert into listings(security_id,display_name,currency_evidence) values (%s,%s,%s) returning listing_id",
                    (sec, name or isin, J({"note": f"{self.source_label}: ISIN bez giełdy i waluty notowania – do ustalenia ze źródła zewnętrznego"})))
        lst = cur.fetchone()[0]
        cur.execute("insert into listing_aliases(broker_id,broker_symbol,listing_id,verified_by) values (%s,%s,%s,%s)",
                    (self.broker_id, isin, lst, f"external:{self.source_label}"))
        self.cache[isin] = lst; self.created.append(isin)
        return lst
