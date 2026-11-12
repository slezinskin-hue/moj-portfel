"""
Warstwa pochodna (przeliczalna od zera z ksiegi, dokument 7 §7.9; decyzje D-KOSZT-1 i Q2 z dokumentu 17):
  * partia (lot) = jeden zakup z ksiegi; koszt = GOTOWKA z ksiegi (nie koszt podatkowy);
  * dopasowanie sprzedazy do zakupu po Position ID XTB (fakt z eksportu); udzial zamknietej czesci pozycji wyznaczany w jednostkach brokera
    (ilosc sprzedazy / ilosc pozycji w jednostkach brokera = zamkniete + pozostale), wiec nie zalezy od niepotwierdzonych korekt splitu; wypelnienia jednego zlecenia scalone przez XTB w jedna pozycje tworza jedna grupe;
  * stosowane sa WYLACZNIE korekty CONFIRMED (hipoteza splitu jest niestosowana; ujemny stan surowej ksiegi jest ostrzezeniem).
"""
from collections import defaultdict
from decimal import Decimal

from psycopg2.extras import Json

from .common import jsonable

Q8 = Decimal("0.00000001")


def rebuild(conn, broker_code="XTB"):
    cur = conn.cursor()
    cur.execute("select broker_id from financial_institutions where code=%s", (broker_code,))
    broker = cur.fetchone()[0]
    cur.execute("select account_id from accounts where broker_id=%s and tracking_mode='FULL'", (broker,))
    accounts = [r[0] for r in cur.fetchall()]
    report = {"lots": 0, "closures": 0, "warnings": [], "quantity_mismatches": [], "pnl_differences": [], "negative_raw_balances": []}
    cur.execute("delete from derived_lot_closures where lot_id in (select lot_id from derived_lots where account_id = any(%s))", (accounts,))
    cur.execute("delete from derived_lots where account_id = any(%s)", (accounts,))

    # --- korekty stosowane (tylko CONFIRMED)
    cur.execute("""select action_id, listing_id, ratio_new/ratio_old, coalesce(applies_to_lots_opened_before, effective_date::timestamptz) from corporate_actions where status='CONFIRMED'""")
    actions = defaultdict(list)
    for aid, lst, factor, cutoff in cur.fetchall():
        actions[lst].append((aid, factor, cutoff))

    # --- pozycje zywe: Position ID, ktore wystepuja w danych brokera (zamkniete lub otwarte); wypelnienia, ktorych ID zniklo, XTB scalil w pozycje
    #     pierwszego wypelnienia tego samego zlecenia (np. 0,5669 + 3,0 -> jedna pozycja 3,5669 pod ID pierwszego wypelnienia)
    cur.execute("select distinct account_id, broker_position_ref from broker_closed_positions")
    alive = {(a, p) for a, p in cur.fetchall()}
    cur.execute("select distinct account_id, broker_position_ref from broker_position_snapshots where row_level='POSITION'")
    alive |= {(a, p) for a, p in cur.fetchall()}

    # --- partie z zakupow
    cur.execute("""select txn_id, account_id, listing_id, occurred_at, quantity, price, -cash_amount, broker_position_ref, order_ref from transactions
                   where account_id = any(%s) and txn_type='BUY' and status='ACTIVE' order by occurred_at, external_id""", (accounts,))
    buys = cur.fetchall()
    first_alive = {}                                   # (konto, order_ref) -> Position ID pierwszego zywego wypelnienia
    for tid, acct, lst, opened, qty, price, cost, pid, order_ref in buys:
        if order_ref and (acct, pid) in alive:
            first_alive.setdefault((acct, order_ref), pid)
    lots_by_pid = defaultdict(list)                    # grupa pozycji = zywa pozycja wraz ze scalonymi wypelnieniami
    pid_group = {}
    for tid, acct, lst, opened, qty, price, cost, pid, order_ref in buys:
        owner = pid if (acct, pid) in alive or not order_ref else first_alive.get((acct, order_ref), pid)
        gkey = (acct, owner)
        ids, factor = [], Decimal(1)
        for aid, f, cutoff in actions.get(lst, []):
            if cutoff is not None and opened < cutoff:
                ids.append(aid); factor *= f
        q_init, p_adj = (qty * factor), (price / factor).quantize(Q8)
        cur.execute("""insert into derived_lots(account_id,listing_id,open_txn_id,broker_position_ref,opened_at,qty_as_reported,price_as_reported,split_factor,qty_initial,price_adjusted,
                                                qty_remaining,cost_cash_initial,method,engine_version,adjustment_action_ids)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'POSITION_ID','derive-1.1',%s) returning lot_id""",
                    (acct, lst, tid, pid, opened, qty, price, factor, q_init, p_adj, q_init, cost, ids))
        lots_by_pid[gkey].append({"lot_id": cur.fetchone()[0], "qty_initial": q_init, "cost": cost, "listing": lst, "closed_fraction": Decimal(0)})
        pid_group[(acct, pid)] = gkey
        report["lots"] += 1
    # --- objetosc pozycji w jednostkach brokera: zamkniete (bez Correction) + pozostale w migawce
    cur.execute("""select account_id, broker_position_ref, sum(volume) from broker_closed_positions where close_origin is distinct from 'Correction' group by 1,2""")
    closed_vol = {(a, p): v for a, p, v in cur.fetchall()}
    cur.execute("""select s.account_id, s.broker_position_ref, s.volume from broker_position_snapshots s join
                     (select account_id, max(as_of) mx from broker_position_snapshots where row_level='POSITION' group by 1) m on m.account_id=s.account_id and m.mx=s.as_of
                   where s.row_level='POSITION'""")
    open_vol = {(a, p): v for a, p, v in cur.fetchall()}

    # --- sprzedaze -> zamkniecia
    cur.execute("""select txn_id, account_id, occurred_at, quantity, cash_amount, broker_position_ref, external_id from transactions
                   where account_id = any(%s) and txn_type='SELL' and status='ACTIVE' order by occurred_at, external_id""", (accounts,))
    sells = cur.fetchall()
    cur.execute("""select bcp_id, account_id, broker_position_ref, closed_at, sale_value, profit_loss from broker_closed_positions where close_origin is distinct from 'Correction'""")
    bcps = defaultdict(list)
    for r in cur.fetchall():
        bcps[(r[1], r[2])].append(r)
    used_bcp = set()
    for tid, acct, when, qty, amount, pid, ext in sells:
        lots = lots_by_pid.get(pid_group.get((acct, pid)))
        if not lots:
            report["warnings"].append(f"Sprzedaż ID {ext} (pozycja {pid}) bez zakupu w księdze – brak partii do zamknięcia.")
            continue
        v0 = closed_vol.get((acct, pid), Decimal(0)) + open_vol.get((acct, pid), Decimal(0))
        fraction = qty / v0 if v0 else Decimal(0)
        total_cost = sum((l["cost"] for l in lots), Decimal(0))
        match = None
        for b in sorted(bcps.get((acct, pid), []), key=lambda b: abs((b[3] - when).total_seconds())):
            if b[0] not in used_bcp and abs(b[4] - amount) <= Decimal("0.011"):
                match = b; used_bcp.add(b[0]); break
        realized_total = Decimal(0); first = True
        for l in lots:
            share = (l["cost"] / total_cost) if total_cost else Decimal(1) / len(lots)
            cost_part = (l["cost"] * fraction).quantize(Q8); proceeds = (amount * share).quantize(Q8)
            q_closed = (l["qty_initial"] * fraction).quantize(Q8)
            l["closed_fraction"] += fraction
            realized = proceeds - cost_part; realized_total += realized
            cur.execute("""insert into derived_lot_closures(lot_id,sell_txn_id,qty_closed,cost_cash,proceeds_cash,realized_pnl,bcp_id) values (%s,%s,%s,%s,%s,%s,%s)""",
                        (l["lot_id"], tid, q_closed, cost_part, proceeds, realized, match[0] if (match and first) else None))
            report["closures"] += 1; first = False
        if match is not None:
            diff = (realized_total - match[5]).quantize(Decimal("0.01"))
            cur.execute("update derived_lot_closures set pnl_diff_vs_broker=%s where sell_txn_id=%s and bcp_id=%s", (diff, tid, match[0]))
            if abs(diff) > Decimal("0.011"):
                report["pnl_differences"].append({"sell_external_id": ext, "pid": pid, "ledger": str(realized_total.quantize(Decimal('0.01'))), "broker": str(match[5]), "diff": str(diff)})
        else:
            report["warnings"].append(f"Sprzedaż ID {ext} (pozycja {pid}): brak dopasowanego wiersza Closed Positions (po Position ID i kwocie).")
    for lots in lots_by_pid.values():
        for l in lots:
            cur.execute("""update derived_lots set qty_remaining = greatest(qty_initial * (1 - %s), 0) where lot_id=%s""", (min(l["closed_fraction"], Decimal(1)), l["lot_id"]))

    # --- kontrole ilosci: partie (po korektach CONFIRMED) vs migawka brokera
    cur.execute("""select a.display_name, l.display_name, coalesce(dl.qty, 0), coalesce(sn.vol, 0), a.account_id, l.listing_id
                   from (select distinct account_id, listing_id from derived_lots union select distinct account_id, listing_id from broker_position_snapshots) k
                   join accounts a on a.account_id=k.account_id join listings l on l.listing_id=k.listing_id
                   left join (select account_id, listing_id, sum(qty_remaining) qty from derived_lots group by 1,2) dl on dl.account_id=k.account_id and dl.listing_id=k.listing_id
                   left join (select s.account_id, s.listing_id, s.volume vol from broker_position_snapshots s join
                                (select account_id, max(as_of) mx from broker_position_snapshots where row_level='INSTRUMENT' group by 1) m on m.account_id=s.account_id and m.mx=s.as_of
                              where s.row_level='INSTRUMENT') sn on sn.account_id=k.account_id and sn.listing_id=k.listing_id
                   where k.account_id = any(%s)""", (accounts,))
    for acct_name, inst, lots_q, snap_q, acct, lst in cur.fetchall():
        if abs(lots_q - snap_q) > Decimal("0.0002"):
            report["quantity_mismatches"].append({"account": acct_name, "instrument": inst, "lots": str(lots_q), "broker_snapshot": str(snap_q)})

    # --- stan ilosci w ksiedze: zakupy (po korektach CONFIRMED) minus sprzedaze; ujemny = ostrzezenie (np. split niewidoczny w eksporcie, hipoteza niestosowana)
    cur.execute("""select a.display_name, l.display_name,
                          coalesce((select sum(dl.qty_initial) from derived_lots dl where dl.account_id = a.account_id and dl.listing_id = l.listing_id), 0) as bought_adjusted,
                          coalesce(sum(t.quantity) filter (where t.txn_type = 'SELL'), 0) as sold,
                          coalesce(sum(case when t.txn_type = 'BUY' then t.quantity else -t.quantity end), 0) as raw_balance
                   from transactions t join accounts a using (account_id) join listings l using (listing_id)
                   where t.account_id = any(%s) and t.txn_type in ('BUY','SELL') and t.status = 'ACTIVE' group by a.account_id, l.listing_id, 1, 2""", (accounts,))
    for acct_name, inst, bought_adj, sold, raw_bal in cur.fetchall():
        if raw_bal < 0:
            report["negative_raw_balances"].append({"account": acct_name, "instrument": inst, "raw_ledger_balance": str(raw_bal)})
        if bought_adj - sold < 0:
            report["warnings"].append(f"{acct_name} / {inst}: stan {bought_adj - sold} (zakupy po korektach {bought_adj} < sprzedaże {sold}) – prawdopodobny split niewidoczny w eksporcie; hipoteza niestosowana, do potwierdzenia.")
        elif raw_bal < 0:
            report["info"] = report.get("info", []) + [f"{acct_name} / {inst}: surowa księga daje stan {raw_bal}, wyjaśniony potwierdzoną korektą (po korekcie {bought_adj - sold})."]
    cur.execute("""select coalesce(sum(realized_pnl),0) from derived_lot_closures c join derived_lots l using(lot_id) where l.account_id = any(%s)""", (accounts,))
    report["realized_ledger_total"] = str(cur.fetchone()[0].quantize(Decimal("0.01")))
    cur.execute("""select coalesce(sum(profit_loss),0) from broker_closed_positions where account_id = any(%s) and close_origin is distinct from 'Correction'""", (accounts,))
    report["realized_broker_total"] = str(cur.fetchone()[0].quantize(Decimal("0.01")))
    return report


