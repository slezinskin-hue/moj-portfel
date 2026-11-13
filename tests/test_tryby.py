"""Tryby pracy: zainstalowana aplikacja (prawdziwe dane) i wersja deweloperska ze zrodel (osobne dane) + kopia danych do wersji deweloperskiej."""
import os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from invapp import config  # noqa: E402
from invapp.tools import kopiuj_dane  # noqa: E402


class TestModes(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="invapp_tryby_"))
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)})
        self.env.start()
        self.saved = os.environ.pop(config.DATA_DIR_ENV, None)

    def tearDown(self):
        if self.saved is not None:
            os.environ[config.DATA_DIR_ENV] = self.saved
        self.env.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_source_run_uses_separate_dev_data_and_installed_uses_real(self):
        with mock.patch.object(config, "is_installed", return_value=False):
            self.assertEqual(config.data_dir(), self.base / "AplikacjaInwestycyjna-dev")
            self.assertTrue(config.is_dev_data())
        with mock.patch.object(config, "is_installed", return_value=True):
            self.assertEqual(config.data_dir(), self.base / "AplikacjaInwestycyjna")
            self.assertFalse(config.is_dev_data())
        os.environ[config.DATA_DIR_ENV] = str(self.base / "inny")
        try:
            self.assertEqual(config.data_dir(), self.base / "inny"); self.assertFalse(config.is_dev_data())
        finally:
            del os.environ[config.DATA_DIR_ENV]
        self.assertFalse(config.is_installed())                    # w repozytorium nie ma _build.py (tworzy go tylko budowanie instalatora)

    def test_source_run_never_migrates_real_data(self):
        """Wersja ze zrodel z --data-dir na prawdziwe dane nie migruje istniejacej bazy (zainstalowana wersja przestalaby sie uruchamiac)."""
        from invapp.db import engine
        real, todo = str(self.base / "AplikacjaInwestycyjna"), [("0099", "x", "h")]
        with mock.patch.object(config, "is_installed", return_value=False):
            with mock.patch.dict(os.environ, {config.DATA_DIR_ENV: real}):
                self.assertTrue(engine.source_migration_blocked(todo, 20))
                self.assertFalse(engine.source_migration_blocked([], 20))                      # nic do migracji
                self.assertFalse(engine.source_migration_blocked(todo, 0))                     # nowa, pusta baza
                with mock.patch.dict(os.environ, {engine.ALLOW_SOURCE_MIGRATION_ENV: "1"}):
                    self.assertFalse(engine.source_migration_blocked(todo, 20))                # swiadomy wyjatek
            self.assertFalse(engine.source_migration_blocked(todo, 20))                        # dane testowe (-dev)
        with mock.patch.object(config, "is_installed", return_value=True):
            self.assertFalse(engine.source_migration_blocked(todo, 20))                        # zainstalowana aplikacja migruje swoje dane

    def test_copy_real_data_to_dev_replaces_dev_and_keeps_source(self):
        src, dst = self.base / "AplikacjaInwestycyjna", self.base / "AplikacjaInwestycyjna-dev"
        (src / "baza").mkdir(parents=True); (src / "baza" / "PG_VERSION").write_text("16"); (src / "baza" / "postmaster.pid").write_text("x")
        (src / "hasla_bazy.json").write_text("{}")
        (dst / "baza").mkdir(parents=True); (dst / "baza" / "stary").write_text("dev")
        kopiuj_dane.copy(src, dst)
        self.assertTrue((dst / "baza" / "PG_VERSION").exists())
        self.assertFalse((dst / "baza" / "stary").exists())                 # dane deweloperskie zastapione
        self.assertFalse((dst / "baza" / "postmaster.pid").exists())        # bez znacznika dzialajacego serwera
        self.assertTrue((src / "baza" / "postmaster.pid").exists())         # zrodlo nietkniete
        with mock.patch.object(kopiuj_dane, "_running", side_effect=lambda d: "aplikacja" if d == src else None):
            with self.assertRaises(SystemExit):                              # dzialajaca aplikacja - nie kopiujemy
                kopiuj_dane.copy(src, dst)


if __name__ == "__main__":
    unittest.main()
