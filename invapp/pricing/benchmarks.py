"""
Indeksy do porownania na wykresie "Zwrot %" (decyzja uzytkownika 2026-11-12: Nasdaq, S&P 500, MSCI World, WIG20; zaznaczane, kilka naraz).

* Zrodlo: Yahoo Finance (ten sam dostawca co ceny historyczne), zamkniecia dzienne; waluta indeksu jest czescia KONFIGURACJI ponizej (nie zgadywana
  z symbolu) i sprawdzana z waluta zwrocona przez Yahoo, gdy ja podaje.
* Wszystko w PLN: poziom indeksu x kurs dnia {waluta}/PLN (fx_rates, YAHOO_1D) - tak jak portfel, wiec porownanie uwzglednia zmiany kursu walut.
* Indeksy cenowe (bez dywidend) - jak TWR portfela (dywidendy sa osobno). WIG20: Yahoo nie udostepnia historii samego indeksu, wiec uzywany jest
  ETF Beta WIG20TR (wersja z dywidendami, po oplacie funduszu) - opisane w interfejsie.
* Dni bez notowan (weekendy, swieta) - ostatnie zamkniecie (maks. 7 dni wstecz).
"""
from bisect import bisect_right
from datetime import date, timedelta

from .history import OVERLAP_DAYS, refresh_fx
from .providers import fetch_all

SOURCE = "YAHOO_1D"
BENCHMARKS = [
    {"key": "ndx", "label": "Nasdaq-100", "symbol": "^NDX", "currency": "USD", "opis": "indeks cenowy Nasdaq-100 (USD → PLN)"},
    {"key": "spx", "label": "S&P 500", "symbol": "^GSPC", "currency": "USD", "opis": "indeks cenowy S&P 500 (USD → PLN)"},
    {"key": "msci", "label": "MSCI World", "symbol": "^990100-USD-STRD", "currency": "USD", "opis": "indeks cenowy MSCI World (USD → PLN)"},
    {"key": "wig20", "label": "WIG20", "symbol": "ETFBW20TR.WA", "currency": "PLN",
     "opis": "ETF Beta WIG20TR (PLN, z dywidendami) – Yahoo nie podaje historii samego indeksu WIG20"},
]
MAX_GAP = 7


def history_start(conn, default=date(2024, 1, 1)):
    cur = conn.cursor()
    cur.execute("select min((occurred_at at time zone 'UTC')::date) from transactions where status = 'ACTIVE'")
    first = cur.fetchone()[0]
    return min(default, first) if first else default


def refresh(conn, provider, start=None):
    """Pobiera brakujace zamkniecia indeksow (przyrostowo) i kursy walut indeksow. Blad jednego indeksu nie przerywa pozostalych."""
    from psycopg2.extras import execute_values
    start = start or history_start(conn)
    cur = conn.cursor(); res = {"indeksy": {}, "errors": []}
    cur.execute("select symbol, min(price_date), max(price_date) from benchmark_prices where source = %s group by symbol", (SOURCE,))
    known = {s: (lo, hi) for s, lo, hi in cur.fetchall()}
    since = {b["symbol"]: max(start, known[b["symbol"]][1] - timedelta(days=OVERLAP_DAYS))
             if b["symbol"] in known and known[b["symbol"]][0] <= start + timedelta(days=MAX_GAP) else start for b in BENCHMARKS}
    fetched = fetch_all(lambda s: provider.daily(s, since[s]), [b["symbol"] for b in BENCHMARKS])
    for b in BENCHMARKS:
        h, err = fetched[b["symbol"]]
        if err is None and h.get("currency") and h["currency"] != b["currency"]:
            err = ValueError(f"waluta Yahoo {h['currency']} != {b['currency']}")
        if err is not None:
            res["errors"].append(f"{b['label']}: {type(err).__name__}"); continue
        last = {d: c for d, c in h["closes"] if c and c > 0}          # Yahoo potrafi podac ten sam dzien dwa razy (sesja w toku) - zostaje ostatni
        rows = [(b["symbol"], d, c, b["currency"], SOURCE) for d, c in sorted(last.items())]
        if rows:
            execute_values(cur, """insert into benchmark_prices(symbol,price_date,close_price,currency,source) values %s
                                   on conflict (symbol, price_date, source) do update set close_price = excluded.close_price, fetched_at = now()""", rows, page_size=1000)
        res["indeksy"][b["key"]] = len(rows)
    fx = refresh_fx(conn, provider, {b["currency"] for b in BENCHMARKS}, start)
    res["errors"] += fx["errors"]
    return res


def _last_on(xs, vals, d):
    i = bisect_right(xs, d)
    if i and (d - xs[i - 1]).days <= MAX_GAP:
        return vals[i - 1]
    return None


def levels(conn, dates):
    """Poziomy indeksow w PLN dla podanych dni: {"indeksy": [opis], "poziomy": {key: [float | None]}}. Brak danych -> None (bez zgadywania)."""
    cur = conn.cursor(); out = {}
    fx = {}
    cur.execute("select base, rate_date, rate from fx_rates where source = %s and quote = 'PLN' order by base, rate_date", (SOURCE,))
    for base, d, r in cur.fetchall():
        xs, vs = fx.setdefault(base, ([], []))
        xs.append(d); vs.append(float(r))
    for b in BENCHMARKS:
        cur.execute("select price_date, close_price from benchmark_prices where symbol = %s and source = %s order by price_date", (b["symbol"], SOURCE))
        rows = cur.fetchall()
        xs, vs = [r[0] for r in rows], [float(r[1]) for r in rows]
        lv = []
        for d in dates:
            c = _last_on(xs, vs, d)
            k = 1.0 if b["currency"] == "PLN" else (_last_on(*fx[b["currency"]], d) if b["currency"] in fx else None)
            lv.append(c * k if c is not None and k is not None else None)
        out[b["key"]] = lv
    return {"indeksy": [{k: b[k] for k in ("key", "label", "opis", "currency")} for b in BENCHMARKS], "poziomy": out}
