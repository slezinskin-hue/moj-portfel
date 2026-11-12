"""
Wbudowana baza PostgreSQL: pliki serwera PostgreSQL 16 dolaczone do programu (pakiet pgserver - z niego bierzemy tylko binarki),
bez instalacji i bez uprawnien administratora. Serwerem zarzadza ten modul (pg_ctl), a nie pgserver, bo:
* dziennik serwera musi byc POZA katalogiem danych - na Windows plik dziennika otwarty przez pg_ctl blokowal odtwarzanie bazy po nieczystym
  zamknieciu (Ctrl+C, zamkniecie okna): serwer czekal 30 s na zablokowany plik, a start konczyl sie bledem po 10 s;
* start czeka na odtworzenie bazy dluzej (do 3 min), a zatrzymanie jest szybkie i czyste (pg_ctl stop -m fast).
Zasady:
* dane w katalogu danych uzytkownika (config.pgdata_dir); serwer nasluchuje tylko na 127.0.0.1, na wolnym porcie;
* pierwsze uruchomienie: initdb od razu z uwierzytelnianiem scram-sha-256 (bez okresu "trust"), role investing_owner (zapis ksiegi)
  i investing_app (odczyt) z LOSOWYMI haslami zapisanymi w katalogu danych (uzytkownik nic nie wpisuje);
* kazde uruchomienie: migracje (przed migracja istniejacej bazy - kopia zapasowa).
"""
import atexit, json, os, secrets, socket, subprocess, tempfile, threading, time
from pathlib import Path

import psycopg2

from invapp import config
from invapp.db import migrate

DB_NAME = "investing"
OWNER, APP = "investing_owner", "investing_app"
START_TIMEOUT = 180                     # s: po nieczystym zamknieciu serwer odtwarza baze (zwykle < 1 s, na wolnym dysku dluzej)
_lock = threading.Lock()
_state = {"server": None, "host": None, "port": None, "secrets": None, "started_here": False, "stop_on_exit": False}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def bin_path(name):
    """Sciezka do programu PostgreSQL (pg_ctl, initdb, pg_dump...) z pakietu pgserver."""
    import pgserver
    exe = Path(pgserver.__file__).with_name("pginstall") / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(exe)


def _run(args, timeout=60, check=True, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check, env=env, creationflags=_NO_WINDOW)


def _load_secrets():
    f = config.secrets_file()
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _write_secrets(data):
    config.secrets_file().write_text(json.dumps(data, indent=2), encoding="utf-8")


HBA = "# zarzadzane przez aplikacje\nhost all all 127.0.0.1/32 scram-sha-256\nhost all all ::1/128 scram-sha-256\n"


def _free_port():
    with socket.socket() as s:
        s.bind((config.HOST, 0))
        return s.getsockname()[1]


def _postmaster(pgdata):
    """(pid, port, status) z postmaster.pid albo None."""
    f = Path(pgdata) / "postmaster.pid"
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        return int(lines[0]), int(lines[3]), (lines[7].strip() if len(lines) > 7 else "")
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid):
    """Czy proces o tym numerze to dzialajacy serwer PostgreSQL (nieaktualny postmaster.pid po awarii -> False)."""
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and "postgres" in p.name().lower()
    except Exception:  # noqa: BLE001
        return False


