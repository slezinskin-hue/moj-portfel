"""
Serwer WWW aplikacji (FastAPI), dostepny wylacznie lokalnie (127.0.0.1). Uruchamianie: python -m invapp (albo uruchom.py).
Strony i odczyt: rola aplikacji (tylko odczyt); import i decyzje uzytkownika: rola wlasciciela, z ochrona przed zadaniami z obcych stron.
"""
import os, threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from invapp import APP_NAME, __version__, brokers, config
from invapp.core.common import ImportFailed
from invapp.db import engine
from invapp.web import dashboard as preview
from invapp.web import importing

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Mój Portfel", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Strona tylko z wlasnych plikow: bez zewnetrznych skryptow, bez osadzania w cudzych stronach, zapytania tylko do tej aplikacji.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _hostname(value):
    from urllib.parse import urlsplit
    try:
        return urlsplit(value if "//" in value else "//" + value).hostname or ""
    except ValueError:
        return ""


@app.middleware("http")
async def _local_only(request: Request, call_next):
    """Bezpieczenstwo (decyzja uzytkownika 2026-11-12: "nikt nie udostepni danych przez przypadek"):
    * serwer slucha tylko na 127.0.0.1 (config.HOST), a dodatkowo przyjmuje wylacznie naglowek Host = 127.0.0.1/localhost - obca strona otwarta
      w przegladarce nie odczyta danych przez podstawienie swojej domeny pod adres 127.0.0.1 (atak "DNS rebinding");
    * zadanie z obcej strony (naglowek Origin innej witryny) jest odrzucane - takze odczyt;
    * naglowki: zakaz osadzania strony w ramce, bez zewnetrznych skryptow (CSP), bez wysylania adresu strony dalej (Referrer-Policy), bez zapisu w pamieci podrecznej."""
    from fastapi.responses import PlainTextResponse
    if _hostname(request.headers.get("host") or "") not in LOCAL_HOSTS:
        return PlainTextResponse("Aplikacja jest dostępna tylko pod adresem 127.0.0.1.", status_code=400)
    origin = request.headers.get("origin")
    if origin and origin != "null" and _hostname(origin) not in LOCAL_HOSTS:
        return PlainTextResponse("Żądanie z obcej strony – odrzucone.", status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.url.path.startswith("/api/") or request.url.path == "/podglad":
        resp.headers["Cache-Control"] = "no-store"
    return resp
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")      # wyglad i wykresy (bez zewnetrznych bibliotek)
STATIC_V = str(int(max(os.path.getmtime(os.path.join(HERE, "static", f)) for f in os.listdir(os.path.join(HERE, "static")))))   # nowa wersja plikow = nowy adres (bez starej kopii w przegladarce)


def db_status():
    """Stan bazy (bez hasel w odpowiedzi). Nigdy nie rzuca wyjatku na zewnatrz."""
    try:
        conn = engine.connect("app", readonly=True)
        try:
            cur = conn.cursor()
            cur.execute("select current_setting('server_version'), (select coalesce(max(version), 0) from schema_migrations)")
            version, migrations = cur.fetchone()
        finally:
            conn.close()
        return {"ok": True, "server_version": version, "last_migration": migrations}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__}


templates.env.filters["pln"] = preview.pln
templates.env.filters["qty"] = preview.qty


@app.on_event("startup")
def _startup():
    """Start: wbudowana baza (inicjalizacja przy pierwszym uruchomieniu, migracje), rejestr brokerow, potem w tle uzupelnienie cen."""
    engine.start(stop_on_exit=True)                    # aplikacja zatrzymuje baze przy wyjsciu, takze gdy zostala po awarii
    conn = engine.connect("owner")
    try:
        brokers.ensure_registered(conn); conn.commit()
    finally:
        conn.close()
    threading.Thread(target=importing.start_pricing_on_launch, daemon=True).start()


@app.on_event("shutdown")
def _shutdown():
    """Ctrl+C / zamkniecie: czyste zatrzymanie bazy (bez odtwarzania przy nastepnym starcie)."""
    engine.stop()


@app.get("/podglad", response_class=HTMLResponse)
def podglad(request: Request, scope: str = "all"):
    """Strona glowna: zakladki XTB / Trade Republic / WSZYSTKIE (parametr scope=xtb|tr|all). Blad bazy -> czytelny komunikat, bez szczegolow technicznych."""
    try:
        data = preview.load(scope)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse(request, "error.html", {"db": {"ok": False, "error": type(e).__name__}}, status_code=503)
    return templates.TemplateResponse(request, "dashboard.html", {"d": type("D", (), data), "app_name": APP_NAME, "version": __version__, "static_v": STATIC_V})


