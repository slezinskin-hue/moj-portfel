"""
Dane dla strony glownej (przez role aplikacji investing_app, tylko odczyt) - osobno dla zakresow: XTB, Trade Republic i WSZYSTKIE (laczny, te same zasady).
Zasada wartosci: wartosc portfela = SUMA (ilosc x aktualna cena z API x kurs) papierow wartosciowych; wolne srodki NIE sa jej czescia i nie sa pokazywane.
Dywidendy, odsetki, nagrody i oplaty oraz zrealizowany zysk sa osobnymi pozycjami; razem z niezrealizowanym tworza zysk calkowity.
Zakladki tworza sie z listy brokerow (invapp.brokers.BROKERS) + zakladka WSZYSTKIE (zakres INVESTMENT_ALL).
"""
import threading
import time as _time
from datetime import date

import psycopg2
import psycopg2.extras

from invapp import brokers, config
from invapp.db import engine


def _scopes():
    out = {b.key: (b.scope_code, b.name) for b in brokers.BROKERS}
    out["all"] = ("INVESTMENT_ALL", "WSZYSTKIE")
    return out


def _brokers_of(key):
    return tuple(b.code for b in brokers.BROKERS) if key == "all" else tuple(b.code for b in brokers.BROKERS if b.key == key)


SCOPES = _scopes()


def pln(value, decimals=2, suffix=" zł"):
    if value is None:
        return "—"
    s = f"{float(value):,.{decimals}f}".replace(",", " ").replace(".", ",")
    return s + suffix


def qty(value):
    if value is None:
        return "—"
    s = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def scope_code(key):
    return SCOPES.get(key, SCOPES["all"])[0]


def normalize_scope(key):
    return key if key in SCOPES else "all"


def _connect(readonly=False):
    return engine.connect("app", readonly=readonly)


