"""
Potwierdza korekte korporacyjna (split) na podstawie OFICJALNEGO zrodla (dokument 17 §17.16).

    python -m invapp.tools.confirm_corporate_action --symbol ABC.PL --ratio 10 --date 2025-01-31 --ref "<opis zrodla>" --url "<adres>" [--dry-run]

W jednej transakcji: hipoteza HYPOTHESIS dla notowania i wspolczynnika (dokladnie jedna) -> CONFIRMED / EXTERNAL / data z zrodla oficjalnego,
historia zmian w evidence, ostrzezenie DQ-06 -> RESOLVED, przeliczenie partii brokera tego notowania. Dane surowe bez zmian.
"""
import argparse
from datetime import datetime, timezone

from psycopg2.extras import Json

from invapp import brokers


def confirm(conn, symbol, ratio, date, ref, url=None, note=None):
    cur = conn.cursor()
    cur.execute("""select a.action_id, a.status, a.listing_id, a.evidence, f.code from corporate_actions a
                   join listing_aliases la on la.listing_id = a.listing_id and la.valid_to is null
                   join financial_institutions f on f.broker_id = la.broker_id
                   where la.broker_symbol = %s and a.action_type = 'SPLIT' and a.ratio_new = %s and a.ratio_old = 1 and a.status = 'HYPOTHESIS'""", (symbol, ratio))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise SystemExit(f"Oczekiwano dokladnie jednej hipotezy splitu {ratio}:1 dla {symbol}, znaleziono {len(rows)}. Nic nie zmieniam.")
    action_id, old_status, listing_id, evidence, broker_code = rows[0]
    now = datetime.now(timezone.utc)
    history = list((evidence or {}).get("history", [])) + [{"at": now.isoformat(), "from": old_status, "to": "CONFIRMED", "by": "uzytkownik: sprawdzenie zrodla oficjalnego"}]
    new_evidence = dict(evidence or {}); new_evidence["history"] = history
    new_evidence["official_source"] = {"ref": ref, "url": url, "note": note, "verified_at": now.isoformat()}
    cur.execute("""update corporate_actions set status='CONFIRMED', source='EXTERNAL', effective_date=%s, effective_date_status='FROM_OFFICIAL_SOURCE',
                          applies_to_lots_opened_before=NULL, official_source_status='VERIFIED', official_source_ref=%s, verified_at=%s, evidence=%s
                   where action_id=%s""", (date, ref, now, Json(new_evidence), action_id))
    cur.execute("""update data_quality_issues set status='RESOLVED', resolved_at=now() where code='DQ-06_IMPLICIT_SPLIT' and entity_ref=%s and status='OPEN'""", (f"listing:{listing_id}",))
    report = brokers.by_code(broker_code).rebuild_derived(conn)
    return action_id, report


def main():
    from invapp.db import engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True); ap.add_argument("--ratio", required=True, type=int); ap.add_argument("--date", required=True)
    ap.add_argument("--ref", required=True); ap.add_argument("--url"); ap.add_argument("--note"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = engine.connect("owner")
    try:
        action_id, rep = confirm(conn, a.symbol, a.ratio, a.date, a.ref, a.url, a.note)
        print(f"korekta #{action_id} ({a.symbol}) -> CONFIRMED; partie: {rep.get('lots')}, zamkniecia: {rep.get('closures')}, ostrzezenia: {rep.get('warnings')}")
        if a.dry_run:
            conn.rollback(); print("--dry-run: wycofano.")
        else:
            conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
