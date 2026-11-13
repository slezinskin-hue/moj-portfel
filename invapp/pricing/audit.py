"""
Kontrola poprawnosci liczb (priorytet: wartosc portfela musi sie zgadzac z rzeczywistoscia).
Zrodlem prawdy o wartosci jest: ILOSC posiadanych papierow x AKTUALNA CENA Z API x KURS WALUTY. Kontrola liczy te wartosc NIEZALEZNIE (z tabel bazowych:
partie, notowania, kursy - bez uzycia widokow wyceny) i porownuje z liczbami pokazanymi w aplikacji, a takze sprawdza tozsamosci ksiegowe
(zysk = wartosc - (zakupy - sprzedaze); koszt = zakupy - koszt sprzedanych; wynik calkowity = zysk + dochody; laczny = suma brokerow) i ilosci z raportem XTB.
Wynik: lista kontroli [{kontrola, status OK|BLAD|OSTRZEZENIE, szczegoly}]. Tylko odczyt.
"""
from decimal import Decimal as D

TOL = D("0.01")


def _dec(x):
    return D(str(x)) if x is not None else D(0)


def _brokers():
    from invapp import brokers
    return brokers.BROKERS


def _names():
    return " + ".join(b.name for b in _brokers())


def _settings(cur):
    """Ustawienia wyceny (migracja 0017); baza bez tabeli ustawien -> domyslne (Yahoo, kurs na zywo)."""
    from invapp.core import cash
    cur.execute("select to_regclass('app_settings') is not null")
    return cash.get_settings(cur) if cur.fetchone()[0] else dict(cash.DEFAULTS)


def _fx(cur, ccy, source):
    """Kurs {ccy}/PLN: domyslnie najnowszy na zywo, a gdy brak - NBP; przy ustawieniu NBP odwrotnie."""
    live = "select rate from fx_quotes where base = %s and quote = 'PLN' order by quoted_at desc limit 1"
    nbp = "select rate from fx_rates where base = %s and quote = 'PLN' and source = 'NBP_A' order by rate_date desc limit 1"
    for q in ((nbp, live) if source == "NBP" else (live, nbp)):
        cur.execute(q, (ccy,))
        r = cur.fetchone()
        if r:
            return _dec(r[0])
    return None