def load(scope_key="all"):
    scope_key = normalize_scope(scope_key)
    code, title = SCOPES[scope_key]
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        def q(sql, args=None):
            cur.execute(sql, args); return [dict(r) for r in cur.fetchall()]

        data = {"data_dir": str(config.data_dir()), "dev": config.is_dev_data(), "scope_key": scope_key, "scope_code": code, "scope_title": title,
                "brokers": [{"key": b.key, "name": b.name, "help": b.file_help} for b in brokers.BROKERS],
                "accept": ",".join(sorted({e for b in brokers.BROKERS for e in b.accept}))}
        data["batches"] = q("""select b.batch_id, b.source_file_name, b.status, b.rows_read, b.rows_inserted, b.rows_duplicate, b.rows_conflict, b.rows_unmapped, b.rows_ignored, b.started_at,
                                      f.code as broker
                               from import_batches b join financial_institutions f using (broker_id) where f.code = any(%s) order by b.batch_id""", (list(_brokers_of(scope_key)),))
        data["empty"] = not q("select 1 from import_batches limit 1")
        data["all_batches"] = q("""select b.batch_id, b.source_file_name, b.rows_inserted, b.rows_duplicate, b.started_at, f.name as broker,
                                          b.account_id, coalesce(a.display_name, f.name) as account
                                   from import_batches b join financial_institutions f using (broker_id) left join accounts a on a.account_id = b.account_id
                                   where b.status = 'COMMITTED' order by coalesce(a.display_name, f.name), b.batch_id desc""")
        summ = {r["scope_code"]: r for r in q("select scope_code, has_data, positions from v_scope_summary")}
        data["tabs"] = [{"key": k, "title": t, "has_data": bool(summ.get(c, {}).get("has_data"))} for k, (c, t) in SCOPES.items()]
        data["has_data"] = bool(summ.get(code, {}).get("has_data"))
        sc_accounts = "(select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s)"
        data["accounts"] = q(f"""select a.display_name, a.product_type, a.tracking_mode, f.code as broker,
                                        (select count(*) from transactions t where t.account_id = a.account_id and t.status = 'ACTIVE') as txn_count,
                                        (select count(distinct dl.listing_id) from derived_lots dl where dl.account_id = a.account_id and dl.qty_remaining > 0) as open_positions,
                                        exists (select 1 from import_batches b where b.account_id = a.account_id) as has_batch
                                 from accounts a join financial_institutions f using (broker_id) where a.account_id in {sc_accounts} order by a.account_id""", (code,))
        data["income_by_year"] = q(f"""select extract(year from t.occurred_at)::int as year,
                    sum(t.cash_amount) filter (where t.txn_type = 'DIVIDEND') as dividends_gross,
                    sum(t.cash_amount) filter (where t.txn_type = 'TAX_WITHHOLDING') as withholding_tax,
                    sum(t.cash_amount) filter (where t.txn_type in ('DIVIDEND','TAX_WITHHOLDING')) as dividends_net,
                    sum(t.cash_amount) filter (where t.txn_type in ('INTEREST','TAX_INTEREST')) as interest_net,
                    sum(t.cash_amount) filter (where t.txn_type in ('FEE','REWARD')) as other
                 from transactions t where t.status = 'ACTIVE' and t.account_id in {sc_accounts}
                   and t.txn_type in ('DIVIDEND','TAX_WITHHOLDING','INTEREST','TAX_INTEREST','FEE','REWARD') group by 1 order by 1 desc""", (code,))
        alias = "(select min(la.broker_symbol) from listing_aliases la where la.listing_id = l.listing_id and la.valid_to is null)"
        data["dividends_by_instrument"] = q(f"""select {alias} as ticker, l.display_name as instrument,
                    sum(t.cash_amount) filter (where t.txn_type = 'DIVIDEND') as gross, sum(t.cash_amount) filter (where t.txn_type = 'TAX_WITHHOLDING') as tax,
                    sum(t.cash_amount) as net, count(*) filter (where t.txn_type = 'DIVIDEND' and (t.details->>'is_correction') is distinct from 'true') as payments,
                    max(t.occurred_at) filter (where t.txn_type = 'DIVIDEND') as last_payment
                 from transactions t join listings l using (listing_id)
                 where t.status = 'ACTIVE' and t.txn_type in ('DIVIDEND','TAX_WITHHOLDING') and t.account_id in {sc_accounts}
                 group by l.listing_id, 1, 2 order by net desc""", (code,))
        data["recent_dividends"] = q(f"""select t.occurred_at, a.display_name as account, {alias} as ticker, l.display_name as instrument, t.cash_amount as gross,
                    coalesce(w.cash_amount, 0) as tax, t.cash_amount + coalesce(w.cash_amount, 0) as net, t.details->>'dividend_per_share' as dps, t.details->>'dividend_currency' as ccy
                 from transactions t join accounts a using (account_id) join listings l using (listing_id)
                 left join transactions w on w.related_txn_id = t.txn_id and w.txn_type = 'TAX_WITHHOLDING'
                 where t.txn_type = 'DIVIDEND' and t.status = 'ACTIVE' and t.account_id in {sc_accounts} order by t.occurred_at desc limit 20""", (code,))
        if "XTB" in _brokers_of(scope_key):
            data["adjustments"] = q("select * from v_active_adjustments order by action_id")
            data["cost_recon"] = q("select t.*, l.display_name from v_cost_reconciliation_totals t join listings l using (listing_id)")
            data["issues"] = q("select code, severity, message from data_quality_issues order by issue_id")
        else:
            data["adjustments"], data["cost_recon"], data["issues"] = [], [], []
        data["instruments"] = q(f"""select {alias} as ticker, l.display_name as instrument, l.trading_currency as currency, l.currency_status, s.isin, s.isin_status, l.mic_status, l.price_unit_status,
                                           l.currency_source
                                    from listings l join securities s using (security_id)
                                    where exists (select 1 from transactions t where t.listing_id = l.listing_id and t.account_id in {sc_accounts})
                                    order by (l.currency_status = 'NIEZNANY') desc, 1""", (code,))
        return data
    finally:
        conn.close()


# ---------------------------------------------------------------- ceny na zywo, historia, wskazniki, kontrola
_cache = {"live_at": 0.0, "hist": {}}
_lock = threading.Lock()
LIVE_MIN_INTERVAL = 45      # sekundy: nie odpytujemy dostawcy czesciej (limit zapytan, uprzejmosc wobec uslugi)
HIST_TTL = 120


