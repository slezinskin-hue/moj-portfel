"""
Start aplikacji:  python -m invapp  [--no-browser] [--port 8000] [--data-dir FOLDER] [--stop]
Uruchamia wbudowana baze (pierwszy raz: inicjalizacja), serwer WWW na 127.0.0.1 i otwiera przegladarke.
Gdy aplikacja juz dziala, tylko otwiera przegladarke. Zatrzymanie: przycisk "Zamknij aplikacje" na stronie, zamkniecie okna konsoli, Ctrl+C
albo `python -m invapp --stop` (uzywa tego instalator przed aktualizacja).
Wersja z instalatora dziala bez okna konsoli (pythonw): komunikaty trafiaja do dziennika w katalogu danych\\logi, bledy - do okienka Windows.
"""
import argparse, json, os, socket, sys, threading, time, traceback, urllib.request, webbrowser

from invapp import APP_NAME, __version__, config

REQUIRED = {"fastapi": "fastapi", "uvicorn": "uvicorn", "jinja2": "jinja2", "psycopg2": "psycopg2-binary", "openpyxl": "openpyxl",
            "pypdf": "pypdf", "pgserver": "pgserver"}
NO_CONSOLE = sys.stdout is None or sys.stderr is None          # pythonw.exe (skrot z instalatora)


def _redirect_output():
    """Bez konsoli: wyjscie i bledy do pliku dziennika (inaczej print/uvicorn zakonczylyby proces bledem)."""
    log = open(config.logs_dir() / "aplikacja.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log


def message_box(text, title=APP_NAME):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    print(text)


def pause_and_exit(message, code=1):
    print("\n" + message)
    if NO_CONSOLE:
        message_box(message)
    elif sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nNaciśnij Enter, aby zamknąć to okno...")
        except EOFError:
            pass
    sys.exit(code)


def check_dependencies():
    missing = []
    for module, package in REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        pause_and_exit("BRAKUJE BIBLIOTEK: " + ", ".join(missing) +
                       f'\n\nZainstaluj je poleceniem (w folderze aplikacji):\n\n    "{sys.executable}" -m pip install -r requirements.txt')


def our_app_running(port):
    """Czy na porcie dziala NASZA aplikacja (a nie cos innego)? Zwraca wersje albo None."""
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{port}/zdrowie", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8"))
            return body.get("wersja") if body.get("aplikacja") == APP_NAME else None
    except Exception:  # noqa: BLE001
        return None


def port_free(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, port)) != 0


def state_file():
    return config.data_dir() / "stan.json"


def running_port():
    """Port dzialajacej aplikacji (z pliku stanu), jesli naprawde odpowiada."""
    try:
        port = int(json.loads(state_file().read_text(encoding="utf-8"))["port"])
    except Exception:  # noqa: BLE001
        return None
    return port if our_app_running(port) else None


def stop_running(timeout=60):
    """Zatrzymuje dzialajaca aplikacje (czysto: serwer WWW i baza). Zwraca True, gdy nic juz nie dziala."""
    port = running_port()
    if port is None:
        return True
    try:
        req = urllib.request.Request(f"http://{config.HOST}:{port}/api/zamknij", data=b"{}", method="POST",
                                     headers={"X-Requested-With": "invapp", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass
    for _ in range(timeout * 2):
        if port_free(port):
            time.sleep(1.0)                                    # chwila na zatrzymanie bazy po zamknieciu serwera WWW
            return True
        time.sleep(0.5)
    return False


_console_handler = None


def install_console_close_handler():
    """Windows: zamkniecie okna konsoli (X), wylogowanie, wylaczenie komputera -> najpierw czyste zatrzymanie bazy (pg_ctl stop -m fast).
    Ctrl+C obsluguje serwer WWW (zdarzenie shutdown aplikacji)."""
    global _console_handler
    if os.name != "nt" or NO_CONSOLE:
        return
    import ctypes
    from invapp.db import engine

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    def handler(event):
        if event in (2, 5, 6):            # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            try:
                engine.stop()
            except Exception:  # noqa: BLE001
                pass
            return 1
        return 0
    _console_handler = handler            # referencja musi zyc tak dlugo jak proces
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


def main():
    ap = argparse.ArgumentParser(prog="invapp", description=APP_NAME)
    ap.add_argument("--no-browser", action="store_true", help="nie otwieraj przegladarki")
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    ap.add_argument("--data-dir", help="inny katalog danych (np. do prob)")
    ap.add_argument("--stop", action="store_true", help="zatrzymaj dzialajaca aplikacje i zakoncz")
    a = ap.parse_args()
    if a.data_dir:
        os.environ[config.DATA_DIR_ENV] = a.data_dir
    if NO_CONSOLE:
        _redirect_output()
    if a.stop:
        sys.exit(0 if stop_running() else 1)
    check_dependencies()
    running = running_port()
    if running is not None:
        print("Aplikacja już działa – otwieram przeglądarkę.")
        if not a.no_browser:
            webbrowser.open(f"http://{config.HOST}:{running}/")
        return
    port = a.port
    while not port_free(port):                     # port zajety przez inny program - nastepny wolny
        port += 1
    url = f"http://{config.HOST}:{port}/"
    print(f"{APP_NAME} {__version__}\nAdres: {url} (tylko na tym komputerze)\nDane: {config.data_dir()}\n"
          "Pierwsze uruchomienie przygotowuje bazę (kilka sekund). Zamknięcie tego okna albo Ctrl+C zatrzymuje aplikację i bazę.")
    if not a.no_browser:
        def open_when_ready():
            for _ in range(240):
                if our_app_running(port):
                    webbrowser.open(url); return
                time.sleep(0.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    install_console_close_handler()
    try:
        import uvicorn
        from invapp.web.app import app
        server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=port, log_level="warning", log_config=None))
        app.state.server = server                  # /api/zamknij: czyste zatrzymanie z przegladarki albo z instalatora
        state_file().write_text(json.dumps({"port": port, "pid": os.getpid(), "wersja": __version__}), encoding="utf-8")
        try:
            server.run()
        finally:
            try:
                state_file().unlink()
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        pause_and_exit(f"Aplikacja zatrzymała się z błędem ({type(e).__name__}). Szczegóły: {config.logs_dir() / 'aplikacja.log'}")


if __name__ == "__main__":
    main()
