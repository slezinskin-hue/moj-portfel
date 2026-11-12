"""
Migracje SQL (invapp/db/migrations/NNNN_opis.sql), stosowane po kolei, kazda w osobnej transakcji; stan w tabeli schema_migrations
(wersja, nazwa, SHA-256). Zmieniony plik juz zastosowanej migracji = BLAD (poprawka = nowa migracja).
Nowy broker, ktory potrzebuje tabel/widokow, dodaje po prostu kolejna migracje z nastepnym numerem.
"""
import hashlib, re
from pathlib import Path

MIGRATIONS = Path(__file__).with_name("migrations")
FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


# Poprawki WYLACZNIE komentarzy w juz zastosowanych migracjach (usuniecie danych osobistych przed publikacja kodu, 2026-11-12):
# stara suma SHA-256 jest akceptowana i w bazie zamieniana na nowa. Kazda inna zmiana pliku nadal jest bledem.
COMMENT_ONLY_EDITS = {
    1: {"b3aa6de38dfc602f1ccd0e46d69ab1bf32beae0d183045ffe7fb0db7b21100c5", "25d2f96de1d52c5c60ff51df0b09129d927ab3f06dc2770f2be73c03250ec826"},
    2: {"4e8bbdccfe5755d1720358beefa4f22833ac817dd69d73275ac9f392ae13df95"},
    4: {"ab94ec57ecda304247e79a074ac831512c656283c71766d8ae637be2bb813ae0"},
    15: {"3ee8aa0244ccf7a1e6cf2164308179fe15e5735bc31d85c655a110aa45d411a8"},
}


def migration_files():
    out = []
    for p in sorted(MIGRATIONS.iterdir()):
        m = FILE_RE.match(p.name)
        if m:
            data = p.read_bytes()
            out.append((int(m.group(1)), p.name, hashlib.sha256(data).hexdigest(), data.decode("utf-8")))
    versions = [v for v, *_ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Numery migracji musza byc ciagle od 1: {versions}")
    return out


def concatenated_sql():
    """Wszystkie migracje po kolei jako jeden tekst (testy na tymczasowej bazie)."""
    return "\n".join(sql for *_, sql in migration_files())


def pending(conn):
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 CHAR(64) NOT NULL,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    conn.commit()
    cur.execute("SELECT version, sha256 FROM schema_migrations")
    applied = {v: h.strip() for v, h in cur.fetchall()}
    files = migration_files()
    for v, name, h, _ in files:
        if v in applied and applied[v] != h:
            if applied[v] in COMMENT_ONLY_EDITS.get(v, ()):
                cur.execute("UPDATE schema_migrations SET sha256 = %s WHERE version = %s", (h, v)); conn.commit()
                continue
            raise MigrationError(f"Migracja {name} zostala zmieniona po zastosowaniu (inna suma SHA-256).")
    unknown = set(applied) - {f[0] for f in files}
    if unknown:
        raise MigrationError(f"Baza ma migracje nowsze niz ten program: {sorted(unknown)}. Zaktualizuj program.")
    return [f for f in files if f[0] not in applied]


def apply(conn):
    """Stosuje oczekujace migracje (rola wlasciciela). Zwraca liste nazw zastosowanych."""
    done = []
    for v, name, h, sql in pending(conn):
        cur = conn.cursor()
        try:
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations(version, name, sha256) VALUES (%s,%s,%s)", (v, name, h))
            conn.commit()
        except Exception:
            conn.rollback(); raise
        done.append(name)
    return done