def refresh_live_if_stale(force=False):
    """Odswieza notowania i kursy (rola aplikacji: tylko dopisywanie danych referencyjnych), nie czesciej niz co LIVE_MIN_INTERVAL s."""
    from invapp.pricing import service
    from invapp.pricing.providers import YahooProvider
    with _lock:
        if not force and _time.time() - _cache["live_at"] < LIVE_MIN_INTERVAL:
            return {"skipped": True}
        conn = _connect()
        try:
            res = service.refresh_quotes(conn, YahooProvider(), pause=0.05)
            conn.commit()
        finally:
            conn.close()
        _cache["live_at"] = _time.time()
        return res


def live_data(scope_key="all"):
    """Podsumowanie zakresu (bez wolnych srodkow) i pozycje: ilosc, cena z API w walucie notowania, kurs, wartosc w walucie notowania i w PLN."""
    code = scope_code(normalize_scope(scope_key))
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("select * from v_scope_summary where scope_code = %s", (code,))
        row = cur.fetchone()
        cur.execute("""select p.listing_id, a.display_name as account, a.product_type, f.code as broker, sum(p.value_used) as value, sum(p.qty) as qty
                       from v_live_position_values p join accounts a using (account_id) join financial_institutions f on f.broker_id = a.broker_id
                       where p.account_id in (select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s)
                       group by 1, 2, 3, 4""", (code,))
        by_acc = {}
        for r in cur.fetchall():
            by_acc.setdefault(r["listing_id"], []).append({k: r[k] for k in ("account", "product_type", "broker", "value", "qty")})
        cur.execute("""select l.listing_id, (select min(la.broker_symbol) from listing_aliases la where la.listing_id = l.listing_id and la.valid_to is null) as ticker, l.display_name as instrument,
                              sum(p.qty) as qty, sum(p.value_used) as value, max(p.price_source) as price_source, max(p.price_used) as price, max(p.quote_currency) as currency,
                              max(p.price_unit) as price_unit, max(p.price_time_used) as price_time, max(p.fx_used) as fx_used, max(p.fx_source) as fx_source, max(p.fx_as_of) as fx_as_of, max(p.market_state) as market_state, sum(p.cost_left) as cost_left,
                              bool_or(p.value_source = 'MISSING') as missing
                       from v_live_position_values p join listings l using (listing_id)
                       where p.account_id in (select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s)
                       group by l.listing_id, l.display_name order by sum(p.value_used) desc nulls last""", (code,))
        positions = []
        for r in cur.fetchall():
            r = dict(r)
            r["price_pln"] = None
            if r["price_source"] == "MARKET" and r["price"] is not None and r["qty"] is not None:
                per_unit = r["price"] / (100 if r["price_unit"] == "MINOR_1_100" else 1)          # cena w walucie notowania (pensy -> funty)
                r["value_native"] = r["qty"] * per_unit
                fx = 1 if r["currency"] == "PLN" else r["fx_used"]                                 # kurs z API (Yahoo, a gdy brak - NBP); PLN = 1
                r["price_pln"] = per_unit * fx if fx is not None else None
            elif r["price_source"] == "XTB_EXPORT" and r["value"] is not None and r["qty"]:
                r["value_native"] = None
                r["price_pln"] = r["value"] / r["qty"]                                          # ustawienie "ceny z eksportu XTB": cena w PLN wg XTB
            else:
                r["value_native"] = None
            r["accounts"] = by_acc.get(r["listing_id"], [])
            positions.append(r)
        scope = dict(row) if row else None
        deps = deposits_of(conn, code)
        if scope is not None:
            add_deposits(scope, deps)
            add_cash(scope, conn, normalize_scope(scope_key), code)
        return {"scope": scope, "positions": positions, "deposits": deps["items"]}
    finally:
        conn.close()


def add_cash(scope, conn, scope_key, code):
    """Wartosc CALEGO portfela (kafelek) = aktywa inwestycyjne + gotowka z kont bankowych i wolne srodki XTB - tylko gdy wlaczone w ustawieniach
    (core/cash.py; domyslnie wylaczone). assets_value (inwestycje) zostaje bez zmian - z niego licza sie wykres i wskazniki."""
    from invapp.core import cash
    try:
        ex = cash.extra_value(conn.cursor(), scope_key, code)
    except psycopg2.Error:              # baza sprzed migracji 0017
        conn.rollback(); ex = {"gotowka": 0, "wolne_xtb": 0, "ustawienia": dict(cash.DEFAULTS)}
    scope["cash_value"], scope["xtb_free_value"] = ex["gotowka"], ex["wolne_xtb"]
    scope["total_value"] = scope["assets_value"] + ex["gotowka"] + ex["wolne_xtb"]
    scope["settings"] = ex["ustawienia"]


