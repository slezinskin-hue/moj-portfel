"""
Import z interfejsu: uzytkownik wgrywa eksporty (dowolny broker z listy invapp.brokers.BROKERS, w dowolnej kolejnosci), aplikacja
  1) rozpoznaje brokera po nazwie i poczatku pliku, waliduje plik,
  2) importuje go (atomowo, idempotentnie; konta zakladaja sie same) i przelicza warstwe pochodna,
  3) w tle: kroki cenowe kazdego brokera (specyfikacje, powiazania symboli), notowania i kursy na zywo, historia, NBP.
Zapis ksiegi odbywa sie wylacznie tu (rola wlasciciela); strony i API odczytu uzywaja roli aplikacji (tylko odczyt).
Wgrany plik jest zapisywany tylko w katalogu tymczasowym na czas importu.
"""
import os, re, tempfile, threading, time, traceback
from datetime import datetime, timezone

from invapp import brokers
from invapp.core.common import ImportFailed
from invapp.db import engine


def owner_conn():
    """Polaczenie roli wlasciciela (zapis ksiegi)."""
    return engine.connect("owner")


def import_path(conn, path, display_name=None):
    """Import pliku z dysku przez pasujacego brokera (bez commitu - robi go wywolujacy)."""
    name = os.path.basename(path)
    with open(path, "rb") as f:
        head = f.read(2048)
    b = brokers.for_upload(name, head)
    if b is None:
        raise ImportFailed(unsupported_message())
    rep = b.import_path(conn, path, display_name)
    rep["broker"] = b.code
    rep.setdefault("created_accounts", [])
    return rep


def unsupported_message():
    return "Nie rozpoznaję tego pliku. Obsługiwane eksporty: " + "; ".join(
        f"{b.name} ({', '.join(b.accept)})" for b in brokers.BROKERS) + ". Wgraj oryginalny plik pobrany od brokera."


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    return name if re.fullmatch(r"[\w.\- ()]{1,120}", name) and not name.startswith(".") else None     # litery (takze polskie), cyfry, . _ - spacja i nawiasy ("(2)" od przegladarki)


_import_lock = threading.Lock()


def run_import_bytes(data, filename):
    """Punkt wejscia dla API: walidacja, zapis w katalogu tymczasowym pod ORYGINALNA nazwa (np. nazwa XTB niesie numer konta), import, commit, sprzatanie.
    Importy sa wykonywane PO KOLEI (blokada): dwa pliki wgrane jednoczesnie (np. ponowne klikniecie albo druga karta) zakladalyby te same konta
    rownolegle i jeden z nich konczyl sie bledem unikalnosci."""
    with _import_lock:
        return _run_import_bytes(data, filename)


def _run_import_bytes(data, filename):
    name = safe_name(filename)
    if not name:
        raise ImportFailed("Nieprawidłowa nazwa pliku. " + unsupported_message())
    b = brokers.for_upload(name, data[:2048])
    if b is None:
        raise ImportFailed(unsupported_message())
    b.validate_upload(name, data)
    tmpdir = tempfile.mkdtemp(prefix="invapp_upload_")
    path = os.path.join(tmpdir, name)
    try:
        with open(path, "wb") as f:
            f.write(data)
        conn = owner_conn()
        try:
            rep = import_path(conn, path, name)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()
        return rep
    finally:
        try:
            os.remove(path); os.rmdir(tmpdir)
        except OSError:
            pass


def remove_import(batch_id, make_backup=True):
    """Usuniecie wczytanego pliku (core/removal.py): po kolei z importami, z kopia zapasowa bazy przed zmiana."""
    from invapp.core import removal
    with _import_lock:
        if make_backup:
            from invapp.db import backup
            backup.create(label="przed-usunieciem-pliku")
        conn = owner_conn()
        try:
            res = removal.remove_batch(conn, batch_id)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()
        return res


# ---------------------------------------------------------------- pytania brokerow (np. waluta instrumentow .PL)
def pending_questions(conn):
    out = []
    for b in brokers.BROKERS:
        for q in b.pending_questions(conn):
            out.append({**q, "broker": b.key})
    return out


def answer_question(conn, broker_key, question_id, answer, fields=None):
    b = brokers.by_key(broker_key)
    if b is None:
        raise ImportFailed("Nieznany broker.")
    return b.answer_question(conn, question_id, answer, fields or {})


