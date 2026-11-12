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
    """Czy na porcie dziala NASZA aplikacja (a nie cos innego) na TYCH SAMYCH danych? Zwraca wersje albo None.
    Aplikacja na innym katalogu danych (np. zainstalowana, gdy uruchamiamy wersje z innym --data-dir) nie jest "nasza"."""
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{port}/zdrowie", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if body.get("aplikacja") != APP_NAME:
        return None
    if body.get("dane_id") is not None and body.get("dane_id") != config.data_id():      # wersje sprzed 0.3.0 nie podaja dane_id
        return None
    return body.get("wersja")


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


APP_ID = "MojPortfel.App"            # identyfikator aplikacji Windows (pasek zadan: ikona i grupowanie jak skrot z instalatora)
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "static", "ikona.ico")


def has_webview():
    """Czy dostepne jest wlasne okno aplikacji (pywebview + WebView2, silnik Edge wbudowany w Windows 10/11)."""
    try:
        import webview  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def choose_mode(a, webview_available):
    """'okno' (wlasne okno programu), 'przegladarka' albo 'bez-okna' (--no-browser: tylko serwer, np. testy)."""
    if a.no_browser:
        return "bez-okna"
    if a.przegladarka or not webview_available:
        return "przegladarka"
    return "okno"


def show_running(port, mode):
    """Aplikacja juz dziala: przenies jej okno na wierzch (tryb okna) albo otworz karte przegladarki."""
    if mode == "okno":
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)     # ASFW_ANY: dzialajaca aplikacja moze wyjsc na wierzch
            except Exception:  # noqa: BLE001
                pass
        try:
            req = urllib.request.Request(f"http://{config.HOST}:{port}/api/pokaz", data=b"{}", method="POST",
                                         headers={"X-Requested-With": "invapp", "Content-Type": "application/json"})
            if json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8")).get("okno"):
                return
        except Exception:  # noqa: BLE001
            pass
    if mode != "bez-okna":
        webbrowser.open(f"http://{config.HOST}:{port}/")


def handle_running(mode):
    """Aplikacja na tych danych juz dziala? Ta sama wersja -> pokaz ja (okno na wierzch / karta) i zakoncz (True).
    INNA wersja (np. starsza po aktualizacji albo wersja z folderu z kodem) -> zatrzymaj ja i uruchom te (False) - inaczej nowa wersja
    podlaczylaby sie do starej i pokazala jej strone w przegladarce."""
    running = running_port()
    if running is None:
        return False
    other = our_app_running(running)
    if other == __version__:
        print("Aplikacja już działa – pokazuję ją.")
        show_running(running, mode)
        return True
    print(f"Działa inna wersja aplikacji ({other}) – zamykam ją i uruchamiam {__version__}.")
    stop_running()
    return False


def windows_dark_mode():
    """Ciemny motyw aplikacji w Windows (tlo okna przed zaladowaniem strony - bez blysku innego koloru)."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _app_user_model_id():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass


def run_window(app, server, url, port):
    """Serwer w watku pobocznym, okno w glownym (wymog pywebview). Zamkniecie okna = czyste zatrzymanie serwera i bazy.
    Zwraca False, gdy okna nie dalo sie otworzyc (wtedy wywolujacy otwiera przegladarke, a serwer dziala dalej)."""
    import webview
    th = threading.Thread(target=server.run, name="serwer", daemon=True)
    th.start()
    for _ in range(240):                               # pierwszy start (tworzenie bazy) moze potrwac
        if our_app_running(port) or not th.is_alive():
            break
        time.sleep(0.5)
    if not th.is_alive():
        return True                                    # serwer nie wystartowal (blad w dzienniku) - nie ma czego pokazywac
    try:
        window = webview.create_window(APP_NAME, url, width=1360, height=900, min_size=(960, 640), text_select=True, zoomable=True,
                                       maximized=True,                       # domyslnie na caly ekran (z paskiem tytulu i przyciskiem zamkniecia)
                                       background_color="#0b1120" if windows_dark_mode() else "#f4f6fa")
        app.state.window = window
        webview.settings["ALLOW_DOWNLOADS"] = True              # eksport do Excela: okno "Zapisz jako" (plik zapisuje sie tylko tam, gdzie wskaze uzytkownik)
        storage = config.data_dir() / "okno"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(private_mode=False, storage_path=str(storage), icon=ICON if os.path.exists(ICON) else None)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        app.state.window = None
        print("Nie udało się otworzyć okna aplikacji – otwieram w przeglądarce.")
        webbrowser.open(url)
        th.join()                                      # dalej jak w trybie przegladarki: do /api/zamknij (przycisk na stronie)
        return True
    server.should_exit = True                          # okno zamkniete -> koniec aplikacji
    th.join(60)
    return True


def main():
    ap = argparse.ArgumentParser(prog="invapp", description=APP_NAME)
    ap.add_argument("--no-browser", action="store_true", help="bez okna i bez przegladarki (sam serwer)")
    ap.add_argument("--przegladarka", action="store_true", help="otworz w przegladarce zamiast we wlasnym oknie")
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
    mode = choose_mode(a, has_webview())
    if handle_running(mode):
        return
    _app_user_model_id()
    port = a.port
    while not port_free(port):                     # port zajety przez inny program - nastepny wolny
        port += 1
    url = f"http://{config.HOST}:{port}/"
    print(f"{APP_NAME} {__version__}\nAdres: {url} (tylko na tym komputerze), tryb: {mode}\nDane: {config.data_dir()}\n"
          "Pierwsze uruchomienie przygotowuje bazę (kilka sekund).")
    if mode == "przegladarka":
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
        app.state.server = server                  # /api/zamknij: czyste zatrzymanie z okna, przegladarki albo z instalatora
        state_file().write_text(json.dumps({"port": port, "pid": os.getpid(), "wersja": __version__, "tryb": mode}), encoding="utf-8")
        try:
            if mode == "okno":
                run_window(app, server, url, port)
            else:
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
