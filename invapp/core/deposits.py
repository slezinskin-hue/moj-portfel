"""
Gotowka oprocentowana traktowana jak LOKATA (decyzja uzytkownika 2026-11-12, Trade Republic: "srodki w gotowce potraktuj jako na lokacie 6% w skali roku
z comiesieczna kapitalizacja i od tego 19% podatku Belki"; wartosc aktywow to takze lokaty).

Model (sprawdzony na ksiedze: odtwarza KAZDA wyplate odsetek Trade Republic co do grosza):
* odsetki dzienne = saldo na koniec dnia x stopa roczna / 365;
* odsetki za miesiac sa wyplacane (kapitalizowane) 1. dnia nastepnego miesiaca; od wyplaty broker pobiera podatek (19%);
* saldo gotowki = suma wszystkich operacji gotowkowych konta z ksiegi (wplaty, wyplaty, zakupy, sprzedaze, odsetki, podatek, nagrody, oplaty).
Wartosc lokaty = SALDO Z KSIEGI (stan na dzien eksportu): tylko to, co zostalo zaksiegowane/wyplacone (decyzja uzytkownika 2026-11-12:
"nie licz jeszcze nie zaksiegowanych rzeczy, tylko to co zostalo wyplacone"). Odsetki naliczone od ostatniej wyplaty (i wyplaty, ktorych eksport
jeszcze nie zawiera) model liczy TYLKO INFORMACYJNIE (pole unbooked_interest_net) - nie wchodza do wartosci, zysku ani wykresu.
Model sluzy tez do wyznaczenia stopy z wyplaconych odsetek i do kontroli (odtworzenie kazdej wyplaty z ksiegi).
Czysta logika (model) + odczyt z bazy (account_deposit, scope_deposits). Tylko odczyt ksiegi.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal as D

CENT = D("0.01")


def _first_of_month(d):
    return d.replace(day=1)


def _prev_month_days(pay_day):
    end = pay_day - timedelta(days=1)
    start = end.replace(day=1)
    return [start + timedelta(days=k) for k in range((end - start).days + 1)]


def eod_balances(txns, end):
    """txns: [(dzien, kwota, typ)] -> {dzien: saldo na koniec dnia} od pierwszej operacji do `end` (wlacznie)."""
    if not txns:
        return {}
    by_day = defaultdict(D)
    for d, amt, _ in txns:
        by_day[d] += D(str(amt))
    out, bal, d = {}, D(0), min(by_day)
    while d <= end:
        bal += by_day.get(d, D(0))
        out[d] = bal
        d += timedelta(days=1)
    return out


def payouts(txns):
    """Wyplaty odsetek z ksiegi: {dzien: (brutto, podatek)}."""
    out = defaultdict(lambda: [D(0), D(0)])
    for d, amt, typ in txns:
        if typ == "INTEREST":
            out[d][0] += D(str(amt))
        elif typ == "TAX_INTEREST":
            out[d][1] += D(str(amt))
    return {d: (g, t) for d, (g, t) in out.items()}


def implied_rate(txns, day_count=365):
    """Stopa roczna wynikajaca z OSTATNIEJ wyplaty odsetek: brutto / (suma sald dziennych miesiaca / liczba dni roku). None, gdy brak wyplat."""
    p = payouts(txns)
    if not p:
        return None
    last = max(p)
    bal = eod_balances(txns, last)
    base = sum((bal.get(d, D(0)) for d in _prev_month_days(last)), D(0))
    if base <= 0:
        return None
    return (p[last][0] / (base / day_count)).quantize(D("0.0001"), ROUND_HALF_UP)


def reproduce_payouts(txns, rate, day_count=365):
    """Kontrola modelu: dla kazdej wyplaty z ksiegi odsetki policzone modelem z sald dziennych poprzedniego miesiaca."""
    p = payouts(txns)
    if not p:
        return []
    bal = eod_balances(txns, max(p))
    out = []
    for d in sorted(p):
        days = _prev_month_days(d)                  # dni przed pierwsza operacja: saldo 0
        est = sum((bal.get(x, D(0)) * D(str(rate)) / day_count for x in days), D(0)).quantize(CENT, ROUND_HALF_UP)
        out.append({"date": d, "booked": p[d][0], "model": est, "diff": p[d][0] - est, "tax_booked": p[d][1]})
    return out


def model(txns, rate, tax_rate, today, day_count=365):
    """Wartosc lokaty = saldo z ksiegi; szereg dzienny [(dzien, saldo na koniec dnia)] do `today` (po dniu eksportu - ostatnie saldo).
    INFORMACYJNIE (nie wchodzi do wartosci): odsetki niezaksiegowane - wyplaty po dniu eksportu, ktore model kapitalizuje 1. dnia miesiaca
    (brutto - podatek), i odsetki naliczone od ostatniej kapitalizacji."""
    rate, tax_rate = D(str(rate)), D(str(tax_rate))
    if not txns:
        return None
    ledger_end = max(d for d, _, _ in txns)
    end = max(today, ledger_end)
    bal = eod_balances(txns, end)
    booked = payouts(txns)
    last_booked = max(booked) if booked else None
    acc = D(0); extra = D(0); capitalized = []; daily = []
    for d in sorted(bal):
        if d in booked and d <= ledger_end:
            acc = D(0)                                         # wyplata z ksiegi (1. dnia miesiaca) zamyka naliczanie za poprzedni miesiac
        elif d > ledger_end and d.day == 1 and acc > 0:        # kapitalizacja, ktorej eksport jeszcze nie zawiera (szacunek)
            gross = acc.quantize(CENT, ROUND_HALF_UP); tax = (gross * tax_rate).quantize(CENT, ROUND_HALF_UP)
            extra += gross - tax; capitalized.append({"date": d, "gross": gross, "tax": tax, "net": gross - tax}); acc = D(0)
        acc += (bal[d] + extra) * rate / day_count             # odsetki za dzien d (od salda na koniec dnia) - tylko informacyjnie
        daily.append((d, bal[d].quantize(CENT, ROUND_HALF_UP)))   # wartosc dnia = saldo z ksiegi (bez niezaksiegowanych odsetek)
    accrued_gross = acc.quantize(CENT, ROUND_HALF_UP)
    accrued_tax = (accrued_gross * tax_rate).quantize(CENT, ROUND_HALF_UP)
    ledger_balance = bal[ledger_end]
    return {"ledger_balance": ledger_balance, "ledger_as_of": ledger_end, "last_payout": last_booked,
            "capitalized_after_export": capitalized, "capitalized_net": extra,
            "accrued_gross": accrued_gross, "accrued_tax": accrued_tax, "accrued_net": accrued_gross - accrued_tax,
            "unbooked_interest_net": extra + accrued_gross - accrued_tax,          # NIE wchodzi do wartosci (decyzja uzytkownika)
            "value": ledger_balance.quantize(CENT, ROUND_HALF_UP), "as_of": today, "rate": rate, "tax_rate": tax_rate, "day_count": day_count,
            "daily": [(d, v) for d, v in daily if d <= today]}


# ---------------------------------------------------------------- baza
def current_terms(cur, account_id):
    cur.execute("""select treat_as_deposit, annual_rate, tax_rate, day_count, capitalization, rate_source, decided_by, decided_at
                   from cash_deposit_terms where account_id = %s order by decided_at desc, terms_id desc limit 1""", (account_id,))
    r = cur.fetchone()
    if not r:
        return None
    return dict(zip(("treat_as_deposit", "annual_rate", "tax_rate", "day_count", "capitalization", "rate_source", "decided_by", "decided_at"), r))


def account_txns(cur, account_id):
    cur.execute("""select (t.occurred_at at time zone 'UTC')::date, t.cash_amount, t.txn_type from transactions t
                   where t.account_id = %s and t.status = 'ACTIVE' and t.cash_currency = 'PLN' and t.cash_amount is not null order by t.occurred_at, t.txn_id""", (account_id,))
    return cur.fetchall()


def account_deposit(cur, account_id, today=None):
    terms = current_terms(cur, account_id)
    if not terms or not terms["treat_as_deposit"]:
        return None
    today = today or datetime.now(timezone.utc).date()
    m = model(account_txns(cur, account_id), terms["annual_rate"], terms["tax_rate"], today, terms["day_count"])
    if m is None:
        return None
    cur.execute("select a.display_name, f.code from accounts a join financial_institutions f using (broker_id) where a.account_id = %s", (account_id,))
    name, broker = cur.fetchone()
    m.update({"account_id": account_id, "account": name, "broker": broker, "terms": terms})
    return m


def scope_deposits(cur, scope_code, today=None, with_daily=False):
    """Lokaty kont w zakresie: lista per konto i suma wartosci."""
    cur.execute("""select distinct t.account_id from cash_deposit_terms t join scope_accounts sa on sa.account_id = t.account_id
                   join scopes s on s.scope_id = sa.scope_id where s.code = %s order by 1""", (scope_code,))
    items = []
    for (acc,) in cur.fetchall():
        m = account_deposit(cur, acc, today)
        if m:
            if not with_daily:
                m.pop("daily", None)
            items.append(m)
    total = sum((m["value"] for m in items), D(0))
    unbooked = sum((m["unbooked_interest_net"] for m in items), D(0))
    return {"items": items, "value": total, "unbooked_interest_net": unbooked}