def _json_default(o):
    import datetime, decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    raise TypeError(type(o))


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=_json_default))


def _same_origin(request):
    """Ochrona przed zapisem z obcych stron: wymagany naglowek niestandardowy (przegladarka nie wysle go z formularza innej witryny) i host lokalny."""
    return request.headers.get("x-requested-with") == "invapp" and _hostname(request.headers.get("host") or "") in LOCAL_HOSTS


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/podglad")


@app.post("/api/import")
async def api_import(request: Request):
    """Wgranie eksportu XTB (.xlsx): cialo zadania = bajty pliku, nazwa w naglowku X-Filename (nazwa niesie numer konta). Dowolna kolejnosc i kombinacja plikow."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    data = await request.body()
    from urllib.parse import unquote
    name = unquote(request.headers.get("x-filename", ""))
    try:
        rep = await run_in_threadpool(importing.run_import_bytes, data, name)
    except ImportFailed as e:
        return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": f"Import nie powiódł się ({type(e).__name__}); nic nie zostało zapisane."}, status_code=500)
    preview.invalidate_caches()
    importing.start_background_pricing()
    c = rep["counters"]
    return JSONResponse(_jsonable({"plik": rep["file"], "wiersze": c["read"], "nowe": c["INSERTED"], "duplikaty": c["DUPLICATE"], "konflikty": c["CONFLICT"],
                                   "niezmapowane": c["UNMAPPED"], "zignorowane": c["IGNORED"], "nowe_konta": rep["created_accounts"],
                                   "ostrzezenia": rep["warnings"][:8], "kontrole": [{"kontrola": k["kontrola"], "status": k["status"]} for k in rep["checks"]],
                                   "informacje": rep.get("info", [])[:6], "pary_przelewow": rep.get("transfer_pairs", {})}))


@app.post("/api/import/{batch_id}/usun")
async def api_import_remove(batch_id: int, request: Request):
    """Usuniecie wczytanego pliku: znika to, co pochodzi tylko z niego (stan jak bez tego pliku). Przed zmiana - kopia zapasowa bazy."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    try:
        res = await run_in_threadpool(importing.remove_import, batch_id)
    except ImportFailed as e:
        return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": f"Nie udało się usunąć pliku ({type(e).__name__}); nic nie zostało zmienione."}, status_code=500)
    preview.invalidate_caches()
    importing.start_background_pricing()
    return JSONResponse(_jsonable(res))