def _accepts(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((config.HOST, port)) == 0


def is_running(pgdata=None):
    pgdata = Path(pgdata or config.pgdata_dir())
    if not (pgdata / "PG_VERSION").exists():
        return False
    return _run([bin_path("pg_ctl"), "status", "-D", str(pgdata)], check=False).returncode == 0


def _initdb(pgdata, postgres_password):
    pgdata.mkdir(parents=True, exist_ok=True)
    fd, pw = tempfile.mkstemp(prefix="invapp_pw_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(postgres_password + "\n")
        _run([bin_path("initdb"), "-D", str(pgdata), "-U", "postgres", "--auth=scram-sha-256", f"--pwfile={pw}", "--encoding=UTF8"], timeout=300)
    finally:
        os.remove(pw)
    (pgdata / "pg_hba.conf").write_text(HBA, encoding="utf-8")


def _start_server(pgdata):
    """Uruchamia serwer (albo podlacza sie do dzialajacego na tym katalogu danych). Zwraca (port, czy_uruchomiony_tutaj)."""
    pm = _postmaster(pgdata)
    if pm and _alive(pm[0]):                              # serwer dziala (albo jeszcze odtwarza baze) - czekamy, az przyjmuje polaczenia
        for _ in range(START_TIMEOUT * 2):
            pm = _postmaster(pgdata)
            if pm is None or not _alive(pm[0]):
                break                                     # zakonczyl sie w miedzyczasie - uruchomimy go ponizej
            if pm[2] == "ready" and _accepts(pm[1]):
                return pm[1], False
            time.sleep(0.5)
        else:
            raise RuntimeError("Baza danych uruchomiona przez inny proces nie odpowiada.")
    old_log = pgdata / "log"                              # dziennik z wersji, w ktorej serwerem zarzadzal pgserver (blokowal odtwarzanie)
    if old_log.exists():
        try:
            old_log.unlink()
        except OSError:
            pass
    port = _free_port()
    log = config.logs_dir() / "baza.log"
    # bez potokow stdout/stderr: serwer dziedziczylby potok i subprocess czekalby na jego zamkniecie w nieskonczonosc
    rc = subprocess.run([bin_path("pg_ctl"), "start", "-D", str(pgdata), "-l", str(log), "-w", "-t", str(START_TIMEOUT),
                         "-o", f"-h {config.HOST} -p {port}" + ("" if os.name == "nt" else ' -k ""')], stdin=subprocess.DEVNULL,   # tylko TCP (bez gniazd unix) stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=START_TIMEOUT + 30, creationflags=_NO_WINDOW).returncode
    if rc != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
        raise RuntimeError(f"Nie udało się uruchomić bazy danych (pg_ctl {rc}). Dziennik: {log}\n{tail}")
    # po awarii pg_ctl potrafi wrocic, zanim nowy serwer nadpisze stary postmaster.pid - czekamy na NASZ port
    for _ in range(START_TIMEOUT * 2):
        pm = _postmaster(pgdata)
        if pm and pm[1] == port and pm[2] == "ready" and _accepts(port):
            return port, True
        time.sleep(0.5)
    raise RuntimeError(f"Baza danych nie zaczęła przyjmować połączeń. Dziennik: {log}")


def stop():
    """Zatrzymuje serwer, jesli ten proces go uruchomil (albo przejal przy starcie aplikacji). Bezpieczne do wielokrotnego wywolania."""
    with _lock:
        if _state["server"] is None:
            return
        pgdata = _state["server"]
        mine = _state["started_here"] or _state["stop_on_exit"]
        _state.update(server=None, host=None, port=None, secrets=None, started_here=False, stop_on_exit=False)
    if mine:
        try:
            _run([bin_path("pg_ctl"), "stop", "-D", str(pgdata), "-m", "fast", "-w", "-t", "60"], timeout=90, check=False)
        except Exception:  # noqa: BLE001
            pass


atexit.register(stop)


def _ensure_roles(sec):
    c = psycopg2.connect(host=_state["host"], port=_state["port"], dbname="postgres", user="postgres", password=sec["postgres"], connect_timeout=10)
    c.autocommit = True
    try:
        cur = c.cursor()
        for role in (OWNER, APP):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            cur.execute(f"{'ALTER' if cur.fetchone() else 'CREATE'} ROLE {role} LOGIN PASSWORD %s", (sec[role],))
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {DB_NAME} OWNER {OWNER} ENCODING 'UTF8' TEMPLATE template0")
    finally:
        c.close()


def _legacy_first_run(sec_new):
    """Katalog danych utworzony przez starsza wersje (pgserver: initdb z 'trust') bez pliku hasel: ustaw hasla i scram."""
    c = psycopg2.connect(host=_state["host"], port=_state["port"], dbname="postgres", user="postgres", connect_timeout=10)
    c.autocommit = True
    try:
        c.cursor().execute("ALTER ROLE postgres PASSWORD %s", (sec_new["postgres"],))
        _state["secrets"] = sec_new
        _write_secrets(sec_new)
    finally:
        c.close()
    _ensure_roles(sec_new)
    (Path(_state["server"]) / "pg_hba.conf").write_text(HBA, encoding="utf-8")
    c = psycopg2.connect(host=_state["host"], port=_state["port"], dbname="postgres", user="postgres", password=sec_new["postgres"], connect_timeout=10)
    c.autocommit = True
    c.cursor().execute("SELECT pg_reload_conf()"); c.close()


def start(stop_on_exit=False):
    """Uruchamia (albo podlacza sie do dzialajacego) wbudowany serwer i przygotowuje baze. Idempotentne.
    stop_on_exit=True (aplikacja): zatrzymaj serwer przy wyjsciu takze wtedy, gdy byl juz uruchomiony (np. pozostal po awarii)."""
    with _lock:
        if _state["server"] is not None:
            _state["stop_on_exit"] = _state["stop_on_exit"] or stop_on_exit
            return _state
        pgdata = config.pgdata_dir()
        sec = _load_secrets()
        fresh = not (pgdata / "PG_VERSION").exists()
        if fresh:
            sec = {"postgres": secrets.token_urlsafe(24), OWNER: secrets.token_urlsafe(24), APP: secrets.token_urlsafe(24)}
            _write_secrets(sec)
            _initdb(pgdata, sec["postgres"])
        port, here = _start_server(pgdata)
        _state.update(server=pgdata, host=config.HOST, port=port, started_here=here, stop_on_exit=stop_on_exit, secrets=sec)
    try:
        if sec is None:
            _legacy_first_run({"postgres": secrets.token_urlsafe(24), OWNER: secrets.token_urlsafe(24), APP: secrets.token_urlsafe(24)})
        elif fresh:
            _ensure_roles(sec)
        conn = connect("owner")
        try:
            todo = migrate.pending(conn)
            cur = conn.cursor(); cur.execute("SELECT count(*) FROM schema_migrations"); had = cur.fetchone()[0]
            if todo and had:                               # aktualizacja programu na istniejacych danych: najpierw kopia
                from invapp.db import backup
                backup.create(label="przed-migracja")
            cur.execute(f"GRANT CONNECT ON DATABASE {DB_NAME} TO {APP}"); cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP}"); conn.commit()
            migrate.apply(conn)
        finally:
            conn.close()
    except Exception:
        stop()
        raise
    return _state


def connect(role="app", readonly=False):
    """Polaczenie z baza aplikacji: role 'app' (odczyt + dopisywanie notowan) albo 'owner' (zapis ksiegi)."""
    if _state["server"] is None:
        start()
    user = APP if role == "app" else OWNER
    conn = psycopg2.connect(host=_state["host"], port=_state["port"], dbname=DB_NAME, user=user, password=_state["secrets"][user], connect_timeout=10)
    if readonly:
        conn.set_session(readonly=True, autocommit=True)
    return conn


def server_info():
    return {"host": _state["host"], "port": _state["port"], "data": str(config.pgdata_dir())}
