"""
Lista obslugiwanych brokerow. NOWY BROKER = nowy folder invapp/brokers/<nazwa>/ z klasa dziedziczaca po base.Broker + jedna linijka ponizej
(oraz, jesli trzeba, migracja dodajaca jego wpis w financial_institutions - zrobi to tez automatycznie ensure_registered()).
"""
from invapp.brokers.trade_republic.broker import TradeRepublic
from invapp.brokers.xtb.broker import Xtb

BROKERS = [Xtb(), TradeRepublic()]


def by_key(key):
    return next((b for b in BROKERS if b.key == key), None)


def by_code(code):
    return next((b for b in BROKERS if b.code == code), None)


def for_upload(filename, head):
    """Broker, do ktorego pasuje wgrany plik (albo None)."""
    return next((b for b in BROKERS if b.matches(filename, head)), None)


def ensure_registered(conn):
    """Kazdy broker z listy ma wpis instytucji i zakres BROKER:<kod> (idempotentnie)."""
    cur = conn.cursor()
    for b in BROKERS:
        cur.execute("INSERT INTO financial_institutions(code, name, institution_kind) VALUES (%s,%s,'BROKER') ON CONFLICT (code) DO NOTHING", (b.code, b.name))
        cur.execute("INSERT INTO scopes(code, name, kind) VALUES (%s,%s,'BROKER') ON CONFLICT (code) DO NOTHING", (b.scope_code, f"Portfel {b.name}"))
    conn.commit()
