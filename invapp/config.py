"""
Ustawienia i katalog danych. Wszystko, co dotyczy konkretnego uzytkownika (baza, hasla generowane automatycznie, kopie zapasowe, logi),
lezy w KATALOGU DANYCH poza folderem programu - dzieki temu program mozna zainstalowac/zaktualizowac bez ruszania danych.

Dwa tryby (decyzja uzytkownika 2026-11-12: zainstalowana aplikacja do codziennego uzytku + osobna praca nad aktualizacjami):
* wersja z INSTALATORA (jest plik invapp/_build.py): %LOCALAPPDATA%\\AplikacjaInwestycyjna - prawdziwe dane;
* wersja uruchamiana ZE ZRODEL (folder z kodem): %LOCALAPPDATA%\\AplikacjaInwestycyjna-dev - osobne dane testowe, wiec nowe migracje
  i zmiany w trakcie pracy nie dotykaja danych zainstalowanej aplikacji. Prawdziwe dane mozna skopiowac do wersji deweloperskiej:
  python -m invapp.tools.kopiuj_dane
Zawsze mozna wskazac inny katalog: zmienna INVAPP_DATA_DIR albo parametr --data-dir.
"""
import os
from pathlib import Path

DATA_DIR_ENV = "INVAPP_DATA_DIR"
DEFAULT_PORT = 8000
HOST = "127.0.0.1"          # aplikacja jest dostepna wylacznie z tego komputera
INSTALLED_DIR_NAME = "AplikacjaInwestycyjna"
DEV_DIR_NAME = "AplikacjaInwestycyjna-dev"


def is_installed() -> bool:
    """Czy to wersja z instalatora (budowanie tworzy invapp/_build.py)."""
    return (Path(__file__).resolve().parent / "_build.py").exists()


def _base() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share"))


def installed_data_dir() -> Path:
    """Katalog danych zainstalowanej aplikacji (prawdziwe dane) - niezaleznie od trybu."""
    return _base() / INSTALLED_DIR_NAME


def is_dev_data() -> bool:
    return not os.environ.get(DATA_DIR_ENV) and not is_installed()


def data_dir() -> Path:
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        p = Path(env)
    else:
        p = _base() / (INSTALLED_DIR_NAME if is_installed() else DEV_DIR_NAME)
    p.mkdir(parents=True, exist_ok=True)
    return p


def pgdata_dir() -> Path:
    return data_dir() / "baza"


def backups_dir() -> Path:
    p = data_dir() / "kopie"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logi"
    p.mkdir(parents=True, exist_ok=True)
    return p


def secrets_file() -> Path:
    return data_dir() / "hasla_bazy.json"
