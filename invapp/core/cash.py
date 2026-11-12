"""
Gotowka poza inwestycjami i ustawienia aplikacji (migracja 0017; decyzja uzytkownika 2026-11-12).

* Konta bankowe wpisywane recznie: nazwa, waluta, saldo na dzien (historia sald - obowiazuje najnowszy wpis). Wartosc w PLN = saldo x kurs
  (dla walut obcych ten sam kurs co dla papierow: Yahoo na zywo, a gdy brak - NBP; widok v_fx_for_valuation).
* Wolne srodki XTB = saldo gotowki z ksiegi (widok v_cash_balance, PLN) kont XTB. Gotowka Trade Republic jest liczona jako lokata (core/deposits.py).
* Ustawienia (domyslnie WYLACZONE - liczby bez zmian):
    include_cash      - doliczaj gotowke z kont bankowych do wartosci portfela (tylko zakladka WSZYSTKIE),
    include_xtb_free  - doliczaj wolne srodki XTB do wartosci portfela (zakladki XTB i WSZYSTKIE).
  Doliczana jest tylko BIEZACA "wartosc portfela"; zysk, wplaty, wykresy i wskazniki zostaja bez zmian (gotowka nie jest inwestycja,
  a dodanie jej do serii bez przeplywu wygladaloby jak zysk).
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

DEFAULTS = {"include_cash": False, "include_xtb_free": False, "price_source": "YAHOO", "fx_source": "YAHOO"}
ALLOWED = {"include_cash": (True, False), "include_xtb_free": (True, False), "price_source": ("YAHOO", "XTB_EXPORT"), "fx_source": ("YAHOO", "NBP")}
CURRENCIES = ("PLN", "EUR", "USD", "GBP", "CHF")
CENT = Decimal("0.01")


class CashError(ValueError):
    pass


def get_settings(cur):
    """Ustawienia z bazy z wartosciami domyslnymi. Baza sprzed migracji 0017 -> same domyslne (wolajacy robi rollback przy bledzie)."""
    out = dict(DEFAULTS)
    cur.execute("select key, value from app_settings")
    for k, v in cur.fetchall():
        if k in ALLOWED and v in ALLOWED[k]:
            out[k] = v
    return out


def set_setting(cur, key, value):
    if key not in ALLOWED or value not in ALLOWED[key] or isinstance(value, bool) != isinstance(ALLOWED[key][0], bool):
        raise CashError("Nieznane ustawienie albo niedozwolona wartość.")
    from psycopg2.extras import Json
    cur.execute("""insert into app_settings(key, value) values (%s, %s)
                   on conflict (key) do update set value = excluded.value, updated_at = now()""", (key, Json(value)))
    return get_settings(cur)


def _amount(v):
    try:
        d = Decimal(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except Exception:  # noqa: BLE001
        raise CashError("Saldo musi być liczbą (np. 12 345,67).")
    if not d.is_finite() or abs(d) >= Decimal("1e15"):
        raise CashError("Saldo musi być liczbą (np. 12 345,67).")
    return d.quantize(CENT, ROUND_HALF_UP)


def _day(v):
    if v in (None, ""):
        return date.today()
    try:
        d = date.fromisoformat(str(v))
    except ValueError:
        raise CashError("Podaj datę salda (RRRR-MM-DD).")
    if d > date.today():
        raise CashError("Data salda nie może być z przyszłości.")
    return d


def accounts(cur):
    """Aktywne konta bankowe z najnowszym saldem i wartoscia w PLN (kurs jak dla papierow, wg ustawienia; brak kursu -> wartosc None, jawnie)."""
    nbp = get_settings(cur)["fx_source"] == "NBP"
    cur.execute("""select a.cash_account_id, a.name, a.currency, b.balance, b.as_of, b.recorded_at,
                          case when %s and fx.nbp_rate is not null then fx.nbp_rate else fx.rate end,
                          case when %s and fx.nbp_rate is not null then 'NBP_A' else fx.source end
                   from manual_cash_accounts a
                   left join lateral (select balance, as_of, recorded_at from manual_cash_balances x where x.cash_account_id = a.cash_account_id
                                      order by as_of desc, recorded_at desc, balance_id desc limit 1) b on true
                   left join v_fx_for_valuation fx on fx.base = a.currency and fx.quote = 'PLN'
                   where a.archived_at is null order by a.cash_account_id""", (nbp, nbp))
    out = []
    for aid, name, ccy, bal, as_of, rec, rate, src in cur.fetchall():
        k = Decimal(1) if ccy == "PLN" else rate
        out.append({"id": aid, "nazwa": name, "waluta": ccy, "saldo": bal, "na_dzien": as_of, "zapisano": rec,
                    "kurs": None if ccy == "PLN" else rate, "zrodlo_kursu": None if ccy == "PLN" else src,
                    "wartosc_pln": (bal * k).quantize(CENT, ROUND_HALF_UP) if bal is not None and k is not None else None})
    return out


def add_account(cur, name, currency, balance, as_of=None):
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise CashError("Podaj nazwę konta (do 80 znaków), np. „mBank – konto osobiste”.")
    currency = (currency or "PLN").upper()
    if currency not in CURRENCIES:
        raise CashError("Nieobsługiwana waluta.")
    bal, d = _amount(balance), _day(as_of)
    cur.execute("insert into manual_cash_accounts(name, currency) values (%s, %s) returning cash_account_id", (name, currency))
    aid = cur.fetchone()[0]
    cur.execute("insert into manual_cash_balances(cash_account_id, balance, as_of) values (%s, %s, %s)", (aid, bal, d))
    return aid


def _active(cur, aid):
    cur.execute("select 1 from manual_cash_accounts where cash_account_id = %s and archived_at is null", (aid,))
    if not cur.fetchone():
        raise CashError("Nie ma takiego konta.")


def set_balance(cur, aid, balance, as_of=None):
    _active(cur, aid)
    cur.execute("insert into manual_cash_balances(cash_account_id, balance, as_of) values (%s, %s, %s)", (aid, _amount(balance), _day(as_of)))


def archive(cur, aid):
    _active(cur, aid)
    cur.execute("update manual_cash_accounts set archived_at = now() where cash_account_id = %s", (aid,))


def broker_free_funds(cur, scope_code, broker="XTB"):
    """Wolne srodki brokera = saldo gotowki z ksiegi (PLN) kont tego brokera w zakresie."""
    cur.execute("""select a.account_id, a.display_name, a.product_type, coalesce(sum(cb.balance), 0)
                   from accounts a join financial_institutions f using (broker_id)
                   left join v_cash_balance cb on cb.account_id = a.account_id and cb.cash_currency = 'PLN'
                   where f.code = %s and a.account_id in (select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s)
                     and exists (select 1 from transactions t where t.account_id = a.account_id)
                   group by 1, 2, 3 order by 1""", (broker, scope_code))
    return [{"konto": n, "typ": p, "saldo": Decimal(b).quantize(CENT)} for _, n, p, b in cur.fetchall()]


def extra_value(cur, scope_key, scope_code):
    """Gotowka doliczana do wartosci portfela wg ustawien: {"gotowka": kwota|0, "wolne_xtb": kwota|0, "ustawienia": {...}}."""
    st = get_settings(cur)
    cash = sum((a["wartosc_pln"] or 0 for a in accounts(cur)), Decimal(0)) if st["include_cash"] and scope_key == "all" else Decimal(0)
    free = sum((x["saldo"] for x in broker_free_funds(cur, scope_code)), Decimal(0)) if st["include_xtb_free"] and scope_key in ("all", "xtb") else Decimal(0)
    return {"gotowka": cash, "wolne_xtb": free, "ustawienia": st}
