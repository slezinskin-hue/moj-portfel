"""
Waluta prezentacji - opcja 2 (decyzja wlasciciela 2026-11-13, docs/WALUTA.md): pelne liczenie w wybranej walucie W.

Przy W = PLN nic sie nie zmienia (ten modul nie jest uzywany). Przy W != PLN kazda kwota jest liczona z kwot PLN z ksiegi:
* przeplyw pieniedzy (zakup, sprzedaz, dywidenda, podatek, odsetki, nagroda, oplata, przelew) / kurs(dzien przeplywu),
* wartosc biezaca (papiery, lokaty, gotowka) / kurs teraz (ten sam kurs na zywo / NBP, co wycena papierow),
* wartosc historyczna dnia d / kurs(d).
kurs(d) = zl za 1 W: kurs dzienny Yahoo (fx_rates YAHOO_1D) z dnia d albo ostatni wczesniejszy (maks. history.STALE_DAYS dni) - jak wykres.
Brak kursu: kwota jawnie oznaczona (pole "brak_kursu"), nigdy cicho zgadnieta.
Kontrola niezalezna: pricing/audit.py (_currency_checks, SQL zamiast tego modulu).
"""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal as D

from .history import SOURCE, STALE_DAYS

CURRENCIES = ("PLN", "USD", "EUR", "GBP", "CHF")
SYMBOL = {"PLN": "zł", "USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF"}


def display_currency(cur):
    """Waluta prezentacji z ustawien (domyslnie PLN); baza bez ustawien -> PLN."""
    from invapp.core import cash
    try:
        return cash.get_settings(cur).get("display_currency", "PLN")
    except Exception:  # noqa: BLE001
        cur.connection.rollback()
        return "PLN"


class Rates:
    """Kursy dzienne W/PLN (YAHOO_1D) z regula "ostatni nie starszy niz STALE_DAYS" (jak ceny na wykresie)."""

    def __init__(self, cur, ccy):
        self.ccy = ccy
        cur.execute("select rate_date, rate from fx_rates where source = %s and base = %s and quote = 'PLN' order by rate_date", (SOURCE, ccy))
        rows = cur.fetchall()
        self.days = [r[0] for r in rows]
        self.vals = [D(r[1]) for r in rows]
        self.missing = set()

    def on(self, d):
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        i = bisect_right(self.days, d)
        if i and (d - self.days[i - 1]).days <= STALE_DAYS:
            return self.vals[i - 1]
        self.missing.add(d)
        return None

    def conv(self, amount, d):
        """Kwota PLN z dnia d w W (None, gdy brak kursu)."""
        if amount is None:
            return None
        k = self.on(d)
        return (D(amount) / k) if k else None


def live_rate(cur, ccy):
    """Kurs teraz (zl za 1 W) wg ustawienia kursow - jak wycena papierow (v_fx_for_valuation). None, gdy brak."""
    from invapp.core import cash
    nbp = cash.get_settings(cur)["fx_source"] == "NBP"
    cur.execute("select rate, nbp_rate, source from v_fx_for_valuation where base = %s and quote = 'PLN'", (ccy,))
    r = cur.fetchone()
    if not r:
        return None
    rate = r[1] if (nbp and r[1] is not None) else r[0]
    return D(rate) if rate is not None else None


def _sum(values):
    """Suma; None, gdy ktoras kwota nie ma kursu (jawny brak zamiast czesciowej sumy)."""
    tot = D(0)
    for v in values:
        if v is None:
            return None
        tot += v
    return tot


