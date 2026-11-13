"""
Liczby dla tabeli pozycji (wersja 0.4): zmiana dzienna, sredni koszt, zamkniete pozycje, dochody z 12 miesiecy, dane wykresu pozycji.
Tylko odczyt, zrodla jak w reszcie aplikacji (partie, zamkniecia partii, ceny i kursy dzienne YAHOO_1D, ksiega).

Definicje (opisane tez w interfejsie, "Jak liczone"):
* Dzis (zl)      = obecna ilosc x (cena teraz w PLN - cena wczoraj w PLN). Cena wczoraj = zamkniecie z dnia `ref` (wczoraj) albo z ostatniej sesji
                   przed nim (maks. history.STALE_DAYS dni) x kurs dzienny z tego dnia - DOKLADNIE ta sama cena, ktora dla tego dnia przyjmuje wykres
                   portfela (pricing/history.build_series). Dzis % = Dzis / (ilosc x cena wczoraj). Tylko dla pozycji wycenianych cena rynkowa z API.
* Sredni koszt   = koszt otwartych partii (gotowka z ksiegi z prowizjami, PLN) / ilosc - ten sam koszt co w kolumnie "Koszt" i w zysku.
* Zamkniete      = sprzedaze z zamkniec partii (derived_lot_closures) pogrupowane po instrumencie; suma zysku = zysk zrealizowany z kafelka
                   (kontrola liczb: pricing/audit.py).
* Dochody 12 m.  = dywidendy netto + odsetki netto + nagrody z dni (dzis - 365, dzis] (kwoty PLN z ksiegi; jak "stopa dywidendy 12 m" we wskaznikach).
"""
from datetime import date, timedelta
from decimal import Decimal as D

from .history import SOURCE, STALE_DAYS

INCOME_12M_DAYS = 365


def prev_close_pln(cur, listing_ids, ref):
    """{listing_id: (cena w PLN, data zamkniecia)} na dzien `ref` - reguly jak w build_series (zamkniecie <= ref nie starsze niz STALE_DAYS,
    kurs dzienny <= ref nie starszy niz STALE_DAYS; PLN = 1). Brak ceny lub kursu -> brak klucza."""
    ids = list(listing_ids)
    if not ids:
        return {}
    cur.execute("""select distinct on (listing_id) listing_id, price_date, close_price, currency from prices
                   where source = %s and listing_id = any(%s) and price_date <= %s order by listing_id, price_date desc""", (SOURCE, ids, ref))
    closes = [r for r in cur.fetchall() if (ref - r[1]).days <= STALE_DAYS]
    ccys = sorted({r[3] for r in closes} - {"PLN"})
    fx = {}
    if ccys:
        cur.execute("""select distinct on (base) base, rate_date, rate from fx_rates
                       where source = %s and quote = 'PLN' and base = any(%s) and rate_date <= %s order by base, rate_date desc""", (SOURCE, ccys, ref))
        fx = {b: D(r) for b, d, r in cur.fetchall() if (ref - d).days <= STALE_DAYS}
    out = {}
    for lst, d, close, ccy in closes:
        rate = D(1) if ccy == "PLN" else fx.get(ccy)
        if rate is not None:
            out[lst] = (D(close) * rate, d)
    return out


def add_day_and_average(cur, positions, today=None):
    """Uzupelnia pozycje (live_data) o: avg_cost (koszt / ilosc), prev_price_pln, prev_date, day_change, day_change_pct."""
    today = today or date.today()
    ref = today - timedelta(days=1)
    prev = prev_close_pln(cur, [p["listing_id"] for p in positions], ref)
    for p in positions:
        q = D(p["qty"]) if p.get("qty") is not None else None
        p["avg_cost"] = (D(p["cost_left"]) / q) if (q and p.get("cost_left") is not None) else None
        p["prev_price_pln"] = p["prev_date"] = p["day_change"] = p["day_change_pct"] = None
        pc = prev.get(p["listing_id"])
        if pc and q and p.get("price_source") == "MARKET" and p.get("value") is not None:
            base = q * pc[0]
            p["prev_price_pln"], p["prev_date"] = pc[0], pc[1].isoformat()
            p["day_change"] = D(p["value"]) - base
            p["day_change_pct"] = (p["day_change"] / base) if base else None
    return {"ref_date": ref.isoformat()}