def exposure_data(scope_key="all"):
    """Zakladka Ekspozycja: pozycje (wartosc jak na stronie glownej - v_live_position_values) z waluta notowania, typem, sektorem i gielda.
    Profil (sektor, gielda wg Yahoo) - migracja 0019; baza bez profili -> pola puste."""
    code = scope_code(normalize_scope(scope_key))
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        prof = "listing_profile" if _has_table(cur, "listing_profile") else None
        cur.execute(f"""select l.listing_id, (select min(la.broker_symbol) from listing_aliases la where la.listing_id = l.listing_id and la.valid_to is null) as ticker,
                               l.display_name as instrument, coalesce(l.trading_currency, max(p.quote_currency)) as currency, s.asset_class,
                               sum(p.value_used) as value,
                               (select m.reference_exchange from listing_provider_symbols m where m.listing_id = l.listing_id and m.provider = 'YAHOO' and m.status = 'VERIFIED') as ref_exchange
                               {", max(pf.quote_type) as quote_type, max(pf.sector) as sector, max(pf.industry) as industry, max(pf.exchange) as exchange" if prof else ""}
                        from v_live_position_values p join listings l using (listing_id) join securities s using (security_id)
                        {"left join listing_profile pf on pf.listing_id = l.listing_id" if prof else ""}
                        where p.account_id in (select sa.account_id from scope_accounts sa join scopes sc using (scope_id) where sc.code = %s)
                        group by l.listing_id, l.display_name, l.trading_currency, s.asset_class having sum(p.value_used) is not null
                        order by sum(p.value_used) desc""", (code,))
        return {"pozycje": [dict(r) for r in cur.fetchall()]}
    finally:
        conn.close()


def _has_table(cur, name):
    cur.execute("select to_regclass(%s) is not null as ok", (name,))
    r = cur.fetchone()
    return bool(r["ok"] if isinstance(r, dict) else r[0])


def cash_data(scope_key="all"):
    from invapp.core import cash
    scope_key = normalize_scope(scope_key)
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor()
        konta = cash.accounts(cur)
        wolne = cash.broker_free_funds(cur, scope_code("all"))
        return {"ustawienia": cash.get_settings(cur), "konta": konta, "waluty": list(cash.CURRENCIES),
                "suma_kont": sum((a["wartosc_pln"] or 0 for a in konta), 0), "wolne_xtb": wolne, "suma_wolnych": sum((w["saldo"] for w in wolne), 0),
                "lokaty": deposits_of(conn, scope_code("all"))["items"]}
    finally:
        conn.close()


def deposits_of(conn, code):
    from invapp.core import deposits
    try:
        return deposits.scope_deposits(conn.cursor(), code)
    except psycopg2.Error:              # baza sprzed migracji 0015 - bez lokat
        conn.rollback()
        return {"items": [], "value": 0, "unbooked_interest_net": 0}


def add_deposits(scope, deps):
    """Wartosc AKTYWOW = papiery (ilosc x cena z API x kurs) + lokaty (saldo gotowki z ksiegi; core/deposits.py).
    Tylko kwoty zaksiegowane: odsetki naliczone, a jeszcze niewyplacone, NIE wchodza ani do wartosci, ani do zysku (decyzja uzytkownika 2026-11-12);
    wyplacone odsetki sa w dochodach dodatkowych."""
    scope["deposits_value"] = deps["value"]
    scope["assets_value"] = scope["securities_value"] + deps["value"]
    scope["deposit_interest_unbooked"] = deps["unbooked_interest_net"]                 # tylko informacyjnie
    scope["total_result_live"] = scope["total_result"]


