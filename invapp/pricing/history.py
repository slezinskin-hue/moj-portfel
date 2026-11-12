"""
Historia cen i wykres portfela (zakres: papiery wartosciowe; wolne srodki ignorowane zgodnie z poleceniem uzytkownika).

  verify_history_mappings(conn, provider) - dla KAZDEGO notowania z transakcjami: symbol dostawcy -> historia dzienna -> WERYFIKACJA:
        waluta dostawcy == waluta notowania w bazie, a ceny ZAKUPOW z ksiegi (po korektach CONFIRMED) zgadzaja sie z zamknieciem z dnia (>= 75% transakcji w [0,8; 1,25]).
  refresh_history(conn, provider) - zapis cen dziennych (upsert) i kursow dziennych do prices / fx_rates (zrodlo YAHOO_1D).
  build_series(conn, scope_code) - dzienny szereg: wartosc papierow, wplaty netto (zakupy - sprzedaze), wynik; z jawna flaga dni oszacowanych.

Definicje (decyzja uzytkownika 2026-11-11): "wplata" = rzeczywisty ZAKUP (gotowka z ksiegi), "wyplata" = rzeczywista SPRZEDAZ (przychod z ksiegi); dywidendy i wolne srodki
nie wchodza do wykresu. Wartosc = ilosc (partie po korektach CONFIRMED) x cena zamkniecia (skorygowana o splity) x kurs dnia. Brak ceny -> ostatnia cena z transakcji
(jawnie oznaczone jako oszacowanie), nigdy cicho zgadniete.
"""
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from psycopg2.extras import Json

from invapp.core.common import jsonable
from .providers import YahooProvider, fetch_all

SOURCE = "YAHOO_1D"
HIST_PROVIDER = "YAHOO_1D"
STALE_DAYS = 7
MATCH_SHARE = Decimal("0.75")


class HistoryYahoo(YahooProvider):
    """Historia dzienna z endpointu wykresow (ten sam dostawca co notowania na zywo; siec wstrzykiwana)."""

    def daily(self, provider_symbol, start):
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(time.time()) + 86400
        body = self.fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}?interval=1d&period1={p1}&period2={p2}")
        import json
        try:
            res = json.loads(body)["chart"]["result"][0]
            meta, ts, closes = res["meta"], res["timestamp"], res["indicators"]["quote"][0]["close"]
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Nieczytelna historia dla {provider_symbol}") from e
        raw = meta.get("currency")
        unit = "MINOR_1_100" if raw in ("GBp", "GBX", "ZAc", "ILA") else "MAJOR"
        ccy = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(raw, raw)
        off = meta.get("gmtoffset") or 0
        out = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            d = (datetime.fromtimestamp(t, timezone.utc) + timedelta(seconds=off)).date()
            out.append((d, Decimal(str(c)) / (100 if unit == "MINOR_1_100" else 1)))
        return {"currency": ccy, "raw_currency": raw, "unit": unit, "closes": out, "exchange": meta.get("fullExchangeName")}


def _close_on(closes, d):
    """Zamkniecie z dnia d albo z ostatniej sesji przed (maks. 5 dni), w ostatecznosci pierwsza po."""
    dates = [x[0] for x in closes]
    i = bisect_right(dates, d)
    if i and (d - dates[i - 1]).days <= 5:
        return closes[i - 1][1]
    if i < len(closes) and (closes[i][0] - d).days <= 5:
        return closes[i][1]
    return None


RECHECK_DAYS = 7


