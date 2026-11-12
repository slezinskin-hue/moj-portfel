"""
Logika cen (etap C):
  verify_mappings(conn, provider) - dla notowan z otwartymi pozycjami: proponowany symbol dostawcy -> notowanie -> WERYFIKACJA:
      (1) waluta dostawcy == waluta notowania w bazie (ze specyfikacji brokera albo z reguly uzytkownika; notowanie bez waluty -> PENDING),
      (2) cena dostawcy w przedziale [1/TOL, TOL] wokol ostatniej ceny z migawki XTB (brak migawki -> PENDING).
      Wynik: VERIFIED / REJECTED / PENDING z dowodami. Bez zgadywania: tylko VERIFIED jest uzywane do wyceny.
      Powiazania juz ZWERYFIKOWANE w ostatnich RECHECK_DAYS dniach nie sa sprawdzane ponownie (przyspieszenie; zasada weryfikacji bez zmian).
  refresh_quotes(conn, provider) - notowania dla VERIFIED + kursy walut potrzebne do wyceny (tylko dopisywanie).
  refresh_nbp(conn, nbp) - tabela A NBP do fx_rates (kurs referencyjny i zapas).
Zapytania do dostawcy ida rownolegle (providers.fetch_all), zapis do bazy po kolei.
Polaczenie przekazuje wywolujacy (CLI: wlasciciel; aplikacja: rola aplikacji - tylko refresh_*).
"""
from decimal import Decimal

from psycopg2.extras import Json

from invapp.core.common import jsonable
from .providers import fetch_all

TOL = Decimal("1.30")          # dopuszczalny stosunek ceny dostawcy do ceny XTB (ruch rynku od chwili migawki); poza nim -> odrzucenie
RECHECK_DAYS = 7


def _open_listings(cur, broker_code="XTB", provider_code="YAHOO", recheck_days=RECHECK_DAYS):
    cur.execute("""select l.listing_id, la.broker_symbol, l.trading_currency, l.currency_status,
                          (select s.current_price from broker_position_snapshots s where s.listing_id = l.listing_id and s.row_level = 'POSITION' and s.current_price is not null
                            order by s.as_of desc limit 1) as broker_price
                   from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   where exists (select 1 from derived_lots dl where dl.listing_id = l.listing_id and dl.qty_remaining > 0)
                     and la.broker_id = (select broker_id from financial_institutions where code = %s)      -- symbole z sufiksem rynku (XTB); notowania po ISIN (Trade Republic) rozwiazuje brokers/trade_republic/pricing.py
                     and not exists (select 1 from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = %s and m.status = 'VERIFIED'
                                       and m.quote_currency = l.trading_currency and m.checked_at > now() - make_interval(days => %s))
                   order by la.broker_symbol""", (broker_code, provider_code, recheck_days))
    return cur.fetchall()