def history_data(scope_key="all"):
    from invapp.pricing import history
    code = scope_code(normalize_scope(scope_key))
    with _lock:
        hit = _cache["hist"].get(code)
        if hit and _time.time() - hit[0] < HIST_TTL:
            return hit[1]
    from invapp.pricing import benchmarks
    conn = _connect(readonly=True)
    try:
        s = history.build_series(conn, code)
        try:                                                    # indeksy do porownania (wykres "Zwrot %"), w PLN, na dni serii
            s["porownanie"] = benchmarks.levels(conn, [date.fromisoformat(p["date"]) for p in s["series"]])
        except psycopg2.Error:
            conn.rollback(); s["porownanie"] = None
    finally:
        conn.close()
    with _lock:
        _cache["hist"][code] = (_time.time(), s)
    return s


def invalidate_history():
    with _lock:
        _cache["hist"] = {}


def invalidate_caches():
    with _lock:
        _cache["hist"], _cache["live_at"] = {}, 0.0


def metrics_data(scope_key="all"):
    """Wskazniki inwestora (pricing/metrics.py): seria dzienna papierow z ostatnim punktem = wartosc NA ZYWO (ceny z API), dywidendy netto jako dochod."""
    from invapp.pricing import metrics
    scope_key = normalize_scope(scope_key); code = scope_code(scope_key)
    s = history_data(scope_key)
    series = [dict(p) for p in s["series"]]
    live = live_data(scope_key)
    cost_left = None
    if series and live.get("scope"):
        series[-1]["value"] = live["scope"]["assets_value"]                  # papiery na zywo + lokaty
        cost_left = float(live["scope"]["securities_cost"] or 0)
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor()
        cur.execute("""select (t.occurred_at at time zone 'UTC')::date, sum(t.cash_amount) from transactions t
                       where t.status = 'ACTIVE' and t.txn_type in ('DIVIDEND', 'TAX_WITHHOLDING')
                         and t.account_id in (select sa.account_id from scope_accounts sa join scopes sc using (scope_id) where sc.code = %s)
                       group by 1 order by 1""", (code,))
        income = [(d, float(a)) for d, a in cur.fetchall()]
    finally:
        conn.close()
    out = metrics.compute(series, income=income, cost_left=cost_left)
    out["estimated_note"] = s.get("estimated_days", 0)
    return out


def income_monthly(scope_key="all"):
    """Dochody dodatkowe miesiecznie (kwoty z ksiegi, PLN, po podatkach pobranych przez brokera) - do wykresu slupkowego."""
    code = scope_code(normalize_scope(scope_key))
    conn = _connect(readonly=True)
    try:
        cur = conn.cursor()
        cur.execute("""select to_char(t.occurred_at at time zone 'UTC', 'YYYY-MM') as m,
                              coalesce(sum(t.cash_amount) filter (where t.txn_type in ('DIVIDEND','TAX_WITHHOLDING')), 0),
                              coalesce(sum(t.cash_amount) filter (where t.txn_type in ('INTEREST','TAX_INTEREST')), 0),
                              coalesce(sum(t.cash_amount) filter (where t.txn_type = 'REWARD'), 0),
                              coalesce(sum(t.cash_amount) filter (where t.txn_type = 'FEE'), 0)
                       from transactions t where t.status = 'ACTIVE' and t.cash_currency = 'PLN'
                         and t.txn_type in ('DIVIDEND','TAX_WITHHOLDING','INTEREST','TAX_INTEREST','REWARD','FEE')
                         and t.account_id in (select sa.account_id from scope_accounts sa join scopes s using (scope_id) where s.code = %s)
                       group by 1 order by 1""", (code,))
        return {"months": [{"month": m, "dividends": a, "interest": b, "rewards": c, "fees": e} for m, a, b, c, e in cur.fetchall()]}
    finally:
        conn.close()


def audit_data(scope_key="all"):
    from invapp.pricing import audit, diagnose
    from invapp.web import importing
    scope_key = normalize_scope(scope_key)
    conn = _connect(readonly=True)
    try:
        res = audit.run(conn, scope_code(scope_key), _brokers_of(scope_key))
        if not res.get("ok", True):                         # uwagi: przyczyna i proponowane rozwiazanie (pytania brokerow czyta wlasciciel bazy, jak /api/pytania)
            try:
                oc = importing.owner_conn()
                try:
                    questions = importing.pending_questions(oc)
                finally:
                    oc.close()
                diagnose.annotate(conn.cursor(), res, questions, scope_code(scope_key))
            except Exception:  # noqa: BLE001  - diagnoza nie moze zablokowac samej kontroli
                conn.rollback()
        return res
    finally:
        conn.close()