def rebuild_fifo(conn, broker_code="TR"):
    """Partie dla brokerow BEZ numeru pozycji w eksporcie (Trade Republic): kazdy zakup = partia, sprzedaz zamyka partie metoda FIFO (najstarsze najpierw).
    Koszt partii = gotowka z ksiegi (z oplata), przychod = wplyw ze sprzedazy (po oplacie); zrealizowany wynik = przychod - koszt zamknietej czesci.
    Przeliczane od zera z ksiegi (jak rebuild); stosowane tylko korekty CONFIRMED."""
    cur = conn.cursor()
    cur.execute("select broker_id from financial_institutions where code=%s", (broker_code,))
    broker = cur.fetchone()[0]
    cur.execute("select account_id from accounts where broker_id=%s and tracking_mode='FULL'", (broker,))
    accounts = [r[0] for r in cur.fetchall()]
    report = {"lots": 0, "closures": 0, "warnings": [], "realized_ledger_total": "0.00"}
    cur.execute("delete from derived_lot_closures where lot_id in (select lot_id from derived_lots where account_id = any(%s))", (accounts,))
    cur.execute("delete from derived_lots where account_id = any(%s)", (accounts,))
    cur.execute("""select action_id, listing_id, ratio_new/ratio_old, coalesce(applies_to_lots_opened_before, effective_date::timestamptz) from corporate_actions where status='CONFIRMED'""")
    actions = defaultdict(list)
    for aid, lst, factor, cutoff in cur.fetchall():
        actions[lst].append((aid, factor, cutoff))
    cur.execute("""select txn_id, account_id, listing_id, txn_type, occurred_at, quantity, price, cash_amount from transactions
                   where account_id = any(%s) and txn_type in ('BUY','SELL') and status='ACTIVE' order by occurred_at, txn_id""", (accounts,))
    queues = defaultdict(list)                                   # (konto, notowanie) -> [partie z pozostala iloscia]
    all_lots = []
    realized = Decimal(0)
    for tid, acct, lst, typ, when, qty, price, cash in cur.fetchall():
        if typ == "BUY":
            ids, factor = [], Decimal(1)
            for aid, f, cutoff in actions.get(lst, []):
                if cutoff is not None and when < cutoff:
                    ids.append(aid); factor *= f
            q_init, p_adj, cost = qty * factor, (price / factor).quantize(Q8), -cash
            cur.execute("""insert into derived_lots(account_id,listing_id,open_txn_id,opened_at,qty_as_reported,price_as_reported,split_factor,qty_initial,price_adjusted,
                                                    qty_remaining,cost_cash_initial,method,engine_version,adjustment_action_ids)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'FIFO','derive-fifo-1.0',%s) returning lot_id""",
                        (acct, lst, tid, when, qty, price, factor, q_init, p_adj, q_init, cost, ids))
            lot = {"lot_id": cur.fetchone()[0], "q_init": q_init, "left": q_init, "cost": cost}
            queues[(acct, lst)].append(lot); all_lots.append(lot)
            report["lots"] += 1
            continue
        q_sell_adj = qty                                          # sprzedaz po dacie korekty jest juz w nowych jednostkach (ilosc z eksportu)
        queue = queues[(acct, lst)]
        remaining_sale = q_sell_adj
        proceeds_total = cash
        while remaining_sale > Decimal("0.000000005") and queue:
            lot = queue[0]
            take = min(lot["left"], remaining_sale)
            share_of_sale = take / q_sell_adj
            cost_part = (lot["cost"] * take / lot["q_init"]).quantize(Q8)
            proceeds = (proceeds_total * share_of_sale).quantize(Q8)
            cur.execute("""insert into derived_lot_closures(lot_id,sell_txn_id,qty_closed,cost_cash,proceeds_cash,realized_pnl) values (%s,%s,%s,%s,%s,%s)""",
                        (lot["lot_id"], tid, take, cost_part, proceeds, proceeds - cost_part))
            realized += proceeds - cost_part
            lot["left"] -= take; remaining_sale -= take; report["closures"] += 1
            if lot["left"] <= Decimal("0.000000005"):
                queue.pop(0)
        if remaining_sale > Decimal("0.00000001"):
            report["warnings"].append(f"Sprzedaż (txn {tid}) przekracza zakupy w księdze o {remaining_sale} – brak partii do zamknięcia (niepełna historia?).")
    for lot in all_lots:
        cur.execute("update derived_lots set qty_remaining=%s where lot_id=%s", (max(lot["left"], Decimal(0)), lot["lot_id"]))
    report["realized_ledger_total"] = str(realized.quantize(Decimal("0.01")))
    return report