def verify_mappings(conn, provider, pause=0.3, broker_code="XTB", recheck_days=RECHECK_DAYS):
    cur = conn.cursor(); out = []
    rows = _open_listings(cur, broker_code, provider.code, recheck_days)
    syms = {r[0]: provider.symbol_for(r[1]) for r in rows}
    fetched = fetch_all(provider.quote, [syms[r[0]] for r in rows if syms[r[0]] and r[2] is not None])
    for listing_id, broker_symbol, ccy, ccy_status, broker_price in rows:
        sym = syms[listing_id]
        ev = {"broker_symbol": broker_symbol, "listing_currency": ccy, "listing_currency_status": ccy_status, "broker_price": broker_price}
        status, q = None, None
        if not sym:
            status, ev["reason"] = "PENDING", "brak reguly symbolu u dostawcy dla tego rynku"
        elif ccy is None:
            status, ev["reason"] = "PENDING", "waluta notowania w bazie niezweryfikowana - nie da sie sprawdzic zgodnosci"
        else:
            q, err = fetched[sym]
            if err is not None:
                status, ev["reason"], q = "PENDING", f"blad pobrania: {type(err).__name__}", None
            else:
                ev.update({"provider_price": q["price"], "provider_currency": q["raw_currency"], "provider_exchange": q["exchange"], "quoted_at": q["quoted_at"]})
                if q["currency"] != ccy:
                    status, ev["reason"] = "REJECTED", f"waluta dostawcy {q['raw_currency']} != waluta notowania {ccy}"
                elif broker_price is None:
                    status, ev["reason"] = "PENDING", "brak ceny z migawki XTB do porownania"
                else:
                    pv = q["price"] / (100 if q["price_unit"] == "MINOR_1_100" else 1)
                    ratio = pv / Decimal(broker_price)
                    ev["price_ratio"] = ratio.quantize(Decimal("0.0001"))
                    if Decimal(1) / TOL <= ratio <= TOL:
                        status = "VERIFIED"
                    else:
                        status, ev["reason"] = "REJECTED", f"cena dostawcy odbiega od ceny XTB (stosunek {ratio:.4f})"
        cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,reference_exchange,checked_at,evidence)
                       values (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       on conflict (listing_id, provider) do update set provider_symbol=excluded.provider_symbol, status=excluded.status, quote_currency=excluded.quote_currency,
                         price_unit=excluded.price_unit, reference_exchange=excluded.reference_exchange, checked_at=excluded.checked_at, evidence=excluded.evidence""",
                    (listing_id, provider.code, sym or "", status, q["currency"] if (q and status == "VERIFIED") else None,
                     q["price_unit"] if (q and status == "VERIFIED") else None, q["exchange"] if q else None, Json(jsonable(ev))))
        if q and status == "VERIFIED":
            _insert_quote(cur, listing_id, provider.code, sym, q)
        out.append({"symbol": broker_symbol, "provider_symbol": sym, "status": status, "reason": ev.get("reason"), "ratio": ev.get("price_ratio")})
    return out


def _insert_quote(cur, listing_id, provider_code, sym, q):
    cur.execute("""insert into market_quotes(listing_id,provider,provider_symbol,price,currency,price_unit,quoted_at,market_state)
                   values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (listing_id, provider, quoted_at) do nothing""",
                (listing_id, provider_code, sym, q["price"], q["currency"], q["price_unit"], q["quoted_at"], q["market_state"]))
    return cur.rowcount


def refresh_quotes(conn, provider, pause=0.3):
    """Pobiera najnowsze notowania dla zweryfikowanych powiazan otwartych pozycji oraz kursy walut tych notowan do PLN."""
    cur = conn.cursor()
    cur.execute("""select m.listing_id, m.provider_symbol from listing_provider_symbols m
                   where m.provider = %s and m.status = 'VERIFIED'
                     and exists (select 1 from derived_lots dl where dl.listing_id = m.listing_id and dl.qty_remaining > 0)
                   order by m.provider_symbol""", (provider.code,))
    rows = cur.fetchall()
    res = {"quotes_new": 0, "quotes_same": 0, "errors": [], "fx": {}}
    fetched = fetch_all(provider.quote, [sym for _, sym in rows])
    currencies = set()
    for listing_id, sym in rows:
        q, err = fetched[sym]
        if err is not None:
            res["errors"].append(f"{sym}: {type(err).__name__}"); continue
        currencies.add(q["currency"])
        if _insert_quote(cur, listing_id, provider.code, sym, q):
            res["quotes_new"] += 1
        else:
            res["quotes_same"] += 1
    fx = fetch_all(lambda c: provider.fx(c, "PLN"), sorted(currencies - {"PLN"}))
    for ccy, (f, err) in sorted(fx.items()):
        if err is not None:
            res["errors"].append(f"{ccy}PLN: {type(err).__name__}"); continue
        cur.execute("""insert into fx_quotes(base,quote,rate,provider,quoted_at) values (%s,'PLN',%s,%s,%s) on conflict do nothing""",
                    (ccy, f["rate"], provider.code, f["quoted_at"]))
        res["fx"][ccy] = str(f["rate"])
    return res


def refresh_nbp(conn, nbp, currencies=("USD", "EUR", "GBP", "CHF")):
    t = nbp.table_a(); cur = conn.cursor(); added = 0
    for c in currencies:
        if c in t["rates"]:
            cur.execute("insert into fx_rates(rate_date,base,quote,rate,source) values (%s,%s,'PLN',%s,'NBP_A') on conflict do nothing", (t["date"], c, t["rates"][c]))
            added += cur.rowcount
    return {"table": t["table"], "date": t["date"], "added": added}
