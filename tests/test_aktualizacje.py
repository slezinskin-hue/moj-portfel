"""Aktualizacje z wydan GitHuba (invapp/updates.py) i zamykanie aplikacji - bez sieci (odpowiedzi API podstawione)."""
import hashlib, os, sys, tempfile, unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("INVAPP_DATA_DIR", os.path.join(tempfile.gettempdir(), "invapp_app_testdata"))
from invapp import __version__, updates  # noqa: E402

PAYLOAD = b"MZ fake installer"
SHA = hashlib.sha256(PAYLOAD).hexdigest()
INFO = {"repo": "uzytkownik/moj-portfel"}
URL = "https://github.com/uzytkownik/moj-portfel/releases/download/v9.9.9/MojPortfel-Setup-9.9.9.exe"


def release(tag, url=URL, sha=SHA, digest=None):
    asset = {"name": "MojPortfel-Setup-9.9.9.exe", "browser_download_url": url}
    if digest:
        asset["digest"] = digest
    return {"tag_name": tag, "name": f"Mój Portfel {tag}", "body": f"Zmiany...\n\nSHA256: {sha}" if sha else "Zmiany",
            "assets": [{"name": "MojPortfel-Setup-9.9.9.exe.sha256", "browser_download_url": url + ".sha256"}, asset],
            "html_url": "https://github.com/uzytkownik/moj-portfel/releases/tag/" + tag}


class TestUpdates(unittest.TestCase):
    def setUp(self):
        self.orig = updates.build_info
        updates.build_info = lambda: INFO
        updates._cache.update(at=0.0, data=None, release=None)

    def tearDown(self):
        updates.build_info = self.orig
        updates._cache.update(at=0.0, data=None, release=None)

    def test_versions(self):
        self.assertEqual(updates.parse_version("v1.2.10"), (1, 2, 10))
        self.assertGreater(updates.parse_version("v0.10.0"), updates.parse_version("0.9.9"))       # porownanie liczbowe, nie tekstowe
        self.assertIsNone(updates.parse_version("v1.2"))
        self.assertIsNone(updates.parse_version("nightly"))

    def test_newer_release_is_offered_and_older_or_same_is_not(self):
        seen = []

        def fetch(url):
            seen.append(url); return release("v9.9.9")
        st = updates.check(force=True, fetch=fetch)
        self.assertTrue(st["dostepna"]); self.assertEqual(st["najnowsza"], "9.9.9")
        self.assertEqual(seen[0], "https://api.github.com/repos/uzytkownik/moj-portfel/releases/latest")     # publiczne API, bez logowania
        self.assertFalse(updates.check(force=True, fetch=lambda url: release("v" + __version__))["dostepna"])
        self.assertFalse(updates.check(force=True, fetch=lambda url: release("v9.9.9", sha=None))["dostepna"])      # brak sumy = nie proponujemy
        st = updates.check(force=True, fetch=lambda url: release("v9.9.9", sha=None, digest="sha256:" + SHA))        # suma z pola digest pliku
        self.assertTrue(st["dostepna"])
        self.assertEqual(updates.latest_release(INFO, lambda url: release("v9.9.9"))["installer_name"], "MojPortfel-Setup-9.9.9.exe")  # .exe, nie .sha256

    def test_no_repo_or_no_network_never_raises(self):
        updates.build_info = lambda: {"repo": ""}
        self.assertFalse(updates.check(force=True)["dostepna"])
        updates.build_info = lambda: INFO

        def boom(url):
            raise OSError("brak sieci")
        st = updates.check(force=True, fetch=boom)
        self.assertFalse(st["dostepna"]); self.assertIn("Nie udało się", st["powod"])

    def test_download_only_from_this_repository_release_and_checksum_verified(self):
        d = Path(tempfile.mkdtemp(prefix="invapp_upd_"))
        rel = updates.latest_release(INFO, lambda url: release("v9.9.9"))
        p = updates.download_and_verify(rel, INFO["repo"], d, fetch_bytes=lambda url: PAYLOAD)
        self.assertEqual(p.read_bytes(), PAYLOAD)
        with self.assertRaises(ValueError):                                                       # zmieniony plik
            updates.download_and_verify(rel, INFO["repo"], d, fetch_bytes=lambda url: PAYLOAD + b"x")
        for bad in ("https://evil.example/uzytkownik/moj-portfel/releases/download/v9.9.9/x.exe",       # obcy serwer
                    "https://github.com/ktos-inny/moj-portfel/releases/download/v9.9.9/x.exe",          # inne repozytorium
                    "http://github.com/uzytkownik/moj-portfel/releases/download/v9.9.9/x.exe"):          # bez https
            r = updates.latest_release(INFO, lambda url: release("v9.9.9", url=bad))
            with self.assertRaises(ValueError):
                updates.download_and_verify(r, INFO["repo"], d, fetch_bytes=lambda url: PAYLOAD)


