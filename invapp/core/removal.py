"""
Usuniecie wczytanego pliku (partii importu) na prosbe uzytkownika (decyzja 2026-11-12: "ktos moze dolozyc eksport albo usunac, jesli chce").

Zasada: stan po usunieciu = stan, jak gdyby tego pliku NIGDY nie wczytano.
* operacje, zamkniete pozycje i migawki, ktore wystepuja takze w innym wczytanym pliku (ten sam klucz deduplikacji), ZOSTAJA
  (np. starszy eksport usuniety po wczytaniu nowszego niczego nie zmienia);
* usuwane jest tylko to, co pochodzi WYLACZNIE z tego pliku, razem z jego surowymi wierszami;
* powiazania liczone z calej ksiegi (przelewy miedzy kontami, podatek -> dywidenda, zlecenia, splity wykryte z danych, klasyfikacja wplat)
  sa liczone od nowa z pozostalych danych tak samo jak przy imporcie; partie i koszty - przeliczone od zera;
* konto, ktore nie ma juz zadnych danych z plikow, jest usuwane (z decyzjami dotyczacymi tylko tego konta, np. lokata);
* decyzje dotyczace instrumentow, ktore nie maja juz zadnych danych (splity), i reguly walut brokera bez zadnego pliku sa usuwane -
  po ponownym wczytaniu aplikacja zapyta o nie od nowa (decyzja uzytkownika 2026-11-12); decyzje dotyczace danych, ktore zostaja, zostaja
  (np. przyjety split instrumentu, ktory jest jeszcze na innym koncie), tak samo rejestr kontrahentow i ustawienia aplikacji.
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


def account_batches(cur, account_id):
    """(nazwa konta, wczytane pliki konta - najnowsze najpierw); blad, gdy konta nie ma albo nie ma plikow."""
    cur.execute("select display_name from accounts where account_id = %s", (account_id,))
    row = cur.fetchone()
    if not row:
        raise ImportFailed("Nie ma takiego konta.")
    batches = _ids(cur, "select batch_id from import_batches where account_id = %s and status = 'COMMITTED' order by batch_id desc", (account_id,))
    if not batches:
        raise ImportFailed("To konto nie ma wczytanych plików.")
    return row[0], batches


def remove_account(conn, account_id):
    """Usuwa caly portfel (konto): wszystkie jego pliki po kolei mechanizmem remove_batch - stan jak gdyby zadnego z nich nie wczytano.
    Bez commitu (robi go wywolujacy). Dane innych kont (takze z tego samego brokera) zostaja."""
    cur = conn.cursor()
    name, batches = account_batches(cur, account_id)
    ops, accounts, files = 0, set(), []
    for b in batches:
        res = remove_batch(conn, b)
        ops += res["usuniete_operacje"]; accounts |= set(res["usuniete_konta"]); files.append(res["plik"])
    cur.execute("select count(*) from transactions where account_id = %s", (account_id,))
    return {"konto": name, "usuniete_pliki": files, "usuniete_operacje": ops, "usuniete_konta": sorted(accounts), "pozostale_operacje_konta": cur.fetchone()[0]}


def remove_all(conn):
    """Usuwa WSZYSTKIE wczytane pliki (wszystkie portfele) - stan jak po czystej instalacji: bez kont, operacji i decyzji dotyczacych danych.
    Zostaje to, co nie pochodzi z plikow: konta bankowe z zakladki Gotowka, ustawienia aplikacji, dane referencyjne (ceny, kursy, specyfikacje).
    Bez commitu (robi go wywolujacy)."""
    cur = conn.cursor()
    batches = _ids(cur, "select batch_id from import_batches where status = 'COMMITTED' order by batch_id desc")
    if not batches:
        raise ImportFailed("Nie ma wczytanych plików.")
    ops, accounts, files = 0, set(), []
    for b in batches:
        res = remove_batch(conn, b)
        ops += res["usuniete_operacje"]; accounts |= set(res["usuniete_konta"]); files.append(res["plik"])
    cur.execute("select count(*) from transactions")
    return {"usuniete_pliki": files, "usuniete_operacje": ops, "usuniete_konta": sorted(accounts), "pozostale_operacje": cur.fetchone()[0]}


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
    # 6a. decyzje, ktore dotyczyly juz tylko usunietych danych (po ponownym wczytaniu aplikacja zapyta o nie od nowa)
    forgotten = _forget_orphan_decisions(cur, broker_id)
    # 7. powiazania i warstwa pochodna od nowa (te same procedury co przy imporcie)
    report = {"warnings": [], "info": []}
    b = brokers.by_code(code)
    if b is not None:
        b.relink(conn, report)                                    # ta sama procedura co przy imporcie (kazdy broker wie, co laczy)
    # kontrahenci zaproponowani automatycznie (bez decyzji uzytkownika), ktorych nic juz nie dotyczy
    cur.execute("""delete from counterparties c where c.ownership_status in ('UNKNOWN', 'OWN_CANDIDATE') and c.decided_at is null
                     and not exists (select 1 from flow_classification fc where fc.counterparty_id = c.counterparty_id)""")
    b = brokers.by_code(code)
    if b is not None:
        b.rebuild_derived(conn)
    return {"usuniete_operacje": len(gone), "zachowane_wspolne": len(kept_shared), "usuniete_konta": removed_accounts, "zapomniane_decyzje": forgotten}


def _forget_orphan_decisions(cur, broker_id):
    """Decyzja uzytkownika 2026-11-12: "jak usune portfel i dodam go znowu, aplikacja ma ponownie zapytac o wszystko, co sie nie zgadza".
    * instrumenty tego brokera BEZ zadnych danych w zadnym koncie (operacje, partie, pozycje, migawki): ich korekty (splity - przyjete, odrzucone,
      hipotezy) i ostrzezenia jakosci danych sa usuwane;
    * gdy broker nie ma juz zadnego wczytanego pliku: jego reguly walut (np. .PL = PLN) sa usuwane, a waluta instrumentow ustalona TYLKO ta regula
      wraca do "nieznanej" (zmiana zapisuje sie w historii atrybutow);
    * decyzje dotyczace instrumentow lub kont, ktore nadal maja dane, zostaja. Ustawienia aplikacji (np. zrodlo cen) nie zaleza od portfela - zostaja."""
    orphans = _ids(cur, """select distinct la.listing_id from listing_aliases la where la.broker_id = %s
                             and not exists (select 1 from transactions t where t.listing_id = la.listing_id)
                             and not exists (select 1 from derived_lots d where d.listing_id = la.listing_id)
                             and not exists (select 1 from broker_closed_positions c where c.listing_id = la.listing_id)
                             and not exists (select 1 from broker_position_snapshots s where s.listing_id = la.listing_id)""", (broker_id,))
    out = {"korekty": 0, "reguly_walut": 0, "waluty_instrumentow": 0}
    if orphans:
        actions = _ids(cur, "select action_id from corporate_actions where listing_id = any(%s)", (orphans,))
        if actions:
            cur.execute("update broker_closed_positions set corporate_action_id = null where corporate_action_id = any(%s)", (actions,))
            cur.execute("delete from corporate_action_position_map where action_id = any(%s)", (actions,))
            cur.execute("delete from data_quality_issues where batch_id is null and (details->>'action_id')::bigint = any(%s)", (actions,))
            cur.execute("delete from corporate_actions where action_id = any(%s)", (actions,))
            out["korekty"] = len(actions)
        cur.execute("delete from data_quality_issues where batch_id is null and entity_ref = any(%s)", ([f"listing:{l}" for l in orphans],))
    cur.execute("select exists (select 1 from import_batches where broker_id = %s)", (broker_id,))
    if not cur.fetchone()[0]:
        cur.execute("select count(*) from broker_symbol_suffix_rules where broker_id = %s", (broker_id,))
        out["reguly_walut"] = cur.fetchone()[0]
        cur.execute("delete from broker_symbol_suffix_rules where broker_id = %s", (broker_id,))
        if orphans:
            cur.execute("""update listings set trading_currency = null, currency_source = null, currency_checked_at = null, currency_evidence = null
                           where listing_id = any(%s) and currency_status = 'POTWIERDZONY_PRZEZ_UZYTKOWNIKA'""", (orphans,))
            out["waluty_instrumentow"] = cur.rowcount
    return out
