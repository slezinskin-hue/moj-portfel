"""
Notowania Trade Republic: ISIN -> symbol dostawcy, z WERYFIKACJA ceną (bez zgadywania).
Trade Republic podaje ISIN, ale nie giełdę ani walutę notowania. Dla papierow z transakcjami TR bez zweryfikowanego powiazania:
  1. wyszukanie kandydatow po ISIN u dostawcy (Yahoo search; wynik nie jest dowodem),
  2. dla kazdego kandydata: notowanie (cena, WALUTA, gielda) -> cena w PLN (cena x kurs dostawcy waluty do PLN, pensy /100),
  3. porownanie z ostatnia cena transakcji TR w PLN (TR podaje ceny w PLN): iloraz w [1/TOL, TOL] -> kandydat zgodny; wybierany jest najblizszy 1;
  4. VERIFIED: zapis powiazania (z dowodami) dla notowan na zywo i historii; waluta notowania z danych dostawcy zapisana z jawnym statusem
     ZGODNY_ZRODLO_ZEWN, zrodlem i data (tylko gdy dotad nieznana - istniejacej wartosci nigdy nie nadpisujemy).
Brak kandydata zgodnego cenowo = PENDING/REJECTED z powodem; pozycja bez ceny rynkowej jest wtedy jawnie oznaczona (nie liczymy jej z zakupu).
"""
import time
from decimal import Decimal

from psycopg2.extras import Json

from invapp.core.common import jsonable
from invapp.pricing.service import TOL, _insert_quote

HIST_PROVIDER = "YAHOO_1D"


def _tr_listings(cur, broker_code="TR"):
    cur.execute("""select l.listing_id, la.broker_symbol,
                          (select t.price from transactions t where t.listing_id = l.listing_id and t.txn_type in ('BUY','SELL') and t.price is not null
                            order by t.occurred_at desc, t.txn_id desc limit 1) as last_price_pln,
                          l.trading_currency
                   from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   where la.broker_id = (select broker_id from financial_institutions where code = %s)
                     and exists (select 1 from derived_lots dl where dl.listing_id = l.listing_id and dl.qty_remaining > 0)
                     and not exists (select 1 from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = 'YAHOO' and m.status = 'VERIFIED')
                   order by la.broker_symbol""", (broker_code,))
    return cur.fetchall()