# ---------------------------------------------------------------- kontrahenci (czy to Twoj rachunek?) - API zachowane, nieuzywane w widoku
def pending_counterparties(conn):
    cur = conn.cursor()
    cur.execute("""select c.counterparty_id, c.scheme, c.value, count(fc.txn_id), coalesce(sum(t.cash_amount), 0), min(t.occurred_at), max(t.occurred_at),
                          coalesce(c.evidence->>'channel', '')
                   from counterparties c join flow_classification fc on fc.counterparty_id = c.counterparty_id and fc.is_current
                   join transactions t on t.txn_id = fc.txn_id
                   where c.ownership_status in ('UNKNOWN','OWN_CANDIDATE') group by c.counterparty_id order by c.counterparty_id""")
    return [{"id": r[0], "scheme": r[1], "value": r[2], "operations": r[3], "amount": r[4], "first": r[5], "last": r[6], "channel": r[7]} for r in cur.fetchall()]


def decide_counterparty(conn, counterparty_id, own):
    cur = conn.cursor()
    cur.execute("select scheme, value, ownership_status from counterparties where counterparty_id = %s", (counterparty_id,))
    row = cur.fetchone()
    if not row:
        raise ImportFailed("Nie ma takiego kontrahenta.")
    scheme, value, status = row
    if status not in ("UNKNOWN", "OWN_CANDIDATE"):
        raise ImportFailed("Ta decyzja została już podjęta.")
    cur.execute("select broker_id from financial_institutions where code = 'XTB'"); broker = cur.fetchone()[0]
    acct = None
    if own:
        ext = f"BA:{value}" if scheme == "XTB_PAYMENT_BA" else value
        cur.execute("select account_id from accounts where broker_id = %s and external_no = %s", (broker, ext)); r = cur.fetchone()
        if r:
            acct = r[0]
        else:
            cur.execute("""insert into accounts(broker_id,external_no,external_no_source,display_name,product_type,account_class,tracking_mode,tax_regime)
                           values (%s,%s,'USER','Rachunek płatniczy XTB (eWallet / karta)','PAYMENT','PAYMENT','TRANSFERS_ONLY','NONE') returning account_id""", (broker, ext))
            acct = cur.fetchone()[0]
            cur.execute("insert into account_identifiers(account_id,scheme,value) values (%s,%s,%s) on conflict do nothing", (acct, scheme, value))
            cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = 'NET_WORTH' on conflict do nothing", (acct,))
    cur.execute("""update counterparties set ownership_status = %s, mapped_account_id = %s, decided_by = 'user', decided_at = now(),
                          evidence = coalesce(evidence, '{}'::jsonb) || jsonb_build_object('decision', %s::text, 'decided_in', 'aplikacja') where counterparty_id = %s""",
                ("OWN_CONFIRMED" if own else "EXTERNAL_CONFIRMED", acct, "mój rachunek" if own else "cudzy rachunek", counterparty_id))
    cls = "OWN_TRANSFER_UNTRACKED" if own else "EXTERNAL"
    cur.execute("""update flow_classification set is_current = false where counterparty_id = %s and is_current""", (counterparty_id,))
    cur.execute("""insert into flow_classification(txn_id,counterparty_id,counterparty_account_id,flow_class,method,confidence,decided_by,classifier_version)
                   select fc.txn_id, %s, %s, %s, 'L3_COUNTERPARTY_REGISTRY', 'HIGH', 'user (aplikacja)', 'app-1.0' from flow_classification fc
                   where fc.counterparty_id = %s and not fc.is_current and fc.classification_id = (select max(f2.classification_id) from flow_classification f2 where f2.txn_id = fc.txn_id)""",
                (counterparty_id, acct, cls, counterparty_id))
    return {"counterparty": value, "own": own, "account_created": acct is not None}


# ---------------------------------------------------------------- zadanie w tle: kroki brokerow, notowania, historia, NBP
STATE = {"state": "idle", "step": "", "log": [], "started": None, "finished": None, "error": None, "rerun": False,
         "step_no": 0, "steps": 0, "version": 0}          # version rosnie po kazdym kroku zmieniajacym ceny - strona odswieza wtedy dane (bez przeladowania)
_state_lock = threading.Lock()
_last_retry = {"t": 0.0}