def run(conn, scope_code, brokers=("XTB", "TR")):
    cur = conn.cursor(); checks = []

    def add(name, ok, detail="", level="BLAD"):
        checks.append({"kontrola": name, "status": "OK" if ok else level, "szczegoly": detail})

    cur.execute("select * from v_scope_summary where scope_code = %s", (scope_code,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row:
        return {"ok": True, "checks": [], "note": "Brak danych w tym zakresie."}
    s = dict(zip(cols, row))
    cur.execute("select sa.account_id from scope_accounts sa join scopes sc using (scope_id) where sc.code = %s", (scope_code,))
    accounts = [r[0] for r in cur.fetchall()]

    # 1. niezalezne przeliczenie wartosci kazdej pozycji: ilosc x cena z API x kurs (albo - przy ustawieniu "ceny z eksportu XTB" - wartosc z raportu XTB,
    #    gdy ilosc w raporcie = ilosc w ksiedze); kurs wg ustawienia (na zywo albo NBP)
    st = _settings(cur)
    cur.execute("""select dl.listing_id, sum(dl.qty_remaining), (select min(la.broker_symbol) from listing_aliases la where la.listing_id = dl.listing_id and la.valid_to is null)
                   from derived_lots dl where dl.account_id = any(%s) group by dl.listing_id having sum(dl.qty_remaining) > 0.00000001""", (accounts,))
    positions = cur.fetchall()
    expected_total, bad, missing, from_broker = D(0), [], [], 0
    for listing_id, qty, ticker in positions:
        cur.execute("""select q.price, q.currency, q.price_unit from market_quotes q join listing_provider_symbols m
                         on m.listing_id = q.listing_id and m.provider = q.provider and m.status = 'VERIFIED'
                       where q.listing_id = %s order by q.quoted_at desc limit 1""", (listing_id,))
        qt = cur.fetchone()
        cur.execute("select coalesce(sum(value_used), 0), bool_or(value_source = 'LIVE') from v_live_position_values where listing_id = %s and account_id = any(%s)", (listing_id, accounts))
        shown, is_live = cur.fetchone()
        exp, unresolved = D(0), False
        cur.execute("select account_id, sum(qty_remaining) from derived_lots where listing_id = %s and account_id = any(%s) group by 1 having sum(qty_remaining) > 0", (listing_id, accounts))
        for acc, aqty in cur.fetchall():
            if st["price_source"] == "XTB_EXPORT":
                cur.execute("""select s.value_cash, s.volume from broker_position_snapshots s
                               where s.account_id = %s and s.listing_id = %s and s.row_level = 'INSTRUMENT'
                                 and s.as_of = (select max(as_of) from broker_position_snapshots where account_id = %s and row_level = 'INSTRUMENT')""", (acc, listing_id, acc))
                sn = cur.fetchone()
                if sn and sn[1] is not None and abs(_dec(sn[1]) - _dec(aqty)) <= D("0.00000001"):
                    exp += _dec(sn[0]); from_broker += 1; continue
            if not qt:
                unresolved = True; continue
            price, ccy, unit = qt
            price = _dec(price) / (100 if unit == "MINOR_1_100" else 1)
            fx = D(1) if ccy == "PLN" else _fx(cur, ccy, st["fx_source"])
            if fx is None:
                unresolved = True; continue
            exp += _dec(aqty) * price * fx
        if unresolved:
            missing.append(ticker); continue
        expected_total += exp
        if abs(exp - _dec(shown)) > TOL:
            bad.append(f"{ticker}: ilość {qty}, przeliczone {exp.quantize(TOL)}, aplikacja pokazuje {_dec(shown).quantize(TOL)}")
    src = "wartość z ostatniego eksportu XTB" if st["price_source"] == "XTB_EXPORT" else "ilość × cena z API × kurs"
    add(f"Wartość każdej pozycji = {src} (przeliczone niezależnie z tabel bazowych)", not bad,
        "; ".join(bad) if bad else f"{len(positions) - len(missing)} pozycji zgodnych co do 0,01 zł" + (f" (z eksportu XTB: {from_broker})" if from_broker else ""))
    add("Wszystkie pozycje mają cenę rynkową z API", not missing, ("brak ceny: " + ", ".join(missing)) if missing else "", "OSTRZEZENIE")
    shown_total = _dec(s["securities_value"])
    add("Wartość portfela (suma pozycji) = suma niezależnie przeliczonych wartości", abs(shown_total - expected_total) <= TOL if not missing else True,
        f"pokazane {shown_total.quantize(TOL)}, przeliczone {expected_total.quantize(TOL)}" + (" (bez pozycji bez ceny rynkowej)" if missing else ""))

    # 2. tozsamosci ksiegowe
    cur.execute("""select coalesce(sum(dl.cost_cash_initial), 0) from derived_lots dl where dl.account_id = any(%s)""", (accounts,))
    purchases = _dec(cur.fetchone()[0])
    cur.execute("""select coalesce(sum(c.proceeds_cash), 0), coalesce(sum(c.cost_cash), 0), coalesce(sum(c.realized_pnl), 0)
                   from derived_lot_closures c join derived_lots dl using (lot_id) where dl.account_id = any(%s)""", (accounts,))
    sales, cost_sold, realized = (_dec(x) for x in cur.fetchone())
    cur.execute("""select coalesce(sum(cost_cash_initial), 0) from derived_lots dl where dl.account_id = any(%s) and dl.qty_remaining > 0.00000001""", (accounts,))
    gain = shown_total - (purchases - sales)
    add("Zysk z inwestycji = wartość papierów − (zakupy − sprzedaże)", abs(gain - _dec(s["investment_gain"])) <= TOL,
        f"{gain.quantize(TOL)} vs {_dec(s['investment_gain']).quantize(TOL)} (zakupy {purchases.quantize(TOL)}, sprzedaże {sales.quantize(TOL)})")
    add("Koszt otwartych pozycji = zakupy − koszt sprzedanych części", abs((purchases - cost_sold) - _dec(s["securities_cost"])) <= TOL,
        f"{(purchases - cost_sold).quantize(TOL)} vs {_dec(s['securities_cost']).quantize(TOL)}")
    add("Zrealizowany zysk = przychód ze sprzedaży − koszt sprzedanych części", abs((sales - cost_sold) - realized) <= TOL and abs(realized - _dec(s["realized_gain"])) <= TOL,
        f"{(sales - cost_sold).quantize(TOL)} vs {realized.quantize(TOL)}")
    add("Zysk z inwestycji = niezrealizowany + zrealizowany", abs(_dec(s["unrealized_gain"]) + _dec(s["realized_gain"]) - _dec(s["investment_gain"])) <= TOL)
    add("Zysk całkowity = zysk z inwestycji + dywidendy, odsetki, nagrody i opłaty",
        abs(_dec(s["investment_gain"]) + _dec(s["additional_income"]) - _dec(s["total_result"])) <= TOL,
        f"{_dec(s['investment_gain']).quantize(TOL)} + {_dec(s['additional_income']).quantize(TOL)} = {_dec(s['total_result']).quantize(TOL)}")

    # 3. zakres laczny = suma brokerow (wszystkich z rejestru invapp/brokers)
    if scope_code == "INVESTMENT_ALL":
        cur.execute("select * from v_scope_summary where scope_code = any(%s)", ([b.scope_code for b in _brokers()],))
        cols2 = [d[0] for d in cur.description]
        parts = [dict(zip(cols2, r)) for r in cur.fetchall()]
        for k, label in (("securities_value", "wartość papierów"), ("investment_gain", "zysk z inwestycji"), ("additional_income", "dochody dodatkowe"), ("total_result", "zysk całkowity")):
            total = sum((_dec(p[k]) for p in parts), D(0))
            add(f"WSZYSTKIE = {_names()}: {label}", abs(total - _dec(s[k])) <= TOL, f"{total.quantize(TOL)} vs {_dec(s[k]).quantize(TOL)}")

    # 3a. lokaty (gotowka oprocentowana; core/deposits.py)
    deps = _deposit_checks(cur, scope_code, accounts, add)

    # 4. ilosci wzgledem raportu brokera (stan na dzien eksportu) - dla kont, ktorych eksport zawiera stan pozycji (np. XTB)
    cur.execute("select exists (select 1 from broker_position_snapshots where account_id = any(%s))", (accounts,))
    if cur.fetchone()[0]:
        cur.execute("""select a.display_name, l.display_name, coalesce(dl.qty, 0), sn.vol, sn.as_of
                       from (select s.account_id, s.listing_id, s.volume vol, s.as_of from broker_position_snapshots s join
                               (select account_id, max(as_of) mx from broker_position_snapshots where row_level = 'INSTRUMENT' group by 1) m on m.account_id = s.account_id and m.mx = s.as_of
                             where s.row_level = 'INSTRUMENT' and s.account_id = any(%s)) sn
                       join accounts a on a.account_id = sn.account_id join listings l on l.listing_id = sn.listing_id
                       left join (select account_id, listing_id, sum(qty_remaining) qty from derived_lots group by 1, 2) dl on dl.account_id = sn.account_id and dl.listing_id = sn.listing_id""", (accounts,))
        diffs = [f"{a} / {i}: księga {q}, raport brokera {v}" for a, i, q, v, _ in cur.fetchall() if abs(_dec(q) - _dec(v)) > D("0.0002")]
        add("Ilości w księdze = ilości w raporcie brokera (stan na dzień eksportu)", not diffs, "; ".join(diffs), "OSTRZEZENIE")
    # 5. ujemne stany (zakupy po potwierdzonych korektach - sprzedaze)
    cur.execute("""select count(*) from (select account_id, listing_id, sum(qty_initial) b from derived_lots where account_id = any(%s) group by 1, 2) x
                   left join (select account_id, listing_id, sum(quantity) s from transactions where txn_type = 'SELL' and status = 'ACTIVE' group by 1, 2) y using (account_id, listing_id)
                   where x.b - coalesce(y.s, 0) < -0.0000001""", (accounts,))
    neg = cur.fetchone()[0]
    add("Brak ujemnych stanów papierów (zakupy po korektach − sprzedaże)", neg == 0, f"{neg} pozycji z ujemnym stanem (możliwy split niewidoczny w eksporcie)" if neg else "", "OSTRZEZENIE")
    return {"ok": all(c["status"] == "OK" for c in checks), "checks": checks, "positions": len(positions), "recomputed_value": expected_total, "shown_value": shown_total,
            "deposits_value": deps}


INCOME_TYPES = ("DIVIDEND", "TAX_WITHHOLDING", "INTEREST", "TAX_INTEREST", "REWARD", "FEE")


def _deposit_checks(cur, scope_code, accounts, add):
    """Kontrole lokat: (1) saldo z ksiegi (widok SQL v_cash_balance) = saldo w modelu; (2) model odtwarza wyplacone odsetki;
    (3) tozsamosc konta: papiery + lokata (saldo z ksiegi) - wplaty netto z banku = zysk z inwestycji + dochody dodatkowe;
    (4) WSZYSTKIE: lokaty = suma lokat brokerow. Zwraca sume wartosci lokat w zakresie."""
    from invapp.core import deposits as dep
    from invapp.pricing.history import DEPOSIT_FLOWS
    try:
        sd = dep.scope_deposits(cur, scope_code)
    except Exception:  # noqa: BLE001  (baza sprzed migracji 0015)
        cur.connection.rollback()
        return D(0)
    for m in sd["items"]:
        acc, name = m["account_id"], m["account"]
        cur.execute("select coalesce(sum(balance), 0) from v_cash_balance where account_id = %s and cash_currency = 'PLN'", (acc,))
        view_bal = _dec(cur.fetchone()[0])
        add(f"Lokata „{name}”: saldo gotówki z księgi (widok SQL) = saldo w modelu lokaty", abs(view_bal - _dec(m["ledger_balance"])) <= TOL,
            f"{view_bal.quantize(TOL)} vs {_dec(m['ledger_balance']).quantize(TOL)} (stan na {m['ledger_as_of']})")
        rep = dep.reproduce_payouts(dep.account_txns(cur, acc), m["rate"], m["day_count"])
        okn = sum(1 for r in rep if abs(r["diff"]) <= D("0.05"))
        worst = max((abs(r["diff"]) for r in rep), default=D(0))
        add(f"Lokata „{name}”: model ({_dec(m['rate']) * 100:.2f}% rocznie od salda dziennego) odtwarza wypłacone odsetki", okn == len(rep),
            f"{okn} z {len(rep)} wypłat zgodnych (największa różnica {worst.quantize(TOL)} zł)" if rep else "brak wypłat do porównania", "OSTRZEZENIE")
        cur.execute("select coalesce(sum(value_used), 0) from v_live_position_values where account_id = %s", (acc,))
        sec = _dec(cur.fetchone()[0])
        cur.execute("select coalesce(sum(cost_cash_initial), 0) from derived_lots where account_id = %s", (acc,))
        buys = _dec(cur.fetchone()[0])
        cur.execute("select coalesce(sum(c.proceeds_cash), 0) from derived_lot_closures c join derived_lots dl using (lot_id) where dl.account_id = %s", (acc,))
        sells = _dec(cur.fetchone()[0])
        cur.execute("""select coalesce(sum(cash_amount) filter (where txn_type = any(%s)), 0), coalesce(sum(cash_amount) filter (where txn_type = any(%s)), 0)
                       from transactions where account_id = %s and status = 'ACTIVE' and cash_currency = 'PLN'""", (list(INCOME_TYPES), list(DEPOSIT_FLOWS), acc))
        income, net_ext = (_dec(x) for x in cur.fetchone())
        lhs = sec + _dec(m["value"]) - net_ext
        rhs = (sec - (buys - sells)) + income
        add(f"Lokata „{name}”: papiery + lokata − wpłaty netto z banku = zysk z inwestycji + dochody", abs(lhs - rhs) <= TOL,
            f"{lhs.quantize(TOL)} vs {rhs.quantize(TOL)} (wpłaty netto {net_ext.quantize(TOL)}, lokata {_dec(m['value']).quantize(TOL)})")
    if scope_code == "INVESTMENT_ALL":
        parts = sum((dep.scope_deposits(cur, b.scope_code)["value"] for b in _brokers()), D(0))
        add(f"WSZYSTKIE = {_names()}: lokaty", abs(parts - sd["value"]) <= TOL, f"{parts.quantize(TOL)} vs {_dec(sd['value']).quantize(TOL)}")
    return sd["value"]