def resolve_tr_listings(conn, provider, pause=0.3, broker_code="TR"):
    """Rozwiazuje symbole dla OTWARTYCH pozycji brokera, ktory podaje ISIN i ceny w PLN (Trade Republic; takze nowi brokerzy z szablonu
    `python -m invapp.tools.nowy_broker`), bez zweryfikowanego powiazania. Zwraca liste wynikow."""
    cur = conn.cursor(); out = []
    for listing_id, isin, ref_price, known_ccy in _tr_listings(cur, broker_code):
        ev = {"isin": isin, "reference_price_pln": ref_price, "candidates": []}
        best, status, reason = None, "PENDING", None
        if ref_price is None:
            reason = "brak ceny z transakcji TR do porównania"
        else:
            try:
                cands = provider.search(isin)
            except Exception as e:  # noqa: BLE001
                cands, reason = [], f"błąd wyszukiwania: {type(e).__name__}"
            if not cands and not reason:
                reason = "dostawca nie zna tego ISIN"
            fx_cache = {}
            for c in cands:
                row = {"symbol": c["symbol"], "exchange": c.get("exchange")}
                try:
                    q = provider.quote(c["symbol"])
                    ccy = q["currency"]
                    if ccy not in fx_cache:
                        fx_cache[ccy] = Decimal(1) if ccy == "PLN" else provider.fx(ccy, "PLN")["rate"]
                    price = q["price"] / (100 if q["price_unit"] == "MINOR_1_100" else 1)
                    ratio = (price * fx_cache[ccy]) / Decimal(ref_price)
                    row.update({"currency": ccy, "price": price, "ratio": ratio.quantize(Decimal("0.0001"))})
                    if Decimal(1) / TOL <= ratio <= TOL and (best is None or abs(ratio - 1) < abs(best[1] - 1)):
                        best = (c, ratio, q, fx_cache[ccy])
                except Exception as e:  # noqa: BLE001
                    row["error"] = type(e).__name__
                ev["candidates"].append(row)
                time.sleep(pause)
            if best:
                status = "VERIFIED"
            elif cands:
                status, reason = "REJECTED", "żaden kandydat nie zgadza się cenowo z ostatnią transakcją TR (iloraz poza przedziałem)"
        sym = best[0]["symbol"] if best else ""
        if reason:
            ev["reason"] = reason
        if best:
            ev.update({"chosen": best[0]["symbol"], "price_ratio": best[1].quantize(Decimal("0.0001")), "provider_currency": best[2]["raw_currency"]})
        for prov in ("YAHOO", HIST_PROVIDER):
            cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,reference_exchange,checked_at,evidence)
                           values (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                           on conflict (listing_id, provider) do update set provider_symbol=excluded.provider_symbol, status=excluded.status, quote_currency=excluded.quote_currency,
                             price_unit=excluded.price_unit, reference_exchange=excluded.reference_exchange, checked_at=excluded.checked_at, evidence=excluded.evidence""",
                        (listing_id, prov, sym, status, best[2]["currency"] if best else None,
                         (best[2]["price_unit"] if prov == "YAHOO" else "MAJOR") if best else None, best[0].get("exchange") if best else None, Json(jsonable(ev))))
        if best:
            _insert_quote(cur, listing_id, "YAHOO", best[0]["symbol"], best[2])
            src = f"Yahoo Finance (symbol {best[0]['symbol']}, ISIN {isin}), {time.strftime('%Y-%m-%d')}; zgodność ceny w PLN z transakcją TR (iloraz {best[1].quantize(Decimal('0.01'))})"
            cur.execute("savepoint cur_upd")
            try:
                cur.execute("""update listings set trading_currency=%s, currency_status='ZGODNY_ZRODLO_ZEWN', currency_source=%s, currency_checked_at=now(), currency_evidence=%s
                               where listing_id=%s and trading_currency is null""", (best[2]["currency"], src, Json(jsonable(ev)), listing_id))
                cur.execute("release savepoint cur_upd")
            except Exception:  # noqa: BLE001  (np. ta sama linia handlowa istnieje juz jako notowanie innego brokera) - waluta zostaje w dowodach powiazania
                cur.execute("rollback to savepoint cur_upd")
        out.append({"isin": isin, "symbol": sym or None, "status": status, "reason": reason, "ratio": ev.get("price_ratio")})
    return out


def resolve_closed_history(conn, provider, start=None, broker_code="TR", recheck_days=7):
    """Symbol HISTORII dla ZAMKNIETYCH pozycji (decyzja uzytkownika 2026-11-21): bez niego wykres wycenial je w czasie posiadania
    ostatnia cena transakcji (oszacowanie). Dzisiejsza cena nie nadaje sie do weryfikacji (pozycja sprzedana dawno temu), wiec kandydat z
    wyszukiwania po ISIN jest sprawdzany na HISTORII: cena kazdej transakcji TR (PLN) / (zamkniecie dnia x kurs dnia do PLN) w [0,8; 1,25]
    dla >= 75% transakcji; splity dostawcy spoza ksiegi cofniete (jak w pricing/history). Zapis tylko powiazania historii (YAHOO_1D)."""
    from datetime import timedelta
    from invapp.pricing import history as h
    cur = conn.cursor(); out = []
    cur.execute("""select l.listing_id, la.broker_symbol from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   where la.broker_id = (select broker_id from financial_institutions where code = %s)
                     and exists (select 1 from transactions t where t.listing_id = l.listing_id and t.txn_type in ('BUY','SELL') and t.status = 'ACTIVE')
                     and not exists (select 1 from derived_lots dl where dl.listing_id = l.listing_id and dl.qty_remaining > 0)
                     and not exists (select 1 from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = %s
                                       and (m.status = 'VERIFIED' or m.checked_at > now() - make_interval(days => %s)))
                   order by la.broker_symbol""", (broker_code, h.HIST_PROVIDER, recheck_days))
    rows = cur.fetchall()
    fx_cache = {}
    for listing_id, isin in rows:
        cur.execute("""select (occurred_at at time zone 'UTC')::date, price from transactions
                       where listing_id = %s and txn_type in ('BUY','SELL') and status = 'ACTIVE' and price is not null order by occurred_at""", (listing_id,))
        trades = [(d, Decimal(p)) for d, p in cur.fetchall()]
        conf = h._confirmed_actions(cur, [listing_id]).get(listing_id, [])
        ev = {"isin": isin, "trades": len(trades), "candidates": []}
        best = None
        first = (start or (trades[0][0] if trades else None))
        try:
            cands = provider.search(isin) if trades else []
        except Exception as e:  # noqa: BLE001
            cands = []; ev["reason"] = f"błąd wyszukiwania: {type(e).__name__}"
        for c in cands:
            row = {"symbol": c["symbol"], "exchange": c.get("exchange")}
            try:
                hc = provider.daily(c["symbol"], first - timedelta(days=10))
                ccy = hc["currency"]
                if ccy != "PLN" and ccy not in fx_cache:
                    fx_cache[ccy] = provider.daily(f"{ccy}PLN=X", first - timedelta(days=10))["closes"]
                um = h._unmatched(hc.get("splits") or [], conf)
                ratios = []
                for d, price in trades:
                    close = h._close_on(hc["closes"], d)
                    rate = Decimal(1) if ccy == "PLN" else h._close_on(fx_cache[ccy], d)
                    if close and rate:
                        ratios.append(price / (close * h._factor(um, d) * rate))
                ok = [r for r in ratios if Decimal("0.8") <= r <= Decimal("1.25")]
                med = sorted(ratios)[len(ratios) // 2] if ratios else None
                row.update({"currency": ccy, "compared": len(ratios), "matching": len(ok), "median_ratio": med.quantize(Decimal("0.0001")) if med else None})
                if ratios and Decimal(len(ok)) / len(ratios) >= h.MATCH_SHARE and (best is None or abs(med - 1) < abs(best[2] - 1)):
                    best = (c, hc, med)
            except Exception as e:  # noqa: BLE001
                row["error"] = type(e).__name__
            ev["candidates"].append(row)
        status = "VERIFIED" if best else ("REJECTED" if cands else "PENDING")
        if not best and "reason" not in ev:
            ev["reason"] = ("żaden kandydat nie zgadza się z cenami transakcji w historii" if cands
                            else ("brak transakcji z ceną" if not trades else "dostawca nie zna tego ISIN"))
        sym = best[0]["symbol"] if best else ""
        if best:
            ev.update({"chosen": sym, "provider_currency": best[1]["raw_currency"]})
            h._store_splits(cur, listing_id, h.HIST_PROVIDER, sym, best[1].get("splits") or [])
        cur.execute("""insert into listing_provider_symbols(listing_id,provider,provider_symbol,status,quote_currency,price_unit,reference_exchange,checked_at,evidence)
                       values (%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       on conflict (listing_id, provider) do update set provider_symbol=excluded.provider_symbol, status=excluded.status, quote_currency=excluded.quote_currency,
                         price_unit=excluded.price_unit, reference_exchange=excluded.reference_exchange, checked_at=excluded.checked_at, evidence=excluded.evidence""",
                    (listing_id, h.HIST_PROVIDER, sym, status, best[1]["currency"] if best else None, "MAJOR" if best else None,
                     best[0].get("exchange") if best else None, Json(jsonable(ev))))
        if best:
            src = f"Yahoo Finance (symbol {sym}, ISIN {isin}), {time.strftime('%Y-%m-%d')}; zgodność cen transakcji TR w PLN z historią (mediana ilorazu {best[2].quantize(Decimal('0.01'))})"
            cur.execute("savepoint cur_upd")
            try:
                cur.execute("""update listings set trading_currency=%s, currency_status='ZGODNY_ZRODLO_ZEWN', currency_source=%s, currency_checked_at=now(), currency_evidence=%s
                               where listing_id=%s and trading_currency is null""", (best[1]["currency"], src, Json(jsonable(ev)), listing_id))
                cur.execute("release savepoint cur_upd")
            except Exception:  # noqa: BLE001  (ta sama linia handlowa istnieje juz jako inne notowanie) - waluta zostaje w dowodach powiazania
                cur.execute("rollback to savepoint cur_upd")
        out.append({"isin": isin, "symbol": sym or None, "status": status, "reason": ev.get("reason")})
    return out


resolve_isin_listings = resolve_tr_listings          # nazwa ogolna dla nowych brokerow (ISIN + ceny w PLN)
