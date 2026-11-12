"""
Usuniecie wczytanego pliku (partii importu) na prosbe uzytkownika (decyzja 2026-11-12: "ktos moze dolozyc eksport albo usunac, jesli chce").

Zasada: stan po usunieciu = stan, jak gdyby tego pliku NIGDY nie wczytano.
* operacje, zamkniete pozycje i migawki, ktore wystepuja takze w innym wczytanym pliku (ten sam klucz deduplikacji), ZOSTAJA
  (np. starszy eksport usuniety po wczytaniu nowszego niczego nie zmienia);
* usuwane jest tylko to, co pochodzi WYLACZNIE z tego pliku, razem z jego surowymi wierszami;
* powiazania liczone z calej ksiegi (przelewy miedzy kontami, podatek -> dywidenda, zlecenia, splity wykryte z danych, klasyfikacja wplat)
  sa liczone od nowa z pozostalych danych tak samo jak przy imporcie; partie i koszty - przeliczone od zera;
* konto, ktore nie ma juz zadnych danych z plikow, jest usuwane (z decyzjami dotyczacymi tylko tego konta, np. lokata);
* decyzje uzytkownika niezalezne od pliku zostaja (reguly walut, potwierdzone splity, rejestr kontrahentow).
Ksiega jest chroniona wyzwalaczami przed zmianami - na czas tej jednej transakcji (rola wlasciciela) sa wylaczone i wlaczane z powrotem.
W aplikacji przed usunieciem robi sie automatyczna kopia zapasowa bazy.
"""
from invapp import brokers
from invapp.core.common import ImportFailed

GUARDED = ("transactions", "raw_rows", "broker_closed_positions", "broker_position_snapshots", "derived_lots")
SUPPORT = ("INSERTED", "DUPLICATE", "CONFLICT")


def _ids(cur, sql, args=()):
    cur.execute(sql, args)
    return [r[0] for r in cur.fetchall()]


def remove_batch(conn, batch_id):
    """Usuwa plik (partie importu). Bez commitu - robi go wywolujacy. Zwraca podsumowanie."""
    cur = conn.cursor()
    cur.execute("""select b.batch_id, f.code, b.source_file_name, b.status, b.broker_id from import_batches b join financial_institutions f using (broker_id)
                   where b.batch_id = %s""", (batch_id,))
    row = cur.fetchone()
    if not row or row[3] != "COMMITTED":
        raise ImportFailed("Nie ma takiego wczytanego pliku.")
    _, code, name, _, broker_id = row
    cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"import:{code}",))
    others = _ids(cur, "select batch_id from import_batches where broker_id = %s and status = 'COMMITTED' and batch_id <> %s", (broker_id, batch_id))
    for t in GUARDED:
        cur.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
    try:
        summary = _remove(cur, conn, batch_id, code, broker_id, others)
    finally:
        for t in GUARDED:
            cur.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
    summary["plik"] = name
    return summary