def income_12m(cur, accounts, today=None):
    """Dochody z ostatnich 12 miesiecy (dni (dzis - 365, dzis]) w PLN: dywidendy netto, odsetki netto, nagrody i razem."""
    today = today or date.today()
    start = today - timedelta(days=INCOME_12M_DAYS)
    cur.execute("""select coalesce(sum(cash_amount) filter (where txn_type in ('DIVIDEND','TAX_WITHHOLDING')), 0),
                          coalesce(sum(cash_amount) filter (where txn_type in ('INTEREST','TAX_INTEREST')), 0),
                          coalesce(sum(cash_amount) filter (where txn_type = 'REWARD'), 0)
                   from transactions where status = 'ACTIVE' and cash_currency = 'PLN' and account_id = any(%s)
                     and (occurred_at at time zone 'UTC')::date > %s and (occurred_at at time zone 'UTC')::date <= %s""", (list(accounts), start, today))
    div, inte, rew = (D(x) for x in cur.fetchone())
    return {"dividends": div, "interest": inte, "rewards": rew, "total": div + inte + rew, "from": (start + timedelta(days=1)).isoformat(), "to": today.isoformat()}


def closed_positions(cur, accounts):
    """Sprzedaze pogrupowane po instrumencie: ilosc sprzedana, koszt sprzedanych partii, przychod, zysk zrealizowany, daty, ilosc nadal posiadana."""
    accounts = list(accounts)
    cur.execute("""select dl.listing_id,
                          (select min(la.broker_symbol) from listing_aliases la where la.listing_id = dl.listing_id and la.valid_to is null) as ticker,
                          l.display_name, sum(c.qty_closed), sum(c.cost_cash), sum(c.proceeds_cash), sum(c.realized_pnl),
                          min(dl.opened_at), max(t.occurred_at), count(distinct c.sell_txn_id),
                          array_agg(distinct f.code || ':' || a.product_type),
                          (select coalesce(sum(x.qty_remaining), 0) from derived_lots x where x.listing_id = dl.listing_id and x.account_id = any(%s))
                   from derived_lot_closures c join derived_lots dl on dl.lot_id = c.lot_id join transactions t on t.txn_id = c.sell_txn_id
                   join listings l on l.listing_id = dl.listing_id join accounts a on a.account_id = dl.account_id
                   join financial_institutions f on f.broker_id = a.broker_id
                   where dl.account_id = any(%s) group by dl.listing_id, l.display_name order by max(t.occurred_at) desc""", (accounts, accounts))
    out = []
    for lst, tick, name, qty, cost, proceeds, pnl, first_buy, last_sell, sells, accts, open_qty in cur.fetchall():
        cost, pnl = D(cost), D(pnl)
        out.append({"listing_id": lst, "ticker": tick, "instrument": name, "qty_sold": D(qty), "cost": cost, "proceeds": D(proceeds), "realized": pnl,
                    "realized_pct": (pnl / cost) if cost > 0 else None, "first_buy": first_buy.date().isoformat(), "last_sell": last_sell.date().isoformat(),
                    "sells": sells, "accounts": [{"broker": x.split(":")[0], "product_type": x.split(":")[1]} for x in accts],
                    "qty_open": D(open_qty), "closed": D(open_qty) <= D("0.00000001")})
    return out


def position_chart(cur, listing_id, accounts):
    """Wykres pozycji: zamkniecia dzienne (waluta notowania, ceny skorygowane o splity - jak u dostawcy) od miesiaca przed pierwsza transakcja
    oraz zakupy i sprzedaze z ksiegi (kwoty i ceny jak w eksporcie)."""
    accounts = list(accounts)
    cur.execute("""select t.occurred_at, t.txn_type, t.quantity, t.price, t.price_currency, t.cash_amount, a.display_name
                   from transactions t join accounts a on a.account_id = t.account_id
                   where t.listing_id = %s and t.account_id = any(%s) and t.status = 'ACTIVE' and t.txn_type in ('BUY', 'SELL') order by t.occurred_at""",
                (listing_id, accounts))
    trades = [{"data": ts.date().isoformat(), "czas": ts.isoformat(), "typ": typ, "ilosc": q, "cena": p, "waluta_ceny": ccy, "kwota": amt, "konto": acc}
              for ts, typ, q, p, ccy, amt, acc in cur.fetchall()]
    start = (date.fromisoformat(trades[0]["data"]) - timedelta(days=31)) if trades else date.today() - timedelta(days=365)
    cur.execute("select price_date, close_price, currency from prices where source = %s and listing_id = %s and price_date >= %s order by price_date",
                (SOURCE, listing_id, start))
    rows = cur.fetchall()
    return {"waluta": rows[-1][2] if rows else None, "ceny": [[d.isoformat(), c] for d, c, _ in rows], "transakcje": trades}