def lots_in(cur, accounts, rates):
    """Partie i zamkniecia kont w W, po instrumencie: koszt otwartych partii, zakupy, przychod ze sprzedazy, zysk zrealizowany."""
    accounts = list(accounts)
    cur.execute("""select dl.lot_id, dl.listing_id, (dl.opened_at at time zone 'UTC')::date, dl.cost_cash_initial,
                          dl.cost_cash_initial - coalesce((select sum(c.cost_cash) from derived_lot_closures c where c.lot_id = dl.lot_id), 0)
                   from derived_lots dl where dl.account_id = any(%s)""", (accounts,))
    out = defaultdict(lambda: {"cost_left": D(0), "purchases": D(0), "sales": D(0), "realized": D(0), "missing": False})
    buy_day = {}
    for lot, lst, d, cost0, left in cur.fetchall():
        buy_day[lot] = d
        o = out[lst]
        a, b = rates.conv(cost0, d), rates.conv(left, d)
        if a is None or b is None:
            o["missing"] = True; continue
        o["purchases"] += a; o["cost_left"] += b
    cur.execute("""select c.lot_id, dl.listing_id, (t.occurred_at at time zone 'UTC')::date, c.proceeds_cash, c.cost_cash
                   from derived_lot_closures c join derived_lots dl on dl.lot_id = c.lot_id join transactions t on t.txn_id = c.sell_txn_id
                   where dl.account_id = any(%s)""", (accounts,))
    for lot, lst, d, proceeds, cost in cur.fetchall():
        o = out[lst]
        p, c = rates.conv(proceeds, d), rates.conv(cost, buy_day[lot])
        if p is None or c is None:
            o["missing"] = True; continue
        o["sales"] += p; o["realized"] += p - c
    return out


INCOME_FIELDS = {"DIVIDEND": "dividends_gross", "TAX_WITHHOLDING": "withholding_tax", "INTEREST": "interest", "TAX_INTEREST": "interest_tax",
                 "REWARD": "rewards", "FEE": "fees"}


def income_txns(cur, accounts):
    cur.execute("""select t.txn_type, (t.occurred_at at time zone 'UTC')::date, t.cash_amount, t.listing_id, t.txn_id from transactions t
                   where t.status = 'ACTIVE' and t.cash_currency = 'PLN' and t.account_id = any(%s) and t.txn_type = any(%s)""",
                (list(accounts), list(INCOME_FIELDS)))
    return cur.fetchall()


def income_in(cur, accounts, rates, since=None, until=None):
    """Dochody dodatkowe w W (kazda operacja po kursie ze swojego dnia); opcjonalnie tylko dni (since, until]."""
    tot = {f: D(0) for f in INCOME_FIELDS.values()}
    missing = False
    for typ, d, amt, _, _ in income_txns(cur, accounts):
        if (since and d <= since) or (until and d > until):
            continue
        v = rates.conv(amt, d)
        if v is None:
            missing = True; continue
        tot[INCOME_FIELDS[typ]] += v
    tot["missing"] = missing
    return tot