class TestStartMode(unittest.TestCase):
    def test_choose_mode(self):
        from types import SimpleNamespace as NS
        from invapp.__main__ import choose_mode
        self.assertEqual(choose_mode(NS(no_browser=False, przegladarka=False), True), "okno")             # domyslnie wlasne okno
        self.assertEqual(choose_mode(NS(no_browser=False, przegladarka=False), False), "przegladarka")    # brak pywebview/WebView2
        self.assertEqual(choose_mode(NS(no_browser=False, przegladarka=True), True), "przegladarka")
        self.assertEqual(choose_mode(NS(no_browser=True, przegladarka=False), True), "bez-okna")
        from invapp.__main__ import ICON
        self.assertTrue(os.path.exists(ICON))                                                            # ikona okna jest w pakiecie programu

    def test_running_other_version_is_replaced_same_version_is_shown(self):
        from unittest import mock
        import invapp.__main__ as m
        calls = []
        with mock.patch.object(m, "running_port", return_value=8000), mock.patch.object(m, "stop_running", lambda: calls.append("stop")), \
             mock.patch.object(m, "show_running", lambda p, mode: calls.append(("show", p, mode))):
            with mock.patch.object(m, "our_app_running", return_value=__version__):
                self.assertTrue(m.handle_running("okno"))                     # ta sama wersja: tylko pokaz
            with mock.patch.object(m, "our_app_running", return_value="0.0.1"):
                self.assertFalse(m.handle_running("okno"))                    # inna wersja: zatrzymaj ja i uruchom sie
        self.assertEqual(calls, [("show", 8000, "okno"), "stop"])
        with mock.patch.object(m, "running_port", return_value=None):
            self.assertFalse(m.handle_running("okno"))


class TestShutdownApi(unittest.TestCase):
    def test_shutdown_requires_header_and_stops_server(self):
        from fastapi.testclient import TestClient
        from invapp.web.app import app

        class FakeServer:
            should_exit = False
        c = TestClient(app, base_url="http://127.0.0.1")
        self.assertEqual(c.post("/api/zamknij").status_code, 403)                                 # bez naglowka ochronnego
        app.state.server = FakeServer()
        try:
            r = c.post("/api/zamknij", headers={"X-Requested-With": "invapp"})
            self.assertEqual(r.status_code, 200); self.assertTrue(app.state.server.should_exit)
        finally:
            del app.state.server
        # wlasne okno aplikacji: zamkniecie = zamkniecie okna (konczy aplikacje); ponowne uruchomienie skrotu = okno na wierzch
        import time

        class FakeWindow:
            def __init__(self):
                self.calls = []
                self._top = False
            def destroy(self): self.calls.append("destroy")
            def restore(self): self.calls.append("restore")
            @property
            def on_top(self): return self._top
            @on_top.setter
            def on_top(self, v): self.calls.append(f"on_top={v}"); self._top = v
        app.state.server, app.state.window = FakeServer(), FakeWindow()
        try:
            self.assertEqual(c.post("/api/pokaz").status_code, 403)
            self.assertTrue(c.post("/api/pokaz", headers={"X-Requested-With": "invapp"}).json()["okno"])
            self.assertEqual(app.state.window.calls, ["restore", "on_top=True", "on_top=False"])
            c.post("/api/zamknij", headers={"X-Requested-With": "invapp"}); time.sleep(0.6)
            self.assertIn("destroy", app.state.window.calls); self.assertFalse(app.state.server.should_exit)   # serwer konczy sie po zamknieciu okna
        finally:
            del app.state.server; del app.state.window
        self.assertFalse(c.post("/api/pokaz", headers={"X-Requested-With": "invapp"}).json()["okno"])          # bez okna (tryb przegladarki)
        self.assertEqual(c.post("/api/aktualizacja/instaluj").status_code, 403)
        self.assertEqual(c.post("/api/import/1/usun").status_code, 403)                          # usuwanie pliku tez tylko z naglowkiem ochronnym
        self.assertEqual(c.get("/api/aktualizacja").status_code, 200)


if __name__ == "__main__":
    unittest.main()