def _set(**kw):
    with _state_lock:
        STATE.update(kw)


def _log(msg):
    with _state_lock:
        STATE["log"] = (STATE["log"] + [f"{datetime.now().strftime('%H:%M:%S')} {msg}"])[-40:]


def _phase_steps(conn, provider):
    out = {"quick": [], "slow": [], "history": []}
    for b in brokers.BROKERS:
        for s in b.pricing_steps(conn, provider):
            title, fn, phase = (s + ("slow",))[:3]
            out.get(phase, out["slow"]).append((title, fn))
    return out


def _pricing_pass():
    """Jeden przebieg, w kolejnosci od tego, co uzytkownik chce zobaczyc najszybciej:
    szybkie kroki brokerow (powiazania symboli) -> notowania na zywo -> wolne kroki (specyfikacje) -> notowania dla nowych -> historia -> NBP.
    Blad kroku nie przerywa pozostalych."""
    from invapp.pricing import history, service
    from invapp.pricing.providers import NbpProvider, YahooProvider
    conn = owner_conn()
    try:
        prov = YahooProvider()
        ph = _phase_steps(conn, prov)
        quotes = ("Notowania i kursy na żywo", lambda: service.refresh_quotes(conn, YahooProvider()), True)
        steps = [(t, f, False) for t, f in ph["quick"]] + [quotes]
        if ph["slow"]:
            steps += [(t, f, False) for t, f in ph["slow"]] + [("Notowania dla uzupełnionych instrumentów", quotes[1], True)]
        steps += [(t, f, False) for t, f in ph["history"]]
        steps += [("Ceny historyczne i kursy dzienne", lambda: history.refresh_history(conn, history.HistoryYahoo()), True),
                  ("Kursy NBP", lambda: service.refresh_nbp(conn, NbpProvider()), True)]
        _set(steps=len(steps))
        for i, (title, fn, bumps) in enumerate(steps, 1):
            _set(step=title, step_no=i); _log("start: " + title)
            try:
                res = fn(); conn.commit()
                _log(f"gotowe: {title}" + (f" ({len(res)} pozycji)" if isinstance(res, list) else ""))
                if bumps:
                    from invapp.web import dashboard
                    dashboard.invalidate_history()
                    with _state_lock:
                        STATE["version"] += 1
            except Exception as e:  # noqa: BLE001
                conn.rollback(); _log(f"pominięto ({type(e).__name__}): {title}")
    finally:
        conn.close()


def background_pricing(run_once=None):
    """Zadanie w tle. Jesli w trakcie przebiegu wczytano kolejny plik (nowe instrumenty), przebieg jest POWTARZANY."""
    run_once = run_once or _pricing_pass
    _set(state="running", step="start", started=datetime.now(timezone.utc).isoformat(), finished=None, error=None, log=[], rerun=False)
    try:
        while True:
            with _state_lock:
                STATE["rerun"] = False
            run_once()
            with _state_lock:
                if not STATE["rerun"]:
                    break
            _log("wczytano kolejne dane w trakcie - powtarzam pobieranie dla nowych instrumentów")
        _set(state="done", step="", finished=datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001
        _set(state="error", error=type(e).__name__, finished=datetime.now(timezone.utc).isoformat())
        traceback.print_exc()


def start_background_pricing():
    with _state_lock:
        if STATE["state"] == "running":
            STATE["rerun"] = True
            return False
    threading.Thread(target=background_pricing, daemon=True).start()
    return True


def retry_missing_prices(min_interval=300):
    """Pozycje bez ceny rynkowej: ponawia pobieranie w tle, nie czesciej niz co min_interval s i nie w trakcie przebiegu."""
    now = time.time()
    with _state_lock:
        if STATE["state"] == "running" or now - _last_retry["t"] < min_interval:
            return False
        _last_retry["t"] = now
    return start_background_pricing()


def has_imports():
    conn = owner_conn()
    try:
        cur = conn.cursor(); cur.execute("select exists (select 1 from import_batches)")
        return cur.fetchone()[0]
    finally:
        conn.close()


def start_pricing_on_launch():
    """Przy starcie: jesli sa juz wczytane dane, uzupelnia symbole i ceny. Na pustej bazie nic nie pobiera."""
    try:
        if has_imports():
            start_background_pricing()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
