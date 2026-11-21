"""
Historia cen i wykres portfela (zakres: papiery wartosciowe; wolne srodki ignorowane zgodnie z poleceniem uzytkownika).

  verify_history_mappings(conn, provider) - dla KAZDEGO notowania z transakcjami: symbol dostawcy -> historia dzienna -> WERYFIKACJA:
        waluta dostawcy == waluta notowania w bazie, a ceny ZAKUPOW z ksiegi (po korektach CONFIRMED) zgadzaja sie z zamknieciem z dnia (>= 75% transakcji w [0,8; 1,25]).
  refresh_history(conn, provider) - zapis cen dziennych (upsert) i kursow dziennych do prices / fx_rates (zrodlo YAHOO_1D).
  build_series(conn, scope_code) - dzienny szereg: wartosc papierow, wplaty netto (zakupy - sprzedaze), wynik; z jawna flaga dni oszacowanych.

Definicje (decyzja uzytkownika 2026-11-11): "wplata" = rzeczywisty ZAKUP (gotowka z ksiegi), "wyplata" = rzeczywista SPRZEDAZ (przychod z ksiegi); dywidendy i wolne srodki
nie wchodza do wykresu. Wartosc = ilosc (partie po korektach CONFIRMED) x cena zamkniecia (w jednostkach ksiegi: splity dostawcy spoza ksiegi cofniete,
migracja 0021) x kurs dnia; bez wlasnej ceny - cena zastepcza (ten sam ISIN z innej gieldy, zweryfikowana). Brak ceny -> ostatnia cena z transakcji
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
        body = self.fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}?interval=1d&period1={p1}&period2={p2}&events=split")
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
        splits = []
        for ev in ((res.get("events") or {}).get("splits") or {}).values():    # dzien gieldy (jak zamkniecia): pierwszy dzien notowan po splicie
            try:
                splits.append(((datetime.fromtimestamp(int(ev["date"]), timezone.utc) + timedelta(seconds=off)).date(),
                               Decimal(str(ev["numerator"])), Decimal(str(ev["denominator"]))))
            except (KeyError, ValueError, TypeError):
                continue
        return {"currency": ccy, "raw_currency": raw, "unit": unit, "closes": out, "exchange": meta.get("fullExchangeName"), "splits": sorted(splits)}


PROXY_PROVIDER = "YAHOO_1D_PROXY"     # cena zastepcza: ten sam ISIN z innej gieldy (migracja 0021), tylko w dni bez wlasnej ceny
PROXY_SOURCE = "YAHOO_1D_PROXY"
SPLIT_MATCH_DAYS = 31
# ISIN notowania: z papieru, a gdy brak - ze specyfikacji brokera dla jego symbolu (ISIN papieru jest unikalny, wiec druga linia tego samego
# funduszu u tego samego brokera, np. VUAA.DE obok VUAA.UK, ma ISIN tylko w specyfikacji XTB)
ISIN_SQL = """coalesce(s.isin, (select bis.isin from broker_instrument_specs bis join listing_aliases a2 on a2.broker_id = bis.broker_id and a2.broker_symbol = bis.symbol
                               where a2.listing_id = l.listing_id and a2.valid_to is null and bis.isin is not null order by bis.valid_from desc limit 1))"""


def _unmatched(splits, confirmed):
    """Splity dostawcy, ktorych NIE ma wsrod potwierdzonych korekt ksiegi (ten sam wspolczynnik, data +-31 dni albo nieznana):
    [(dzien splitu, wspolczynnik)]. Dostawca podaje zamkniecia przeliczone o wszystkie pozniejsze splity; ksiega - tylko o potwierdzone.
    Dla dnia d przed takim splitem faktyczna cena = zamkniecie dostawcy x wspolczynnik (decyzja uzytkownika 2026-11-21, migracja 0021)."""
    out = []
    for sd, num, den in splits:
        f = Decimal(num) / Decimal(den)
        if f == 1:
            continue
        if any(abs(Decimal(r) - f) < Decimal("0.000001") and (ed is None or abs((ed - sd).days) <= SPLIT_MATCH_DAYS) for r, ed in confirmed):
            continue
        out.append((sd, f))
    return sorted(out)


def _factor(unmatched, d):
    """Wspolczynnik cofniecia przeliczenia dla dnia d: iloczyn splitow spoza ksiegi PO dniu d (dzien splitu i pozniej = 1)."""
    f = Decimal(1)
    for sd, k in unmatched:
        if d < sd:
            f *= k
    return f


def _confirmed_actions(cur, listing_ids):
    """{notowanie: [(wspolczynnik, data)]} potwierdzonych korekt ksiegi (te same, ktore stosuje derive)."""
    cur.execute("""select listing_id, ratio_new / ratio_old, effective_date from corporate_actions where status = 'CONFIRMED' and listing_id = any(%s)""",
                (list(listing_ids),))
    out = defaultdict(list)
    for lst, r, ed in cur.fetchall():
        out[lst].append((Decimal(r), ed))
    return out


def split_factors(cur, listing_ids, provider=None):
    """{notowanie: [(dzien splitu, wspolczynnik)]} - splity dostawcy (provider_splits) spoza potwierdzonych korekt ksiegi."""
    ids = list(listing_ids)
    if not ids:
        return {}
    cur.execute("select listing_id, split_date, ratio_new, ratio_old from provider_splits where provider = %s and listing_id = any(%s) order by 1, 2",
                (provider or HIST_PROVIDER, ids))
    raw = defaultdict(list)
    for lst, sd, n, o in cur.fetchall():
        raw[lst].append((sd, n, o))
    conf = _confirmed_actions(cur, raw) if raw else {}
    return {lst: u for lst, sp in raw.items() if (u := _unmatched(sp, conf.get(lst, [])))}


def _store_splits(cur, listing_id, provider, symbol, splits):
    for sd, num, den in splits:
        cur.execute("""insert into provider_splits(listing_id, provider, split_date, ratio_new, ratio_old, provider_symbol) values (%s,%s,%s,%s,%s,%s)
                       on conflict (listing_id, provider, split_date) do update set ratio_new = excluded.ratio_new, ratio_old = excluded.ratio_old,
                         provider_symbol = excluded.provider_symbol, fetched_at = now()""", (listing_id, provider, sd, num, den, symbol))


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


def _check_candidate(cur, listing_id, ccy, h, confirmed):
    """Weryfikacja historii kandydata: waluta == waluta notowania i ceny ZAKUPOW z ksiegi (po korektach CONFIRMED) zgodne z zamknieciem dnia
    (>= 75% w [0,8; 1,25]) - zamkniecie po cofnieciu przeliczenia o splity spoza ksiegi. Zwraca (status, dowody)."""
    ev = {"provider_currency": h["raw_currency"], "provider_exchange": h["exchange"], "days": len(h["closes"])}
    um = _unmatched(h.get("splits") or [], confirmed)
    if um:
        ev["provider_splits_outside_ledger"] = [(sd.isoformat(), k) for sd, k in um]
    if h["currency"] != ccy:
        return "REJECTED", dict(ev, reason=f"waluta dostawcy {h['raw_currency']} != waluta notowania {ccy}")
    if not h["closes"]:
        return "REJECTED", dict(ev, reason="pusta historia")
    # porownanie z ZAKUPAMI w jednostkach po potwierdzonych korektach (partie: price_adjusted); dostawca podaje ceny skorygowane o WSZYSTKIE splity,
    # wiec splity, ktorych ksiega nie stosuje (pozycja zamknieta przed splitem), sa cofane
    cur.execute("""select (t.occurred_at at time zone 'UTC')::date, coalesce(dl.price_adjusted, t.price) from transactions t
                   left join derived_lots dl on dl.open_txn_id = t.txn_id where t.listing_id = %s and t.txn_type = 'BUY' and t.price is not null""", (listing_id,))
    ratios = []
    for d, price in cur.fetchall():
        c = _close_on(h["closes"], d)
        if c:
            ratios.append(Decimal(price) / (c * _factor(um, d)))
    ok = [r for r in ratios if Decimal("0.8") <= r <= Decimal("1.25")]
    ev.update({"trades_compared": len(ratios), "trades_matching": len(ok)})
    if ratios and Decimal(len(ok)) / len(ratios) >= MATCH_SHARE:
        return "VERIFIED", ev
    reason = f"ceny zakupow zgadzaja sie z historia tylko dla {len(ok)} z {len(ratios)} (wymagane >= 75%)"
    if ratios:
        med = sorted(ratios)[len(ratios) // 2]; ev["median_ratio"] = med.quantize(Decimal("0.01"))
        for k in (2, 3, 4, 5, 8, 10, 20):
            if abs(med / k - 1) < Decimal("0.12") or abs(med * k - 1) < Decimal("0.12"):
                reason += f"; iloraz cen ~{'x' if med > 1 else '1/'}{k} wskazuje na NIEWYKRYTY split (ani w ksiedze, ani u dostawcy) - do potwierdzenia ze zrodla oficjalnego"; break
    return "REJECTED", dict(ev, reason=reason)


def verify_history_mappings(conn, provider, start=date(2024, 1, 1), pause=0.3, broker_code="XTB", recheck_days=RECHECK_DAYS):
    """Powiazania juz ZWERYFIKOWANE w ostatnich recheck_days dniach (z ta sama waluta) nie sa sprawdzane ponownie; historie pobierane rownolegle.
    Symbol z reguly rynku (sufiks brokera) nie przechodzi weryfikacji -> kandydaci z wyszukiwania po ISIN (np. inny symbol tego samego funduszu
    na tej samej gieldzie), kazdy z ta sama weryfikacja (decyzja uzytkownika 2026-11-21)."""
    cur = conn.cursor(); out = []
    cur.execute("""select l.listing_id, la.broker_symbol, l.trading_currency, """ + ISIN_SQL + """ from listings l
                   join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   join securities s on s.security_id = l.security_id
                   where exists (select 1 from transactions t where t.listing_id = l.listing_id and t.txn_type in ('BUY','SELL'))
                     and la.broker_id = (select broker_id from financial_institutions where code = %s)
                     and not exists (select 1 from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = %s and m.status = 'VERIFIED'
                                       and m.quote_currency = l.trading_currency and m.checked_at > now() - make_interval(days => %s))
                   order by la.broker_symbol""", (broker_code, HIST_PROVIDER, recheck_days))
    rows = cur.fetchall()
    syms = {r[0]: provider.symbol_for(r[1]) for r in rows}
    fetched = fetch_all(lambda s: provider.daily(s, start), [syms[r[0]] for r in rows if syms[r[0]] and r[2] is not None])
    conf = _confirmed_actions(cur, [r[0] for r in rows]) if rows else {}
    for listing_id, broker_symbol, ccy, isin in rows:
        sym = syms[listing_id]; ev = {"broker_symbol": broker_symbol, "listing_currency": ccy}; status = None; h = None
        if ccy is None:
            status, ev["reason"] = "PENDING", "waluta notowania w bazie niezweryfikowana"
        else:
            if not sym:
                status, ev["reason"] = "PENDING", "brak reguly symbolu u dostawcy dla tego rynku"
            else:
                try:
                    h, err = fetched[sym]
                    if err is not None:
                        raise err
                    status, more = _check_candidate(cur, listing_id, ccy, h, conf.get(listing_id, []))
                    ev.update(more)
                except Exception as e:  # noqa: BLE001
                    status, ev["reason"], h = "PENDING", f"blad pobrania: {type(e).__name__}", None
            if status != "VERIFIED" and isin:                       # zapas: kandydaci po ISIN (wynik wyszukiwania nie jest dowodem - kazdy weryfikowany)
                tried = []
                try:
                    cands = [c["symbol"] for c in provider.search(isin) if c["symbol"] != sym]
                except Exception as e:  # noqa: BLE001
                    cands = []
                    tried.append({"isin_search_error": type(e).__name__})
                for cs in cands:
                    try:
                        hc = provider.daily(cs, start)
                        st, more = _check_candidate(cur, listing_id, ccy, hc, conf.get(listing_id, []))
                    except Exception as e:  # noqa: BLE001
                        st, more, hc = "PENDING", {"reason": f"blad pobrania: {type(e).__name__}"}, None
                    tried.append({"symbol": cs, "status": st, "reason": more.get("reason")})
                    if st == "VERIFIED":
                        first_reason = ev.get("reason")
                        ev = {k: v for k, v in ev.items() if k != "reason"}
                        ev.update(more)
                        ev.update({"isin": isin, "chosen_by": "ISIN", "rule_symbol": sym, "rule_symbol_result": first_reason})
                        status, sym, h = st, cs, hc
                        break
                if tried:
                    ev["isin_candidates"] = tried
        if status == "VERIFIED" and h:
            _store_splits(cur, listing_id, HIST_PROVIDER, sym, h.get("splits") or [])
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


def _refresh_provider(cur, provider, prov_code, source, start, res):
    """Jeden rodzaj powiazania (wlasna historia albo zastepcza): przyrostowo od (ostatni dzien - OVERLAP_DAYS), ale CALA historia, gdy:
    splity tego powiazania nie byly jeszcze sprawdzone (evidence.splits_checked) albo w pobranym oknie pojawil sie nowy split - dostawca
    przelicza wtedy wszystkie wczesniejsze zamkniecia, wiec zapisane ceny sprzed splitu musza zostac nadpisane (jedne jednostki w calej tabeli)."""
    from psycopg2.extras import execute_values
    cur.execute("""select m.listing_id, m.provider_symbol, m.quote_currency, m.evidence ? 'splits_checked',
                          (select max(p.price_date) from prices p where p.listing_id = m.listing_id and p.source = %s)
                   from listing_provider_symbols m where m.provider = %s and m.status = 'VERIFIED' order by m.provider_symbol""", (source, prov_code))
    rows = cur.fetchall()
    if not rows:
        return set()
    cur.execute("select listing_id, split_date from provider_splits where provider = %s and listing_id = any(%s)", (prov_code, [r[0] for r in rows]))
    known = defaultdict(set)
    for lst, sd in cur.fetchall():
        known[lst].add(sd)
    since = {sym: (max(start, last - timedelta(days=OVERLAP_DAYS)) if (last and checked) else start) for _, sym, _, checked, last in rows}
    fetched = fetch_all(lambda s_: provider.daily(s_, since[s_]), [r[1] for r in rows])
    full = [r for r in rows if fetched[r[1]][1] is None and since[r[1]] > start
            and any(sd not in known[r[0]] for sd, _, _ in fetched[r[1]][0].get("splits") or [])]
    if full:                                              # nowy split w oknie: cala historia od nowa
        refetched = fetch_all(lambda s_: provider.daily(s_, start), [r[1] for r in full])
        for r in full:
            fetched[r[1]] = refetched[r[1]]
    currencies = set()
    for listing_id, sym, ccy, checked, _ in rows:
        h, err = fetched[sym]
        if err is not None:
            res["errors"].append(f"{sym}: {type(err).__name__}"); continue
        currencies.add(ccy)
        if h["closes"]:
            execute_values(cur, """insert into prices(listing_id,price_date,close_price,currency,source) values %s
                                   on conflict (listing_id, price_date, source) do update set close_price = excluded.close_price, currency = excluded.currency, fetched_at = now()""",
                           [(listing_id, d, c, ccy, source) for d, c in sorted(dict(h["closes"]).items())], page_size=1000)   # dzien raz (ostatni)
        _store_splits(cur, listing_id, prov_code, sym, h.get("splits") or [])
        if not checked:
            cur.execute("""update listing_provider_symbols set evidence = evidence || jsonb_build_object('splits_checked', current_date::text)
                           where listing_id = %s and provider = %s""", (listing_id, prov_code))
        res["price_rows"] += len(h["closes"])
        res["listings"] += 1
    return currencies


def refresh_history(conn, provider, start=date(2024, 1, 1), pause=0.3):
    """Ceny dzienne (wlasne i zastepcze) i kursy dzienne; zapytania rownolegle, zapis wsadowy (upsert)."""
    cur = conn.cursor(); res = {"listings": 0, "price_rows": 0, "fx": {}, "errors": []}
    currencies = _refresh_provider(cur, provider, HIST_PROVIDER, SOURCE, start, res)
    currencies |= _refresh_provider(cur, provider, PROXY_PROVIDER, PROXY_SOURCE, start, res)
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


PROXY_MIN_OVERLAP = 20                  # dni wspolne wlasnej historii i kandydata
PROXY_TOL = Decimal("0.03")             # rozne gieldy i godziny zamkniecia (+ kurs walut): roznica w PLN do 3%
PROXY_SHARE = Decimal("0.95")


def _to_pln(amount, fxs, d):
    """Kwota w walucie x kurs dnia d (albo ostatni wczesniejszy, maks. 5 dni); waluta PLN -> fxs None. Brak kursu -> None."""
    if amount is None or fxs is None:
        return amount
    r = _close_on(fxs, d)
    return amount * r if r else None


def verify_proxy_mappings(conn, provider, start=date(2024, 1, 1), recheck_days=RECHECK_DAYS):
    """CENA ZASTEPCZA (decyzja uzytkownika 2026-11-21, migracja 0021): notowanie z ZWERYFIKOWANA wlasna historia, ktora zaczyna sie pozniej niz
    posiadanie (np. VUAA.DE: Yahoo od 2024-12-30, pozycja od 2024-02) -> kandydaci z wyszukiwania po ISIN (ten sam papier, inna gielda).
    Kandydat przechodzi, gdy: (1) w >= 20 dniach wspolnych z wlasna historia >= 95% dni ma wartosc w PLN w granicach 3%, (2) ceny zakupow sprzed
    wlasnej historii zgadzaja sie z zamknieciem kandydata w PLN (>= 75% w [0,8; 1,25]). Zweryfikowany: zapis powiazania YAHOO_1D_PROXY i cen
    (zrodlo YAHOO_1D_PROXY); wykres uzywa ich TYLKO w dni bez wlasnej ceny i oznacza je jako zastepcze."""
    from psycopg2.extras import execute_values
    cur = conn.cursor(); out = []
    cur.execute("""select m.listing_id, m.provider_symbol, m.quote_currency, """ + ISIN_SQL + """,
                          (select min(p.price_date) from prices p where p.listing_id = m.listing_id and p.source = %s),
                          (select min((dl.opened_at at time zone 'UTC')::date) from derived_lots dl where dl.listing_id = m.listing_id)
                   from listing_provider_symbols m join listings l on l.listing_id = m.listing_id join securities s on s.security_id = l.security_id
                   where m.provider = %s and m.status = 'VERIFIED'
                     and not exists (select 1 from listing_provider_symbols x where x.listing_id = m.listing_id and x.provider = %s
                                       and x.checked_at > now() - make_interval(days => %s))""", (SOURCE, HIST_PROVIDER, PROXY_PROVIDER, recheck_days))
    rows = [r for r in cur.fetchall() if r[3] and r[4] and r[5] and (r[4] - r[5]).days > STALE_DAYS]
    fx_cache = {}

    def fxs(ccy):
        if ccy == "PLN":
            return None
        if ccy not in fx_cache:
            fx_cache[ccy] = provider.daily(f"{ccy}PLN=X", start - timedelta(days=10))["closes"]
        return fx_cache[ccy]
    for listing_id, own_sym, own_ccy, isin, own_first, held_first in rows:
        ev = {"isin": isin, "own_symbol": own_sym, "own_history_from": own_first.isoformat(), "held_from": held_first.isoformat(), "candidates": []}
        cur.execute("select price_date, close_price from prices where listing_id = %s and source = %s order by price_date", (listing_id, SOURCE))
        own = [(d, Decimal(c)) for d, c in cur.fetchall()]
        cur.execute("""select (t.occurred_at at time zone 'UTC')::date, coalesce(dl.price_adjusted, t.price) from transactions t
                       left join derived_lots dl on dl.open_txn_id = t.txn_id
                       where t.listing_id = %s and t.txn_type = 'BUY' and t.price is not null and (t.occurred_at at time zone 'UTC')::date < %s""", (listing_id, own_first))
        buys = cur.fetchall()
        conf = _confirmed_actions(cur, [listing_id]).get(listing_id, [])
        best = None
        try:
            cands = [c["symbol"] for c in provider.search(isin) if c["symbol"] != own_sym]
        except Exception as e:  # noqa: BLE001
            cands = []; ev["reason"] = f"blad wyszukiwania: {type(e).__name__}"
        for cs in cands:
            row = {"symbol": cs}
            try:
                hc = provider.daily(cs, start)
                um = _unmatched(hc.get("splits") or [], conf)
                closes = [(d, c * _factor(um, d)) for d, c in hc["closes"]]
                fo, fc = fxs(own_ccy), fxs(hc["currency"])
                cand = dict(closes)
                pairs = []                                          # ten sam dzien sesji: kandydat / wlasne zamkniecie, oba w PLN
                for d, c in own:
                    a_, b_ = _to_pln(c, fo, d), _to_pln(cand.get(d), fc, d)
                    if a_ and b_:
                        pairs.append(b_ / a_)
                ok = [r for r in pairs if abs(r - 1) <= PROXY_TOL]
                bratios = []                                        # zakupy sprzed wlasnej historii: cena zakupu / zamkniecie kandydata, oba w PLN
                for d, price in buys:
                    a_, b_ = _to_pln(Decimal(price), fo, d), _to_pln(_close_on(closes, d), fc, d)
                    if a_ and b_:
                        bratios.append(a_ / b_)
                bok = [r for r in bratios if Decimal("0.8") <= r <= Decimal("1.25")]
                row.update({"currency": hc["currency"], "exchange": hc["exchange"], "days_before_own": sum(1 for d, _ in closes if d < own_first),
                            "overlap": len(pairs), "overlap_matching": len(ok), "buys_compared": len(bratios), "buys_matching": len(bok)})
                good = (len(pairs) >= PROXY_MIN_OVERLAP and Decimal(len(ok)) / len(pairs) >= PROXY_SHARE and row["days_before_own"] > 0
                        and (not bratios or Decimal(len(bok)) / len(bratios) >= MATCH_SHARE))
                row["status"] = "VERIFIED" if good else "REJECTED"
                if good and (best is None or row["days_before_own"] > best[1]["days_before_own"]):
                    best = (hc, row, closes)
            except Exception as e:  # noqa: BLE001
                row.update({"status": "PENDING", "error": type(e).__name__})
            ev["candidates"].append(row)
        status = "VERIFIED" if best else ("REJECTED" if cands else "PENDING")
        if not best and "reason" not in ev:
            ev["reason"] = "brak kandydata zgodnego z wlasna historia i cenami zakupow" if cands else "dostawca nie zna innego notowania tego ISIN"
        sym = best[1]["symbol"] if best else ""
        if best:
            ev.update({"chosen": sym, "splits_checked": date.today().isoformat()})
            hc = best[0]
            if hc["closes"]:
                execute_values(cur, """insert into prices(listing_id,price_date,close_price,currency,source) values %s
                                       on conflict (listing_id, price_date, source) do update set close_price = excluded.close_price, currency = excluded.currency, fetched_at = now()""",
                               [(listing_id, d, c, hc["currency"], PROXY_SOURCE) for d, c in sorted(dict(hc["closes"]).items())], page_size=1000)
            _store_splits(cur, listing_id, PROXY_PROVIDER, sym, hc.get("splits") or [])
        cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,reference_exchange,checked_at,evidence)
                       values (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       on conflict (listing_id, provider) do update set provider_symbol=excluded.provider_symbol, status=excluded.status, quote_currency=excluded.quote_currency,
                         price_unit=excluded.price_unit, reference_exchange=excluded.reference_exchange, checked_at=excluded.checked_at, evidence=excluded.evidence""",
                    (listing_id, PROXY_PROVIDER, sym, status, best[0]["currency"] if best else None, "MAJOR" if best else None,
                     best[0]["exchange"] if best else None, Json(jsonable(ev))))
        out.append({"listing_id": listing_id, "own_symbol": own_sym, "proxy_symbol": sym or None, "status": status, "currency": best[0]["currency"] if best else None})
    new_ccy = {r["currency"] for r in out if r["status"] == "VERIFIED"}
    if new_ccy:                                   # kurs dzienny waluty ceny zastepczej (wykres przelicza ja na PLN kursem dnia)
        refresh_fx(conn, provider, new_ccy, start)
    return out


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
        return {"series": [], "estimated_days": 0, "proxy_days": 0, "no_history": [], "first_date": None}
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
    cur.execute("select listing_id, price_date, close_price, currency, source from prices where source = any(%s) and listing_id = any(%s) order by listing_id, price_date",
                ([SOURCE, PROXY_SOURCE], list(listings)))
    px = defaultdict(list); px2 = defaultdict(list)                # wlasne zamkniecia i zastepcze (ten sam ISIN z innej gieldy, migracja 0021)
    for lst, d, c, ccy, src in cur.fetchall():
        (px if src == SOURCE else px2)[lst].append((d, c, ccy))
    # splity dostawcy spoza potwierdzonych korekt ksiegi: cena dnia sprzed splitu = zamkniecie dostawcy x wspolczynnik (migracja 0021)
    facs = split_factors(cur, listings); facs2 = split_factors(cur, listings, PROXY_PROVIDER) if px2 else {}
    cur.execute("select base, rate_date, rate from fx_rates where source = %s and quote = 'PLN' order by base, rate_date", (SOURCE,))
    fx = defaultdict(list)
    for b, d, r in cur.fetchall():
        fx[b].append((d, r))
    ptr = {l: 0 for l in listings}; ptr2 = {l: 0 for l in listings}; fptr = {l: 0 for l in listings}; xptr = {b: 0 for b in fx}
    qty = defaultdict(Decimal); series = []; gross = Decimal(0); sold = Decimal(0)
    first = min(min((r[1] for r in lots), default=date.max), min((r[1] for r in closures), default=date.max), min(dep_daily, default=date.max))
    estimated_days = 0; proxy_days = 0; no_history = sorted({l for l in listings if not px.get(l) and not px2.get(l)})

    def close_pln(rows, pt, lst, d, um):
        """Zamkniecie <= d (nie starsze niz STALE_DAYS) x wspolczynnik splitow spoza ksiegi x kurs dnia (nie starszy niz STALE_DAYS); brak -> None."""
        while pt[lst] < len(rows) and rows[pt[lst]][0] <= d:
            pt[lst] += 1
        if pt[lst] == 0:
            return None
        pd, pc, ccy = rows[pt[lst] - 1]
        if (d - pd).days > STALE_DAYS:
            return None
        if um:
            k = _factor(um, d)
            if k != 1:
                pc = pc * k
        if ccy == "PLN":
            return pc
        fr = fx.get(ccy, [])
        while xptr.get(ccy, 0) < len(fr) and fr[xptr[ccy]][0] <= d:
            xptr[ccy] += 1
        if xptr.get(ccy, 0) > 0 and (d - fr[xptr[ccy] - 1][0]).days <= STALE_DAYS:
            return pc * fr[xptr[ccy] - 1][1]
        return None

    # Wydajnosc: kazdego dnia przechodzimy tylko po pozycjach posiadanych (ilosc > 0), a ilosci przeliczamy tylko w dni, w ktorych sie zmieniaja.
    # Kolejnosc sumowania wartosci jest ta sama co w petli po wszystkich notowaniach (order), wiec wynik jest identyczny co do bitu.
    order = list(listings)
    change_days = {dd for lst in order for dd in dqty[lst]}
    EPS, ZERO, CENT = Decimal("0.00000001"), Decimal(0), Decimal("0.01")
    held_now = []
    d = first
    while d <= end:
        if d in change_days:
            for lst in order:
                qty[lst] += dqty[lst].get(d, ZERO)
            held_now = [l for l in order if qty[l] > EPS]
        gross += buys.get(d, ZERO); sold += sells.get(d, ZERO)
        value = ZERO; est = 0; prox = 0; held = 0
        for lst in held_now:
            held += 1
            price_pln = close_pln(px.get(lst, []), ptr, lst, d, facs.get(lst))
            if price_pln is None and px2.get(lst):                 # cena zastepcza (ten sam papier z innej gieldy), jawnie oznaczona
                price_pln = close_pln(px2[lst], ptr2, lst, d, facs2.get(lst))
                if price_pln is not None:
                    prox += 1
            if price_pln is None:                                   # oszacowanie: ostatnia cena z transakcji (PLN za sztuke), jawnie oznaczone
                fb = fallback[lst]
                while fptr[lst] < len(fb) and fb[fptr[lst]][0] <= d:
                    fptr[lst] += 1
                price_pln = fb[fptr[lst] - 1][2] if fptr[lst] > 0 else ZERO
                est += 1
            value += qty[lst] * price_pln
        estimated_days += 1 if est else 0
        proxy_days += 1 if prox else 0
        securities = value
        deposit = dep_daily.get(d, ZERO)
        value = securities + deposit
        series.append({"date": d.isoformat(), "value": value.quantize(CENT), "securities": securities.quantize(CENT),
                       "deposits": deposit.quantize(CENT), "invested_gross": gross.quantize(CENT),
                       "sold": sold.quantize(CENT), "invested_net": (gross - sold).quantize(CENT),
                       "gain": (value - (gross - sold)).quantize(CENT), "positions": held, "estimated_positions": est, "proxy_positions": prox})
        d += timedelta(days=1)
    return {"series": series, "estimated_days": estimated_days, "proxy_days": proxy_days, "no_history": no_history, "first_date": first.isoformat()}