@app.post("/api/konto/{account_id}/usun")
async def api_account_remove(account_id: int, request: Request):
    """Usuniecie calego portfela (konta): wszystkie jego wczytane pliki, stan jak bez nich. Przed zmiana - kopia zapasowa bazy."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    try:
        res = await run_in_threadpool(importing.remove_account, account_id)
    except ImportFailed as e:
        return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": f"Nie udało się usunąć portfela ({type(e).__name__}); nic nie zostało zmienione."}, status_code=500)
    preview.invalidate_caches()
    importing.start_background_pricing()
    return JSONResponse(_jsonable(res))


@app.get("/api/import/status")
def api_import_status():
    with importing._state_lock:
        return JSONResponse(dict(importing.STATE))


@app.get("/api/kontrahenci")
def api_counterparties():
    try:
        conn = importing.owner_conn()
        try:
            return JSONResponse(_jsonable({"kontrahenci": importing.pending_counterparties(conn)}))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)


@app.post("/api/kontrahenci/{counterparty_id}")
async def api_counterparty_decision(counterparty_id: int, request: Request):
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    body = await request.json()
    if not isinstance(body.get("moj"), bool):
        return JSONResponse({"blad": "brak decyzji"}, status_code=422)
    conn = importing.owner_conn()
    try:
        res = importing.decide_counterparty(conn, counterparty_id, body["moj"]); conn.commit()
    except ImportFailed as e:
        conn.rollback(); return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        conn.rollback(); return JSONResponse({"blad": f"Nie udało się zapisać decyzji ({type(e).__name__})."}, status_code=500)
    finally:
        conn.close()
    preview.invalidate_caches()
    return JSONResponse(res)

@app.get("/api/zywo")
def api_live(force: bool = False, scope: str = "all", fast: bool = False):
    """Wartosc na zywo (papiery wartosciowe: ilosc x cena z API x kurs; bez wolnych srodkow): odswieza notowania (z ograniczeniem czestosci). Tylko odczyt ksiegi."""
    try:
        refresh = {"skipped": True, "fast": True} if fast else preview.refresh_live_if_stale(force=force)      # fast: tylko zapisane ceny (szybkie pierwsze wyswietlenie)
        data = preview.live_data(scope)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)
    if data.get("scope") and (data["scope"].get("snapshot_positions") or data["scope"].get("missing_positions")):        # pozycje bez ceny rynkowej: sprobuj uzupelnic w tle (z ograniczeniem czestosci)
        importing.retry_missing_prices()
    return JSONResponse(_jsonable({"odswiezenie": refresh, **data}))


@app.get("/api/historia")
def api_history(scope: str = "all"):
    """Dzienny szereg wartosci papierow i wplat netto (zakupy - sprzedaze) do wykresu."""
    try:
        s = preview.history_data(scope)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)
    return JSONResponse(_jsonable(s))

@app.get("/api/wskazniki")
def api_metrics(scope: str = "all"):
    """Wskazniki inwestorskie (zwrot %, rok do roku, XIRR, obsuniecie, zmiennosc...). Tylko odczyt."""
    try:
        data = preview.metrics_data(scope)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)
    return JSONResponse(_jsonable(data))

@app.get("/api/dochody")
def api_income(scope: str = "all"):
    """Dochody dodatkowe miesiecznie (dywidendy, odsetki, nagrody, oplaty) do wykresu. Tylko odczyt."""
    try:
        return JSONResponse(_jsonable(preview.income_monthly(scope)))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)


@app.get("/api/kontrola")
def api_audit(scope: str = "all"):
    """Kontrola poprawnosci liczb: wartosc = ilosc x cena z API x kurs (przeliczone niezaleznie) i tozsamosci ksiegowe. Tylko odczyt."""
    try:
        data = preview.audit_data(scope)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)
    return JSONResponse(_jsonable(data))


@app.get("/api/pytania")
def api_questions():
    """Pytania brokerow do uzytkownika (np. waluta instrumentow .PL)."""
    try:
        conn = importing.owner_conn()
        try:
            return JSONResponse(_jsonable({"pytania": importing.pending_questions(conn)}))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)


@app.post("/api/pytania/{broker_key}/{question_id}")
async def api_answer(broker_key: str, question_id: str, request: Request):
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    body = await request.json()
    answer = body.get("odpowiedz")
    if not isinstance(answer, str):
        return JSONResponse({"blad": "brak odpowiedzi"}, status_code=422)
    conn = importing.owner_conn()
    try:
        fields = body.get("pola") if isinstance(body.get("pola"), dict) else {}
        res = importing.answer_question(conn, broker_key, question_id, answer, {k: str(v)[:200] for k, v in fields.items()}); conn.commit()
    except ImportFailed as e:
        conn.rollback(); return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        conn.rollback(); return JSONResponse({"blad": f"Nie udało się zapisać odpowiedzi ({type(e).__name__})."}, status_code=500)
    finally:
        conn.close()
    preview.invalidate_caches()
    importing.start_background_pricing()
    return JSONResponse(_jsonable(res))


@app.post("/api/zamknij")
async def api_shutdown(request: Request):
    """Zamkniecie aplikacji z przegladarki albo przez instalator (przed aktualizacja): czyste zatrzymanie serwera WWW, potem bazy."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    server = getattr(request.app.state, "server", None)
    if server is None:
        return JSONResponse({"blad": "Aplikacja nie została uruchomiona przez program startowy."}, status_code=409)
    window = getattr(request.app.state, "window", None)
    if window is not None:                                   # wlasne okno: zamkniecie okna konczy aplikacje (serwer i baza)
        threading.Timer(0.3, window.destroy).start()
    else:
        server.should_exit = True
    return JSONResponse({"zamykanie": True})


@app.post("/api/pokaz")
async def api_show(request: Request):
    """Ponowne uruchomienie skrotu, gdy aplikacja juz dziala: jej okno na wierzch (zamiast drugiej kopii)."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    window = getattr(request.app.state, "window", None)
    if window is None:
        return JSONResponse({"okno": False})
    try:
        if not _focus_native_window():                     # Windows: przywroc tylko zminimalizowane (zmaksymalizowane zostaje) i daj na wierzch
            window.restore(); window.on_top = True; window.on_top = False
    except Exception:  # noqa: BLE001
        return JSONResponse({"okno": False})
    return JSONResponse({"okno": True})


def _focus_native_window():
    """Okno tego procesu na wierzch (Windows). Zwraca False, gdy sie nie da (inny system, brak okna)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32, me, found = ctypes.windll.user32, os.getpid(), []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                             # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


@app.get("/api/eksport.xlsx")
def api_export_xlsx(scope: str = "all"):
    """Eksport do Excela: plik budowany w pamieci i oddawany oknu aplikacji (okno "Zapisz jako"); aplikacja go nigdzie nie wysyla ani nie zapisuje sama."""
    from fastapi.responses import Response
    from invapp.web import export_xlsx
    data, name = export_xlsx.build(scope)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/ekspozycja")
