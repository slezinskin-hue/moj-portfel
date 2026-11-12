"""Kopie zapasowe bazy (pg_dump z wbudowanego PostgreSQL) w katalogu danych\\kopie; zostaje 10 najnowszych."""
import os, subprocess
from datetime import datetime

from invapp import config
from invapp.db import engine

KEEP = 10


def create(label="kopia"):
    st = engine._state if engine._state["server"] is not None else engine.start()
    out = config.backups_dir() / f"investing_{datetime.now():%Y%m%d_%H%M%S}_{label}.dump"
    env = dict(os.environ, PGPASSWORD=st["secrets"][engine.OWNER])
    subprocess.run([engine.bin_path("pg_dump"), "-h", st["host"], "-p", str(st["port"]), "-U", engine.OWNER, "-Fc", "-f", str(out), engine.DB_NAME],
                   check=True, env=env, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    dumps = sorted(config.backups_dir().glob("investing_*.dump"))
    for old in dumps[:-KEEP]:
        old.unlink(missing_ok=True)
    return out


def list_backups():
    return sorted(config.backups_dir().glob("investing_*.dump"), reverse=True)