def _remove(cur, conn, batch_id, code, broker_id, others):
    accounts_before = _ids(cur, "select account_id from accounts where broker_id = %s", (broker_id,))
    # 1. operacje tylko z tego pliku (bez wsparcia w surowych wierszach innych plikow)
    gone = _ids(cur, """select t.txn_id from transactions t
                        where t.broker_id = %s and (t.first_batch_id = %s or exists (select 1 from raw_rows r where r.batch_id = %s and r.txn_id = t.txn_id))
                          and not exists (select 1 from raw_rows r where r.txn_id = t.txn_id and r.batch_id = any(%s) and r.outcome = any(%s))""",
                (broker_id, batch_id, batch_id, others, list(SUPPORT)))
    kept_shared = _ids(cur, """select distinct r.txn_id from raw_rows r where r.batch_id = %s and r.txn_id is not null and r.txn_id <> all(%s)""", (batch_id, gone))
    # warstwa pochodna brokera - przeliczana od zera na koncu
    cur.execute("delete from derived_lot_closures where lot_id in (select lot_id from derived_lots dl join accounts a using (account_id) where a.broker_id = %s)", (broker_id,))
    cur.execute("delete from derived_lots where account_id in (select account_id from accounts where broker_id = %s)", (broker_id,))
    # 2. powiazania liczone z calej ksiegi: zerowane dla brokera i liczone od nowa (jak przy imporcie)
    cur.execute("update transactions set related_txn_id = null where broker_id = %s and related_txn_id is not null", (broker_id,))
    groups = _ids(cur, "select distinct transfer_group_id from transactions where broker_id = %s and transfer_group_id is not null", (broker_id,))
    cur.execute("update transactions set transfer_group_id = null where broker_id = %s and transfer_group_id is not null", (broker_id,))
    cur.execute("""delete from flow_classification where txn_id in (select txn_id from transactions where broker_id = %s)
                     and (decided_by like 'rule:%%' or txn_id = any(%s))""", (broker_id, gone))
    cur.execute("delete from flow_classification where transfer_group_id = any(%s)", (groups,))
    cur.execute("delete from transfer_groups where transfer_group_id = any(%s)", (groups,))
    # splity wykryte automatycznie z danych (artefakt brokera, niepotwierdzona hipoteza) - wykrywane od nowa z pozostalych danych;
    # splity potwierdzone albo odrzucone przez uzytkownika zostaja (to jego decyzje)
    auto = _ids(cur, """select a.action_id from corporate_actions a
                        where (a.source = 'BROKER_ARTIFACT' or (a.source = 'INFERRED' and a.status = 'HYPOTHESIS'))
                          and exists (select 1 from listing_aliases la where la.listing_id = a.listing_id and la.broker_id = %s)""", (broker_id,))
    if auto:
        cur.execute("update broker_closed_positions set corporate_action_id = null where corporate_action_id = any(%s)", (auto,))
        cur.execute("delete from corporate_action_position_map where action_id = any(%s)", (auto,))
        cur.execute("delete from data_quality_issues where batch_id is null and (details->>'action_id')::bigint = any(%s)", (auto,))
        cur.execute("delete from corporate_actions where action_id = any(%s)", (auto,))
    # 3. usuniecie operacji tylko z tego pliku
    cur.execute("delete from flow_classification where txn_id = any(%s)", (gone,))
    cur.execute("update raw_rows set txn_id = null where txn_id = any(%s)", (gone,))
    cur.execute("delete from transactions where txn_id = any(%s)", (gone,))
    # operacje zachowane (sa tez w innych plikach): znaczniki pierwszego / ostatniego pliku z pozostalych plikow
    cur.execute("""update transactions t set first_batch_id = s.mn, last_seen_batch_id = s.mx
                   from (select r.txn_id, min(r.batch_id) mn, max(r.batch_id) mx from raw_rows r where r.batch_id = any(%s) and r.txn_id is not null group by 1) s
                   where s.txn_id = t.txn_id and t.broker_id = %s and (t.first_batch_id = %s or t.last_seen_batch_id = %s)""", (others, broker_id, batch_id, batch_id))
    # 4. zamkniete pozycje i migawki pozycji (XTB)
    cur.execute("""delete from broker_closed_positions c where c.first_batch_id = %s
                     and not exists (select 1 from raw_rows r where r.batch_id = any(%s) and r.sheet_name = 'Closed Positions' and r.natural_key = c.dedup_key and r.outcome = any(%s))""",
                (batch_id, others, list(SUPPORT)))
    cur.execute("""update broker_closed_positions c set first_batch_id = s.mn
                   from (select r.natural_key, min(r.batch_id) mn from raw_rows r where r.batch_id = any(%s) and r.sheet_name = 'Closed Positions' group by 1) s
                   where s.natural_key = c.dedup_key and c.first_batch_id = %s""", (others, batch_id))
    cur.execute("""update broker_position_snapshots s set batch_id = o.batch_id
                   from (select b.batch_id, b.account_id, b.report_generated_at from import_batches b where b.batch_id = any(%s)) o,
                        import_batches me
                   where me.batch_id = %s and s.batch_id = me.batch_id and o.report_generated_at = me.report_generated_at
                     and o.report_generated_at is not null and o.account_id = me.account_id""", (others, batch_id))
    cur.execute("delete from broker_position_snapshots where batch_id = %s", (batch_id,))
    # 5. sam plik
    cur.execute("delete from raw_rows where batch_id = %s", (batch_id,))
    cur.execute("delete from import_conflicts where batch_id = %s", (batch_id,))
    cur.execute("delete from data_quality_issues where batch_id = %s", (batch_id,))
    cur.execute("delete from import_batches where batch_id = %s", (batch_id,))
    # 6. konta bez zadnych danych z plikow (najpierw podkonta)
    removed_accounts = []
    for _ in range(3):
        empty = _ids(cur, """select a.account_id from accounts a where a.broker_id = %s and a.account_id = any(%s)
                               and coalesce(a.external_no_source, 'FILE') <> 'USER'
                               and not exists (select 1 from transactions t where t.account_id = a.account_id)
                               and not exists (select 1 from import_batches b where b.account_id = a.account_id)
                               and not exists (select 1 from broker_position_snapshots s where s.account_id = a.account_id)
                               and not exists (select 1 from broker_closed_positions c where c.account_id = a.account_id)
                               and not exists (select 1 from accounts ch where ch.parent_account_id = a.account_id)""", (broker_id, accounts_before))
        if not empty:
            break
        cur.execute("select display_name from accounts where account_id = any(%s)", (empty,))
        removed_accounts += [r[0] for r in cur.fetchall()]
        cur.execute("update counterparties set mapped_account_id = null where mapped_account_id = any(%s)", (empty,))
        cur.execute("delete from flow_classification where counterparty_account_id = any(%s)", (empty,))
        for t in ("account_aliases", "account_identifiers", "scope_accounts", "cash_deposit_terms", "account_balance_snapshots", "corporate_action_position_map"):
            cur.execute(f"delete from {t} where account_id = any(%s)", (empty,))
        cur.execute("delete from accounts where account_id = any(%s)", (empty,))
    # 7. powiazania i warstwa pochodna od nowa (te same procedury co przy imporcie)
    report = {"warnings": [], "info": []}
    if code == "XTB":
        from invapp.brokers.xtb.importer import XtbImporter
        imp = XtbImporter(conn)
        imp.link_wht(report); imp.link_sec_corrections(report); imp.group_orders(report); imp.link_transfers(report)
        imp.classify_flows(report); imp.detect_corporate_actions(report)
    elif code == "TR":
        from invapp.brokers.trade_republic.importer import TrImporter
        imp = TrImporter(conn)
        imp._link_rewards(report); imp._classify_flows(report)
    # kontrahenci zaproponowani automatycznie (bez decyzji uzytkownika), ktorych nic juz nie dotyczy
    cur.execute("""delete from counterparties c where c.ownership_status in ('UNKNOWN', 'OWN_CANDIDATE') and c.decided_at is null
                     and not exists (select 1 from flow_classification fc where fc.counterparty_id = c.counterparty_id)""")
    b = brokers.by_code(code)
    if b is not None:
        b.rebuild_derived(conn)
    return {"usuniete_operacje": len(gone), "zachowane_wspolne": len(kept_shared), "usuniete_konta": removed_accounts}