def api_exposure(scope: str = "all"):
    try:
        return JSONResponse(_jsonable(preview.exposure_data(scope)))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)


@app.get("/api/gotowka")
def api_cash():
    """Zakladka Gotowka: konta bankowe (wpisane recznie), wolne srodki XTB (z ksiegi), lokaty, ustawienia."""
    try:
        return JSONResponse(_jsonable(preview.cash_data()))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": type(e).__name__}, status_code=503)


async def _cash_write(request, fn):
    """Zapis gotowki/ustawien: tylko z naglowkiem ochronnym, przez wlasciciela bazy (rola aplikacji ma tylko odczyt tych tabel)."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    from invapp.core import cash
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    conn = importing.owner_conn()
    try:
        res = fn(conn.cursor(), cash, body); conn.commit()
    except cash.CashError as e:
        conn.rollback(); return JSONResponse({"blad": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001
        conn.rollback(); return JSONResponse({"blad": f"Nie udało się zapisać ({type(e).__name__})."}, status_code=500)
    finally:
        conn.close()
    preview.invalidate_caches()
    return JSONResponse(_jsonable(res if res is not None else {"zapisano": True}))


@app.post("/api/gotowka/konto")
async def api_cash_add(request: Request):
    return await _cash_write(request, lambda cur, cash, b: {"id": cash.add_account(cur, b.get("nazwa"), b.get("waluta"), b.get("saldo"), b.get("na_dzien"))})


@app.post("/api/gotowka/konto/{aid}/saldo")
async def api_cash_balance(aid: int, request: Request):
    return await _cash_write(request, lambda cur, cash, b: cash.set_balance(cur, aid, b.get("saldo"), b.get("na_dzien")))


@app.post("/api/gotowka/konto/{aid}/usun")
async def api_cash_archive(aid: int, request: Request):
    return await _cash_write(request, lambda cur, cash, b: cash.archive(cur, aid))


@app.post("/api/ustawienia")
async def api_settings(request: Request):
    return await _cash_write(request, lambda cur, cash, b: {"ustawienia": cash.set_setting(cur, str(b.get("klucz")), b.get("wartosc"))})


@app.post("/api/ceny/odswiez")
async def api_prices_refresh(request: Request):
    """Rozwiazanie z kontroli liczb: ponowne pobranie cen i weryfikacja symboli (zadanie w tle)."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    return JSONResponse({"uruchomiono": importing.start_background_pricing()})


@app.post("/api/przelicz")
async def api_rebuild(request: Request):
    """Rozwiazanie z kontroli liczb: przeliczenie partii, korekt i klasyfikacji od nowa z danych z eksportow (deterministycznie; dane surowe bez zmian)."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)

    def work():
        with importing._import_lock:                       # nie w trakcie importu pliku
            conn = importing.owner_conn()
            try:
                out = {}
                for b in brokers.BROKERS:
                    out[b.key] = bool(b.rebuild_derived(conn) is not None)
                conn.commit()
                return out
            except Exception:
                conn.rollback(); raise
            finally:
                conn.close()
    try:
        res = await run_in_threadpool(work)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": f"Nie udało się przeliczyć ({type(e).__name__})."}, status_code=500)
    preview.invalidate_caches(); importing.start_background_pricing()
    return JSONResponse({"przeliczono": res})


@app.get("/api/aktualizacja")
def api_update_check(force: bool = False):
    """Czy jest nowsza wersja na GitHubie (Releases repozytorium)? Tylko odczyt; brak internetu = brak informacji."""
    from invapp import updates
    return JSONResponse(_jsonable(updates.check(force=force)))


@app.post("/api/aktualizacja/instaluj")
async def api_update_install(request: Request):
    """Pobiera instalator nowej wersji (tylko z wydania repozytorium, sprawdzona suma SHA-256) i uruchamia go; instalator zamyka i uruchamia aplikacje."""
    if not _same_origin(request):
        return JSONResponse({"blad": "niedozwolone zadanie"}, status_code=403)
    from invapp import updates
    try:
        res = await run_in_threadpool(updates.install_latest)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"blad": str(e) if isinstance(e, ValueError) else f"Nie udało się pobrać aktualizacji ({type(e).__name__})."}, status_code=422)
    return JSONResponse(res)


@app.get("/zdrowie")
def health():
    return JSONResponse({"aplikacja": APP_NAME, "wersja": __version__, "status": "dziala", "baza": db_status(), "dane_id": config.data_id()})