def verify_history_mappings(conn, provider, start=date(2024, 1, 1), pause=0.3, broker_code="XTB", recheck_days=RECHECK_DAYS):
    """Powiazania juz ZWERYFIKOWANE w ostatnich recheck_days dniach (z ta sama waluta) nie sa sprawdzane ponownie; historie pobierane rownolegle."""
    cur = conn.cursor(); out = []
    cur.execute("""select l.listing_id, la.broker_symbol, l.trading_currency from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   where exists (select 1 from transactions t where t.listing_id = l.listing_id and t.txn_type in ('BUY','SELL'))
                     and la.broker_id = (select broker_id from financial_institutions where code = %s)
                     and not exists (select 1 from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = %s and m.status = 'VERIFIED'
                                       and m.quote_currency = l.trading_currency and m.checked_at > now() - make_interval(days => %s))
                   order by la.broker_symbol""", (broker_code, HIST_PROVIDER, recheck_days))
    rows = cur.fetchall()
    syms = {r[0]: provider.symbol_for(r[1]) for r in rows}
    fetched = fetch_all(lambda s: provider.daily(s, start), [syms[r[0]] for r in rows if syms[r[0]] and r[2] is not None])
    for listing_id, broker_symbol, ccy in rows:
        sym = syms[listing_id]; ev = {"broker_symbol": broker_symbol, "listing_currency": ccy}; status = None; h = None
        if not sym:
            status, ev["reason"] = "PENDING", "brak reguly symbolu u dostawcy dla tego rynku"
        elif ccy is None:
            status, ev["reason"] = "PENDING", "waluta notowania w bazie niezweryfikowana"
        else:
            try:
                h, err = fetched[sym]
                if err is not None:
                    raise err
                ev.update({"provider_currency": h["raw_currency"], "provider_exchange": h["exchange"], "days": len(h["closes"])})
                if h["currency"] != ccy:
                    status, ev["reason"] = "REJECTED", f"waluta dostawcy {h['raw_currency']} != waluta notowania {ccy}"
                elif not h["closes"]:
                    status, ev["reason"] = "REJECTED", "pusta historia"
                else:
                    # porownanie z ZAKUPAMI w jednostkach po potwierdzonych korektach (partie: price_adjusted), bo dostawca podaje ceny skorygowane o splity
                    cur.execute("""select (t.occurred_at at time zone 'UTC')::date, coalesce(dl.price_adjusted, t.price) from transactions t
                                   left join derived_lots dl on dl.open_txn_id = t.txn_id where t.listing_id = %s and t.txn_type = 'BUY' and t.price is not null""", (listing_id,))
                    ratios = []
                    for d, price in cur.fetchall():
                        c = _close_on(h["closes"], d)
                        if c:
                            ratios.append(Decimal(price) / c)
                    ok = [r for r in ratios if Decimal("0.8") <= r <= Decimal("1.25")]
                    ev.update({"trades_compared": len(ratios), "trades_matching": len(ok)})
                    if ratios and Decimal(len(ok)) / len(ratios) >= MATCH_SHARE:
                        status = "VERIFIED"
                    else:
                        status = "REJECTED"; reason = f"ceny zakupow zgadzaja sie z historia tylko dla {len(ok)} z {len(ratios)} (wymagane >= 75%)"
                        if ratios:
                            med = sorted(ratios)[len(ratios) // 2]; ev["median_ratio"] = med.quantize(Decimal("0.01"))
                            for k in (2, 3, 4, 5, 8, 10, 20):
                                if abs(med / k - 1) < Decimal("0.12") or abs(med * k - 1) < Decimal("0.12"):
                                    reason += f"; iloraz cen ~{'x' if med > 1 else '1/'}{k} wskazuje na NIEWYKRYTY split lub odwrocony split w ksiedze (do potwierdzenia ze zrodla oficjalnego)"; break
                        ev["reason"] = reason
            except Exception as e:  # noqa: BLE001
                status, ev["reason"], h = "PENDING", f"blad pobrania: {type(e).__name__}", None
        cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,reference_exchange,checked_at,evidence)
                       values (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       on conflict (listing_id, provider) do update set provider_symbol=excluded.provider_symbol, status=excluded.status, quote_currency=excluded.quote_currency,
                         price_unit=excluded.price_unit, reference_exchange=excluded.reference_exchange, checked_at=excluded.checked_at, evidence=excluded.evidence""",
                    (listing_id, HIST_PROVIDER, sym or "", status, ccy if status == "VERIFIED" else None, "MAJOR" if status == "VERIFIED" else None,
                     h["exchange"] if h else None, Json(jsonable(ev))))
        out.append({"symbol": broker_symbol, "provider_symbol": sym, "status": status, "reason": ev.get("reason"),
                    "matching": f"{ev.get('trades_matching')}/{ev.get('trades_compared')}" if "trades_compared" in ev else ""})
    return out


OVERLAP_DAYS = 10       # przy dopelnianiu historii pobieramy ostatnie dni ponownie (korekty zamkniec u dostawcy)


def refresh_history(conn, provider, start=date(2024, 1, 1), pause=0.3):
    """Ceny dzienne i kursy dzienne. Przyrostowo: dla notowania, ktore ma juz historie, tylko od (ostatni dzien - OVERLAP_DAYS);
    zapytania rownolegle, zapis wsadowy (upsert)."""
    from psycopg2.extras import execute_values
    cur = conn.cursor(); res = {"listings": 0, "price_rows": 0, "fx": {}, "errors": []}
    cur.execute("""select m.listing_id, m.provider_symbol, m.quote_currency,
                          (select max(p.price_date) from prices p where p.listing_id = m.listing_id and p.source = %s)
                   from listing_provider_symbols m where m.provider = %s and m.status = 'VERIFIED' order by m.provider_symbol""", (SOURCE, HIST_PROVIDER))
    rows = cur.fetchall()
    since = {sym: max(start, last - timedelta(days=OVERLAP_DAYS)) if last else start for _, sym, _, last in rows}
    fetched = fetch_all(lambda s: provider.daily(s, since[s]), [r[1] for r in rows])
    currencies = set()
    for listing_id, sym, ccy, _ in rows:
        h, err = fetched[sym]
        if err is not None:
            res["errors"].append(f"{sym}: {type(err).__name__}"); continue
        currencies.add(ccy)
        if h["closes"]:
            execute_values(cur, """insert into prices(listing_id,price_date,close_price,currency,source) values %s
                                   on conflict (listing_id, price_date, source) do update set close_price = excluded.close_price, currency = excluded.currency, fetched_at = now()""",
                           [(listing_id, d, c, ccy, SOURCE) for d, c in sorted(dict(h["closes"]).items())], page_size=1000)   # dzien raz (ostatni)
        res["price_rows"] += len(h["closes"])
        res["listings"] += 1
    fx = refresh_fx(conn, provider, currencies, start)
    res["fx"], res["errors"] = fx["fx"], res["errors"] + fx["errors"]
    return res


def refresh_fx(conn, provider, currencies, start=date(2024, 1, 1)):
    """Kursy dzienne {waluta}/PLN (YAHOO_1D) dla podanych walut; przyrostowo od (ostatni dzien - OVERLAP_DAYS), ale nie pozniej niz `start`
    (gdy potrzebna jest wczesniejsza historia, niz juz zapisana - np. dla indeksow porownawczych)."""
    from psycopg2.extras import execute_values
    cur = conn.cursor(); res = {"fx": {}, "errors": []}
    fx_list = sorted(set(currencies) - {"PLN"})
    cur.execute("select base, min(rate_date), max(rate_date) from fx_rates where source = %s and quote = 'PLN' group by base", (SOURCE,))
    known = {b: (lo, hi) for b, lo, hi in cur.fetchall()}
    fx_since = {c: max(start, known[c][1] - timedelta(days=OVERLAP_DAYS)) if c in known and known[c][0] <= start + timedelta(days=5) else start for c in fx_list}
    fx_fetched = fetch_all(lambda c: provider.daily(f"{c}PLN=X", fx_since[c]), fx_list)
    for ccy in fx_list:
        h, err = fx_fetched[ccy]
        if err is not None:
            res["errors"].append(f"{ccy}PLN: {type(err).__name__}"); continue
        if h["closes"]:
            execute_values(cur, """insert into fx_rates(rate_date,base,quote,rate,source) values %s
                                   on conflict (rate_date, base, quote, source) do update set rate = excluded.rate""",
                           [(d, ccy, "PLN", c, SOURCE) for d, c in sorted(dict(h["closes"]).items())], page_size=1000)   # dzien raz (ostatni)
        res["fx"][ccy] = len(h["closes"])
    return res


DEPOSIT_FLOWS = ("CASH_DEPOSIT", "CASH_WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT")     # przeplywy przez granice konta (bank <-> broker)
# NAGRODY brokera (np. darmowa akcja na start w Trade Republic) to CZYSTY ZYSK, nie wplata (decyzja uzytkownika 2026-11-12: "dostalem za darmo,
# nie fair uznac to jako wplate"). Konto z lokata: nagroda nie jest przeplywem (zwieksza wartosc = zysk). Konto bez lokaty: zakup oplacony nagroda
# nie jest wplata wlasnych pieniedzy - kwota nagrody pomniejsza wplaty dnia.
SERIES_INFLOWS = DEPOSIT_FLOWS


def build_series(conn, scope_code="BROKER:XTB", end=None):
    """Granica aktywow zalezy od konta: zwykle konto = papiery (wplata = zakup, wyplata = sprzedaz); konto z gotowka jako LOKATA (core/deposits.py) =
    papiery + lokata (wplata = przelew na konto, wyplata = przelew z konta; zakupy i sprzedaze to przesuniecia wewnatrz aktywow, a odsetki - zwrot)."""
    from invapp.core import deposits as dep
    cur = conn.cursor(); end = end or date.today()
    cur.execute("select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s", (scope_code,))
    accounts = [r[0] for r in cur.fetchall()]
    dep_daily, dep_accounts = defaultdict(Decimal), set()
    for acc in accounts:
        m = dep.account_deposit(cur, acc, end)
        if m:
            dep_accounts.add(acc)
            for d, v in m["daily"]:
                dep_daily[d] += v
    cur.execute("""select dl.listing_id, (dl.opened_at at time zone 'UTC')::date, dl.qty_initial, dl.cost_cash_initial, dl.account_id, dl.opened_at from derived_lots dl where dl.account_id = any(%s)""", (accounts,))
    lots = cur.fetchall()
    cur.execute("""select dl.listing_id, (t.occurred_at at time zone 'UTC')::date, c.qty_closed, c.proceeds_cash, c.cost_cash, dl.account_id, t.occurred_at
                   from derived_lot_closures c join derived_lots dl on dl.lot_id = c.lot_id join transactions t on t.txn_id = c.sell_txn_id where dl.account_id = any(%s)""", (accounts,))
    closures = cur.fetchall()
    cur.execute("""select (t.occurred_at at time zone 'UTC')::date, t.cash_amount from transactions t
                   where t.account_id = any(%s) and t.status = 'ACTIVE' and t.txn_type = any(%s) and t.cash_currency = 'PLN'""", (list(dep_accounts), list(SERIES_INFLOWS)))
    dep_flows = cur.fetchall()
    if not lots and not dep_daily:
        return {"series": [], "estimated_days": 0, "no_history": [], "first_date": None}
    dqty = defaultdict(lambda: defaultdict(Decimal)); buys = defaultdict(Decimal); sells = defaultdict(Decimal); fallback = defaultdict(list)
    for lst, d, q, cost, acc, ts in lots:
        dqty[lst][d] += q; fallback[lst].append((d, ts, cost / q))
        if acc not in dep_accounts:
            buys[d] += cost
    for lst, d, q, proceeds, cost, acc, ts in closures:
        dqty[lst][d] -= q
        if acc not in dep_accounts:
            sells[d] += proceeds
        if q:
            fallback[lst].append((d, ts, proceeds / q))
    for d, amt in dep_flows:
        if amt >= 0:
            buys[d] += amt
        else:
            sells[d] += -amt
    cur.execute("""select (t.occurred_at at time zone 'UTC')::date, t.cash_amount from transactions t
                   where t.account_id = any(%s) and t.status = 'ACTIVE' and t.txn_type = 'REWARD' and t.cash_currency = 'PLN'""",
                ([a for a in accounts if a not in dep_accounts],))
    for d, amt in cur.fetchall():
        buys[d] -= amt                                         # nagroda wydana na papiery: to nie sa wlasne pieniadze (zysk, nie wplata)
    for lst in fallback:
        fallback[lst].sort(key=lambda x: (x[0], x[1]))           # ta sama data: OSTATNIA transakcja dnia (wczesniej: najwyzsza cena)
    listings = set(dqty)
    cur.execute("select listing_id, price_date, close_price, currency from prices where source = %s and listing_id = any(%s) order by listing_id, price_date", (SOURCE, list(listings)))
    px = defaultdict(list)
    for lst, d, c, ccy in cur.fetchall():
        px[lst].append((d, c, ccy))
    cur.execute("select base, rate_date, rate from fx_rates where source = %s and quote = 'PLN' order by base, rate_date", (SOURCE,))
    fx = defaultdict(list)
    for b, d, r in cur.fetchall():
        fx[b].append((d, r))
    ptr = {l: 0 for l in listings}; fptr = {l: 0 for l in listings}; xptr = {b: 0 for b in fx}
    qty = defaultdict(Decimal); series = []; gross = Decimal(0); sold = Decimal(0)
    first = min(min((r[1] for r in lots), default=date.max), min((r[1] for r in closures), default=date.max), min(dep_daily, default=date.max))
    estimated_days = 0; no_history = sorted({l for l in listings if not px.get(l)})
    d = first
    while d <= end:
        for lst in listings:
            qty[lst] += dqty[lst].get(d, Decimal(0))
        gross += buys.get(d, Decimal(0)); sold += sells.get(d, Decimal(0))
        value = Decimal(0); est = 0; held = 0
        for lst in listings:
            if qty[lst] <= Decimal("0.00000001"):
                continue
            held += 1
            rows = px.get(lst, [])
            while ptr[lst] < len(rows) and rows[ptr[lst]][0] <= d:
                ptr[lst] += 1
            price_pln = None
            if ptr[lst] > 0:
                pd, pc, ccy = rows[ptr[lst] - 1]
                if (d - pd).days <= STALE_DAYS:
                    if ccy == "PLN":
                        price_pln = pc
                    else:
                        fr = fx.get(ccy, [])
                        while xptr.get(ccy, 0) < len(fr) and fr[xptr[ccy]][0] <= d:
                            xptr[ccy] += 1
                        if xptr.get(ccy, 0) > 0 and (d - fr[xptr[ccy] - 1][0]).days <= STALE_DAYS:
                            price_pln = pc * fr[xptr[ccy] - 1][1]
            if price_pln is None:                                   # oszacowanie: ostatnia cena z transakcji (PLN za sztuke), jawnie oznaczone
                fb = fallback[lst]
                while fptr[lst] < len(fb) and fb[fptr[lst]][0] <= d:
                    fptr[lst] += 1
                price_pln = fb[fptr[lst] - 1][2] if fptr[lst] > 0 else Decimal(0)
                est += 1
            value += qty[lst] * price_pln
        estimated_days += 1 if est else 0
        securities = value
        deposit = dep_daily.get(d, Decimal(0))
        value = securities + deposit
        series.append({"date": d.isoformat(), "value": value.quantize(Decimal("0.01")), "securities": securities.quantize(Decimal("0.01")),
                       "deposits": deposit.quantize(Decimal("0.01")), "invested_gross": gross.quantize(Decimal("0.01")),
                       "sold": sold.quantize(Decimal("0.01")), "invested_net": (gross - sold).quantize(Decimal("0.01")),
                       "gain": (value - (gross - sold)).quantize(Decimal("0.01")), "positions": held, "estimated_positions": est})
        d += timedelta(days=1)
    return {"series": series, "estimated_days": estimated_days, "no_history": no_history, "first_date": first.isoformat()}
