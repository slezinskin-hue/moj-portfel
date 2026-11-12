"""
Kopiuje dane ZAINSTALOWANEJ aplikacji (prawdziwe) do katalogu wersji deweloperskiej - do testowania zmian na prawdziwych liczbach,
bez ryzyka dla danych zainstalowanej aplikacji (nowe migracje zostana zastosowane tylko na kopii).

    python -m invapp.tools.kopiuj_dane [--tak]

Warunki: obie aplikacje (zainstalowana i deweloperska) sa zamkniete. Dotychczasowe dane deweloperskie sa zastepowane (najpierw pytanie).
Zrodlo nie jest zmieniane (tylko kopiowane).
"""
import argparse, json, shutil, sys
from pathlib import Path

from invapp import config


def _running(data_dir: Path):
    """Czy aplikacja albo baza z tego katalogu danych dziala?"""
    from invapp.__main__ import our_app_running
    try:
        port = int(json.loads((data_dir / "stan.json").read_text(encoding="utf-8"))["port"])
        if our_app_running(port):
            return "aplikacja"
    except Exception:  # noqa: BLE001
        pass
    from invapp.db import engine
    pm = engine._postmaster(data_dir / "baza")
    if pm and engine._alive(pm[0]):
        return "baza danych"
    return None


def copy(src: Path, dst: Path):
    if not (src / "baza" / "PG_VERSION").exists() or not (src / "hasla_bazy.json").exists():
        raise SystemExit(f"Brak danych zainstalowanej aplikacji w {src}.")
    for d, label in ((src, "zainstalowana"), (dst, "deweloperska")):
        what = _running(d)
        if what:
            raise SystemExit(f"Najpierw zamknij aplikację ({label}: działa {what}) – przycisk ⏻ na stronie albo: python -m invapp --stop")
    if (dst / "baza").exists():
        shutil.rmtree(dst / "baza")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "baza", dst / "baza", ignore=shutil.ignore_patterns("postmaster.pid", "log", "*.log"))
    shutil.copy2(src / "hasla_bazy.json", dst / "hasla_bazy.json")
    for f in ("stan.json",):
        (dst / f).unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Kopia prawdziwych danych do wersji deweloperskiej")
    ap.add_argument("--tak", action="store_true", help="bez pytania o potwierdzenie")
    a = ap.parse_args()
    src, dst = config.installed_data_dir(), config._base() / config.DEV_DIR_NAME
    print(f"Z:  {src}\nDo: {dst}  (obecne dane deweloperskie zostaną zastąpione)")
    if not a.tak and input("Kontynuować? [t/N] ").strip().lower() not in ("t", "tak", "y"):
        print("Przerwano."); return 1
    copy(src, dst)
    print("Gotowe. Uruchom wersję deweloperską: python uruchom.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