def convert_live(cur, live, ccy, accounts, today=None):
    """Dane /api/zywo (PLN) -> W. Zwraca nowy slownik; pola PLN nie sa zmieniane w miejscu."""
    today = today or date.today()
    rates = Rates(cur, ccy)
    k = live_rate(cur, ccy)
    s = dict(live["scope"]) if live.get("scope") else None
    out = {"waluta": ccy, "kurs": k, "kurs_zrodlo": "na żywo / NBP (jak wycena)", "brak_kursu": k is None}
    if k is None:
        return dict(live, **out)
    now = lambda v: (D(v) / k) if v is not None else None  # noqa: E731
    lots = lots_in(cur, accounts, rates)
    ref = today - timedelta(days=1)
    k_ref = rates.on(ref)
    positions = []
    for p in live.get("positions", []):
        p = dict(p)
        lo = lots.get(p["listing_id"])
        p["value"] = now(p.get("value"))
        p["cost_left"] = None if (lo is None or lo["missing"]) else lo["cost_left"]
        p["price_pln"] = now(p.get("price_pln"))
        p["avg_cost"] = (p["cost_left"] / D(p["qty"])) if (p["cost_left"] is not None and p.get("qty")) else None
        p["accounts"] = [dict(a, value=now(a.get("value"))) for a in p.get("accounts", [])]
        if p.get("prev_price_pln") is not None and k_ref and p["value"] is not None:
            prev = D(p["prev_price_pln"]) / k_ref
            base = D(p["qty"]) * prev
            p["prev_price_pln"] = prev
            p["day_change"] = p["value"] - base
            p["day_change_pct"] = (p["day_change"] / base) if base else None
        else:
            p["prev_price_pln"] = p["day_change"] = p["day_change_pct"] = None
        positions.append(p)
    deps = [dict(d, value=now(d.get("value")), unbooked_interest_net=now(d.get("unbooked_interest_net"))) for d in live.get("deposits", [])]
    if s is not None:
        for f in ("securities_value", "deposits_value", "assets_value", "total_value", "cash_value", "xtb_free_value", "deposit_interest_unbooked"):
            if s.get(f) is not None:
                s[f] = now(s[f])
        miss = any(lo["missing"] for lo in lots.values())
        cost = _sum([lo["cost_left"] if not lo["missing"] else None for lo in lots.values()])       # zamkniete partie maja koszt 0
        realized = _sum([lo["realized"] if not lo["missing"] else None for lo in lots.values()])
        inc = income_in(cur, accounts, rates)
        s["securities_cost"] = cost
        s["unrealized_gain"] = (s["securities_value"] - cost) if cost is not None else None
        s["realized_gain"] = realized
        s["investment_gain"] = (s["unrealized_gain"] + realized) if (s["unrealized_gain"] is not None and realized is not None) else None
        for f in INCOME_FIELDS.values():
            s[f] = inc[f]
        s["dividends_net"] = inc["dividends_gross"] + inc["withholding_tax"]
        s["interest_net"] = inc["interest"] + inc["interest_tax"]
        s["additional_income"] = s["dividends_net"] + s["interest_net"] + inc["rewards"] + inc["fees"]
        s["total_result"] = s["total_result_live"] = (s["investment_gain"] + s["additional_income"]) if s["investment_gain"] is not None else None
        s["total_purchases"] = _sum([lo["purchases"] for lo in lots.values()])
        s["total_sales"] = _sum([lo["sales"] for lo in lots.values()])
        i12 = income_in(cur, accounts, rates, since=today - timedelta(days=365), until=today)
        dv, it = i12["dividends_gross"] + i12["withholding_tax"], i12["interest"] + i12["interest_tax"]
        s["income_12m"] = dict(s.get("income_12m") or {}, dividends=dv, interest=it, rewards=i12["rewards"], total=dv + it + i12["rewards"])
        out["brak_kursu"] = miss or inc["missing"] or bool(rates.missing)
    out["brakujace_dni_kursu"] = sorted(d.isoformat() for d in rates.missing)[:20]
    return dict(live, scope=s, positions=positions, deposits=deps, **out)


def convert_series(series, rates):
    """Seria dzienna z history.build_series (PLN) -> W: wartosc dnia / kurs(d), przeplywy dnia (zmiana wplat brutto i sprzedazy) / kurs(d), narastajaco.
    Dni bez kursu: kurs z najblizszego dnia (oznaczone w "estimated_fx_days"), zeby wskazniki mialy ciagla serie."""
    out, gross, sold, prev_g, prev_s, est, last_k = [], D(0), D(0), D(0), D(0), 0, None
    first_k = next((rates.on(p["date"]) for p in series if rates.on(p["date"])), None)
    for p in series:
        k = rates.on(p["date"])
        if k is None:
            k = last_k or first_k; est += 1
        if k is None:
            return {"series": [], "estimated_fx_days": len(series)}
        last_k = k
        g, so = D(p["invested_gross"]), D(p["sold"])
        gross += (g - prev_g) / k; sold += (so - prev_s) / k; prev_g, prev_s = g, so
        val = D(p["value"]) / k
        q = dict(p, value=val, securities=D(p["securities"]) / k, deposits=D(p["deposits"]) / k, invested_gross=gross, sold=sold,
                 invested_net=gross - sold, gain=val - (gross - sold), fx=k)
        out.append(q)
    return {"series": out, "estimated_fx_days": est}
