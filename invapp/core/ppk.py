"""
PPK - "Oszczednosci emerytalne" (decyzja uzytkownika 2026-11-19, docs/PPK.md). Warstwa bazy z migracji 0001 (sekcja 9a), tu logika aplikacji.

* Uzytkownik wpisuje dane recznie (ze strony instytucji, np. PKO TFI): rachunek (nazwa, instytucja, fundusz, data przystapienia)
  i STANY na dzien wyceny: wartosc calkowita wg instytucji + dla kazdej kategorii skladek wplaty narastajaco i wartosc.
  Opcjonalnie wkleja tekst ze strony - parse_text() tylko UZUPELNIA formularz (nic nie zapisuje; tekst nie jest przechowywany,
  bo zawiera np. nazwe pracodawcy i numer rejestru).
* Stan = niezmienna migawka (fn_ppk_commit_snapshot): ta sama tresc -> DUPLICATE, ten sam dzien z inna trescia -> CORRECTION (stara zostaje).
  Puste pole = brak danych (NIE zero); "0,00" = zero.
* Liczby: wartosc = wartosc wg instytucji; laczne wplaty = suma wplat kategorii (tylko gdy wszystkie znane);
  zmiana = wartosc - laczne wplaty (ORIENTACYJNA - brak historii jednostek, to nie jest stopa zwrotu);
  "ponad Twoje wplaty" = wartosc - wplaty pracownika (skladki pracodawcy i panstwa to tez Twoj kapital, ale nie Twoje pieniadze).
* Rachunek PPK nalezy do zakresow PPK i NET_WORTH - NIE do INVESTMENT_ALL. Ustawienie include_ppk (domyslnie wylaczone; decyzja 2026-11-19)
  wlacza PPK do portfela WSZYSTKIE: wartosc, wplaty (WSZYSTKIE skladki - pracownika, pracodawcy i panstwa - jako kapital, D-PPK-1),
  zysk (jedna linia "PPK" = wartosc - laczne wplaty) i seria dzienna (wykres, TWR, XIRR...) z modelu model() - patrz nizej.
"""
import calendar
import re
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from psycopg2.extras import Json

SCOPES = ("PPK", "NET_WORTH")
STALE_DAYS = 45                       # jak v_ppk_account_current.is_stale
APPROVED_BY = "wlasciciel"            # zatwierdzenie w formularzu (fn_ppk_commit_snapshot wymaga)
SOURCES = ("RECZNIE", "WKLEJONY_TEKST")


class PpkError(ValueError):
    pass


def categories(cur):
    cur.execute("""select category_code, name_pl, payer, is_additional from ppk_contribution_categories
                   order by case payer when 'EMPLOYEE' then 1 when 'EMPLOYER' then 2 else 3 end, is_additional, sort_order""")   # jak na wyciagu: Ty, pracodawca, panstwo
    return [{"kod": c, "nazwa": n, "platnik": p, "dodatkowa": a} for c, n, p, a in cur.fetchall()]


def _amount(cur, v, field, required=False):
    """Kwota wpisana przez uzytkownika -> Decimal (grosz) albo None (puste pole = brak danych). Ta sama regula co w bazie (fn_ppk_parse_amount):
    niejednoznaczne "1.234" odrzucone, nie zgadujemy."""
    s = "" if v is None else str(v).strip()
    if not s:
        if required:
            raise PpkError(f"Podaj {field}.")
        return None
    cur.execute("select fn_ppk_parse_amount(%s)", (s,))
    d = cur.fetchone()[0]
    if d is None:
        raise PpkError(f"{field[0].upper() + field[1:]}: nie rozpoznaję kwoty „{s[:30]}” – wpisz np. 1 234,56.")
    if d < 0:
        raise PpkError(f"{field[0].upper() + field[1:]}: kwota nie może być ujemna.")
    if d >= Decimal("1e13"):
        raise PpkError(f"{field[0].upper() + field[1:]}: zbyt duża kwota.")
    return Decimal(d).quantize(Decimal("0.01"))


def _day(v, field):
    s = "" if v is None else str(v).strip()
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise PpkError(f"Podaj {field} (RRRR-MM-DD).")
    if d > date.today():
        raise PpkError(f"{field[0].upper() + field[1:]} nie może być z przyszłości.")
    return d


def _text(v, field, maxlen=80, required=True):
    s = re.sub(r"\s+", " ", "" if v is None else str(v)).strip()
    if required and not s:
        raise PpkError(f"Podaj {field}.")
    if len(s) > maxlen:
        raise PpkError(f"{field[0].upper() + field[1:]}: maksymalnie {maxlen} znaków.")
    return s


def _institution(cur, name):
    """Instytucja (TFI) po nazwie bez wzgledu na wielkosc liter; nowa - wpis w rejestrze instytucji z kodem TFI:<NAZWA>."""
    cur.execute("select broker_id from financial_institutions where institution_kind = 'TFI' and lower(name) = lower(%s)", (name,))
    r = cur.fetchone()
    if r:
        return r[0]
    base = "TFI:" + (re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "INSTYTUCJA")[:40]
    code, n = base, 1
    while True:
        cur.execute("select 1 from financial_institutions where code = %s", (code,))
        if not cur.fetchone():
            break
        n += 1; code = f"{base}_{n}"
    cur.execute("insert into financial_institutions(code, name, institution_kind) values (%s, %s, 'TFI') returning broker_id", (code, name))
    return cur.fetchone()[0]


def add_account(cur, nazwa, instytucja, fundusz, data_przystapienia, rok_docelowy=None):
    """Nowy rachunek PPK: konto PPK/RETIREMENT/BALANCE_ONLY + retirement_accounts + zakresy PPK i NET_WORTH. Zwraca account_id."""
    nazwa = _text(nazwa, "nazwę rachunku", required=False) or "PPK"
    inst = _text(instytucja, "instytucję (np. PKO TFI)")
    fund = _text(fundusz, "nazwę funduszu (np. PKO Emerytura 2055)", 120)
    joined = _day(data_przystapienia, "datę przystąpienia (otwarcia rachunku)")
    year = None
    if rok_docelowy not in (None, ""):
        try:
            year = int(str(rok_docelowy).strip())
        except ValueError:
            year = 0
        if not 2000 <= year <= 2200:
            raise PpkError("Rok docelowy funduszu: podaj rok, np. 2055 (albo zostaw puste).")
    inst_id = _institution(cur, inst)
    cur.execute("""insert into retirement_funds(institution_id, name, target_year) values (%s, %s, %s)
                   on conflict (institution_id, name) do update set target_year = coalesce(excluded.target_year, retirement_funds.target_year)
                   returning fund_id""", (inst_id, fund, year))
    fund_id = cur.fetchone()[0]
    n = 1
    while True:
        cur.execute("select 1 from accounts where broker_id = %s and external_no = %s", (inst_id, f"PPK-{n}"))
        if not cur.fetchone():
            break
        n += 1
    cur.execute("""insert into accounts(broker_id, external_no, external_no_source, display_name, product_type, account_class, tracking_mode, tax_regime,
                                        base_currency, opened_at)
                   values (%s, %s, 'USER', %s, 'PPK', 'RETIREMENT', 'BALANCE_ONLY', 'PPK', 'PLN', %s) returning account_id""",
                (inst_id, f"PPK-{n}", nazwa, joined))
    aid = cur.fetchone()[0]
    cur.execute("insert into retirement_accounts(account_id, fund_id, joined_on) values (%s, %s, %s)", (aid, fund_id, joined))
    for code in SCOPES:
        cur.execute("insert into scope_accounts(scope_id, account_id) select scope_id, %s from scopes where code = %s", (aid, code))
    return aid


def _active(cur, aid):
    cur.execute("select joined_on from retirement_accounts where account_id = %s and status = 'ACTIVE'", (aid,))
    r = cur.fetchone()
    if not r:
        raise PpkError("Nie ma takiego rachunku PPK.")
    return r[0]


def add_snapshot(cur, aid, data_wyceny, wartosc, linie, zrodlo="RECZNIE"):
    """Stan rachunku na dzien wyceny. linie: {kod_kategorii: {"wplaty": "...", "wartosc": "..."}}; kategoria z oboma polami pustymi = brak wiersza
    (brak danych). Zwraca {"id", "wynik": CREATED|DUPLICATE|CORRECTION, "roznica": wartosc - suma wartosci kategorii albo None}."""
    joined = _active(cur, aid)
    d = _day(data_wyceny, "datę wyceny")
    if d < joined:
        raise PpkError("Data wyceny nie może być wcześniejsza niż data przystąpienia.")
    total = _amount(cur, wartosc, "wartość całkowitą rachunku", required=True)
    known = {c["kod"]: c["nazwa"] for c in categories(cur)}
    linie = linie if isinstance(linie, dict) else {}
    unknown = [k for k in linie if k not in known]
    if unknown:
        raise PpkError("Nieznana kategoria składki.")
    lines = []
    for code, name in known.items():
        x = linie.get(code) or {}
        if not isinstance(x, dict):
            raise PpkError("Niepoprawne dane kategorii.")
        p = _amount(cur, x.get("wplaty"), f"wpłaty – {name}")
        v = _amount(cur, x.get("wartosc"), f"wartość – {name}")
        if p is None and v is None:
            continue
        lines.append({"category": code, "paid_in": None if p is None else str(p), "value": None if v is None else str(v), "origin": "ENTERED"})
    src = zrodlo if zrodlo in SOURCES else "RECZNIE"
    cur.execute("select snapshot_id, outcome from fn_ppk_commit_snapshot(%s, %s, %s, %s, %s, %s)", (aid, d, total, Json(lines), APPROVED_BY, src))
    sid, outcome = cur.fetchone()
    cur.execute("select control_difference from ppk_snapshots where snapshot_id = %s", (sid,))
    return {"id": sid, "wynik": outcome, "roznica": cur.fetchone()[0]}


def archive(cur, aid):
    """Usuniecie z listy = zamkniecie rachunku (status CLOSED). Migawki sa niezmienne i zostaja."""
    _active(cur, aid)
    cur.execute("update retirement_accounts set status = 'CLOSED' where account_id = %s", (aid,))


def _ratio(a, b):
    return (a / b) if (a is not None and b) else None


WARNING_PL = {"PAID_IN_DECREASED": "Wpłaty narastająco zmalały względem poprzedniego stanu ({}) – sprawdź wpisaną kwotę (wpłaty narastająco nie powinny maleć, chyba że była wypłata).",
              "CATEGORY_MISSING": "Kategoria {} była w poprzednim stanie, a w tym jej nie ma – brak danych, nie zero. Jeśli wynosi 0, wpisz 0,00.",
              "CONTROL_MISMATCH": "Suma wartości kategorii różni się od wartości całkowitej o {} zł – sprawdź wpisane kwoty.",
              "INCOMPLETE_DATA": "Nie wszystkie kategorie mają wpisane kwoty ({}) – łączne wpłaty i zmiana wartości są wtedy nieznane."}


def overview(cur):
    """Aktywne rachunki PPK z biezacym stanem, kategoriami, historia stanow i ostrzezeniami. Kwoty w PLN (Decimal)."""
    cat = {c["kod"]: c for c in categories(cur)}
    cur.execute("""select a.account_id, a.display_name, f.name, rf.name, rf.target_year, ra.joined_on
                   from retirement_accounts ra join accounts a using (account_id) join financial_institutions f on f.broker_id = a.broker_id
                   join retirement_funds rf on rf.fund_id = ra.fund_id
                   where ra.status = 'ACTIVE' order by a.account_id""")
    out = []
    for aid, name, inst, fund, year, joined in cur.fetchall():
        acc = {"id": aid, "nazwa": name, "instytucja": inst, "fundusz": fund, "rok_docelowy": year, "data_przystapienia": joined,
               "stan": None, "kategorie": [], "historia": [], "ostrzezenia": []}
        cur.execute("""select snapshot_id, valuation_date, value, control_difference, control_status, days_since_valuation, is_stale
                       from v_ppk_account_current where account_id = %s""", (aid,))
        r = cur.fetchone()
        if r:
            sid, vd, value, diff, status, days, stale = r
            cur.execute("select category_code, paid_in, value from ppk_snapshot_lines where snapshot_id = %s", (sid,))
            lines = {c: (p, v) for c, p, v in cur.fetchall()}
            rows = []
            for code, c in cat.items():
                if code not in lines:
                    continue
                p, v = lines[code]
                rows.append({"kod": code, "nazwa": c["nazwa"], "platnik": c["platnik"], "wplaty": p, "wartosc": v,
                             "zmiana": (v - p) if (p is not None and v is not None) else None, "zmiana_pct": _ratio((v - p) if (p is not None and v is not None) else None, p)})
            paid_known = bool(rows) and all(x["wplaty"] is not None for x in rows)
            paid = sum((x["wplaty"] for x in rows), Decimal(0)) if paid_known else None
            emp_rows = [x for x in rows if x["platnik"] == "EMPLOYEE"]
            own = sum((x["wplaty"] for x in emp_rows), Decimal(0)) if (emp_rows and all(x["wplaty"] is not None for x in emp_rows)) else None
            by = {}
            for payer in ("EMPLOYEE", "EMPLOYER", "STATE"):
                xs = [x for x in rows if x["platnik"] == payer]
                by[payer] = sum((x["wplaty"] for x in xs), Decimal(0)) if (xs and all(x["wplaty"] is not None for x in xs)) else None
            change = (value - paid) if paid is not None else None
            over_own = (value - own) if own is not None else None
            acc["stan"] = {"id": sid, "data_wyceny": vd, "wartosc": value, "dni_od_wyceny": days, "nieaktualna": stale, "kontrola": status, "roznica": diff,
                           "wplaty": paid, "zmiana": change, "zmiana_pct": _ratio(change, paid),
                           "wplaty_wlasne": own, "ponad_wlasne": over_own, "ponad_wlasne_pct": _ratio(over_own, own),
                           "wplaty_pracownik": by["EMPLOYEE"], "wplaty_pracodawca": by["EMPLOYER"], "wplaty_panstwo": by["STATE"]}
            acc["kategorie"] = rows
            cur.execute("""select w.warning, w.detail, c.name_pl from v_ppk_snapshot_warnings w left join ppk_contribution_categories c on c.category_code = w.detail
                           where w.snapshot_id = %s order by 1, 2""", (sid,))
            for wtype, detail, cname in cur.fetchall():
                arg = cname or ("wartości" if detail == "values" else "wpłaty" if detail == "paid_in" else detail)
                acc["ostrzezenia"].append(WARNING_PL.get(wtype, wtype).format(arg))
        cur.execute("""select h.snapshot_id, h.valuation_date, h.value, h.paid_in_total, h.paid_in_complete, s.entered_at, s.supersedes_snapshot_id is not null, s.source
                       from v_ppk_history h join ppk_snapshots s using (snapshot_id) where h.account_id = %s order by h.valuation_date""", (aid,))
        for sid, vd, value, paid, complete, entered, corr, src in cur.fetchall():
            paid = paid if complete else None
            acc["historia"].append({"id": sid, "data_wyceny": vd, "wartosc": value, "wplaty": paid, "zmiana": (value - paid) if paid is not None else None,
                                    "wpisano": entered, "poprawka": corr, "zrodlo": src})
        out.append(acc)
    return out


def total_value(cur):
    """Suma biezacych wartosci aktywnych rachunkow PPK (PLN; najnowszy stan kazdego rachunku)."""
    cur.execute("""select coalesce(sum(c.value), 0) from v_ppk_account_current c join retirement_accounts ra using (account_id) where ra.status = 'ACTIVE'""")
    return Decimal(cur.fetchone()[0])


# ---------------------------------------------------------------- model w czasie (PPK w portfelu WSZYSTKIE; decyzja 2026-11-19)
# Instytucja podaje tylko STANY (wplaty narastajaco i wartosc na dzien wyceny). Do wykresu i wskaznikow portfela potrzebne sa dni wplat
# i wartosc dzien po dniu - odtwarzane jawnie oznaczonym MODELEM (kazda kwota sumuje sie co do grosza do wpisanych stanow):
#   * skladki pracownika i pracodawcy: co miesiac 15. dnia (pracodawca przekazuje skladki do 15. dnia nastepnego miesiaca), pierwsza
#     w miesiacu po przystapieniu; przyrost wplat miedzy stanami rozlozony rowno na te dni (reszta groszy w ostatniej),
#   * wplata powitalna: po 3 pelnych miesiacach uczestnictwa, ksiegowana po kwartale (koniec kwartalu + WELCOME_DELAY_DAYS),
#   * doplata roczna: 15 kwietnia roku nastepnego (ustawa o PPK); przyrost = n x 240 zl -> po 240 zl na kolejne 15 kwietnia,
#   * dzien bez pasujacego terminu (np. powitalna pozniej niz wynika z regul) -> dzien stanu, w ktorym kwota sie pojawila.
#   Wartosc: miedzy kolejnymi stanami jedna stala stopa dzienna g, dobrana tak, by (wartosc poprzedniego stanu + wplaty) rosnac w tempie g
#   dawaly DOKLADNIE wartosc nastepnego stanu: V(t) = V(t-1) x g + wplata(t). Po ostatnim stanie wartosc stala (brak informacji).
#   Rachunek, ktorego najnowszy stan nie ma wszystkich wplat (brak danych), nie jest modelowany (wartosc tylko w kafelku, bez zysku).
MONTHLY = ("EMPLOYEE_BASIC", "EMPLOYEE_ADDITIONAL", "EMPLOYER_BASIC", "EMPLOYER_ADDITIONAL")
WELCOME_DELAY_DAYS = 30
ANNUAL_AMOUNT = Decimal("240.00")
CENT = Decimal("0.01")


def _add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def welcome_date(joined):
    """Wplata powitalna: 3 pelne miesiace kalendarzowe uczestnictwa, zaksiegowana WELCOME_DELAY_DAYS po koncu kwartalu, w ktorym minely."""
    first_full = joined if joined.day == 1 else _add_months(joined, 1)
    third = _add_months(first_full, 2)
    q_end_month = ((third.month - 1) // 3 + 1) * 3
    q_end = date(third.year, q_end_month, calendar.monthrange(third.year, q_end_month)[1])
    return q_end + timedelta(days=WELCOME_DELAY_DAYS)


def _split(total, n):
    """Kwota rozlozona na n czesci po rowno, reszta groszy w ostatniej (suma = total co do grosza)."""
    q = (total / n).quantize(CENT, ROUND_DOWN)
    return [q] * (n - 1) + [total - q * (n - 1)]


def _flows_between(cat, delta, prev, day, joined):
    """Wplaty jednej kategorii odpowiadajace przyrostowi `delta` miedzy stanem z dnia `prev` (wylacznie) a stanem z dnia `day` (wlacznie)."""
    if not delta:
        return []
    if delta < 0:                                       # spadek wplat narastajaco (wyplata / korekta) - w dniu stanu, bez zgadywania
        return [(day, delta)]
    if cat in MONTHLY:
        dates, d = [], _add_months(joined, 1).replace(day=15)
        while d <= day:
            if d > prev:
                dates.append(d)
            d = _add_months(d, 1).replace(day=15)
    elif cat == "WELCOME":
        w = welcome_date(joined)
        dates = [w] if prev < w <= day else []
    elif cat == "STATE_ANNUAL":
        dates = [date(y, 4, 15) for y in range(joined.year + 1, day.year + 1) if prev < date(y, 4, 15) <= day]
        n = int(delta / ANNUAL_AMOUNT) if delta % ANNUAL_AMOUNT == 0 else 0
        if n and n <= len(dates):                          # n doplat po 240 zl: na NAJNOWSZE terminy (starsze juz byly w poprzednim stanie)
            return [(d, ANNUAL_AMOUNT) for d in dates[-n:]]
    else:
        dates = []
    if not dates:
        return [(day, delta)]
    return list(zip(dates, _split(delta, len(dates))))


def _rate(v0, flows, a, b, vb):
    """Stala stopa dzienna g na odcinku (a, b]: v0 x g^(b-a) + suma f x g^(b-t) = vb (bisekcja po stopie rocznej). None, gdy brak rozwiazania."""
    n = (b - a).days
    if n <= 0:
        return None

    def fv(r):
        g = (1 + r) ** (1 / 365)
        return float(v0) * g ** n + sum(float(f) * g ** (b - t).days for t, f in flows)
    lo, hi = -0.95, 10.0
    flo, fhi = fv(lo) - float(vb), fv(hi) - float(vb)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = fv(mid) - float(vb)
        if (fm < 0) == (flo < 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (1 + (lo + hi) / 2) ** (1 / 365)


def account_model(cur, aid, joined, end):
    """Model jednego rachunku: {"flows": [(dzien, kwota)], "daily": {dzien: wartosc}, "paid": wplaty, "value": wartosc, "anchors": [(dzien, wartosc, wplaty)],
    "annual_rates": [...]} albo None (najnowszy stan bez kompletnych wplat)."""
    cur.execute("""select s.snapshot_id, s.valuation_date, s.institution_total_value, s.paid_in_complete from v_ppk_effective_snapshots s
                   where s.account_id = %s order by s.valuation_date""", (aid,))
    snaps = cur.fetchall()
    if not snaps or not snaps[-1][3]:
        return None
    anchors, flows, prev_day, prev_paid = [], [], joined, {}
    for sid, day, value, complete in snaps:
        if not complete:
            continue
        cur.execute("select category_code, paid_in from ppk_snapshot_lines where snapshot_id = %s", (sid,))
        paid = {c: Decimal(p) for c, p in cur.fetchall()}
        for cat in sorted(set(paid) | set(prev_paid)):
            flows += _flows_between(cat, paid.get(cat, Decimal(0)) - prev_paid.get(cat, Decimal(0)), prev_day, day, joined)
        anchors.append((day, Decimal(value), sum(paid.values(), Decimal(0))))
        prev_day, prev_paid = day, paid
    by_day = {}
    for d, f in flows:
        by_day[d] = by_day.get(d, Decimal(0)) + f
    start = min([joined] + list(by_day))
    # odcinki (a, b]: pierwszy od dnia przed przystapieniem (wartosc 0), kolejne od poprzedniego stanu; V(d) = V(d-1) x g + wplata(d)
    daily, rates, v, a, va = {}, [], 0.0, start - timedelta(days=1), Decimal(0)
    for day, value, _ in anchors:
        seg = [(d, f) for d, f in sorted(by_day.items()) if a < d <= day]
        g = _rate(va, seg, a, day, value)
        rates.append(None if g is None else g ** 365 - 1)
        step = float(value - va - sum((f for _, f in seg), Decimal(0))) / max((day - a).days, 1)
        d = a + timedelta(days=1)
        while d <= day:
            f = float(by_day.get(d, Decimal(0)))
            v = (v * g + f) if g is not None else (v + f + step)   # brak stalej stopy (np. wyplata) - przyrost liniowy
            daily[d] = Decimal(str(v)).quantize(CENT)
            d += timedelta(days=1)
        daily[day] = value                              # dzien stanu: dokladnie wartosc wg instytucji
        v, a, va = float(value), day, value
    d = a + timedelta(days=1)
    while d <= end:
        daily[d] = va
        d += timedelta(days=1)
    return {"flows": sorted(by_day.items()), "daily": daily, "paid": anchors[-1][2], "value": anchors[-1][1], "anchors": anchors, "annual_rates": rates}


def model(cur, end=None):
    """Wszystkie aktywne rachunki PPK razem: {"flows", "daily", "paid", "value", "gain", "start", "unmodeled": [nazwy]}; flows/daily posortowane."""
    end = end or date.today()
    cur.execute("""select ra.account_id, a.display_name, ra.joined_on from retirement_accounts ra join accounts a using (account_id)
                   where ra.status = 'ACTIVE' order by 1""")
    flows, daily, paid, value, unmodeled, start = {}, {}, Decimal(0), Decimal(0), [], None
    for aid, name, joined in cur.fetchall():
        m = account_model(cur, aid, joined, end)
        if m is None:
            unmodeled.append(name); continue
        for d, f in m["flows"]:
            flows[d] = flows.get(d, Decimal(0)) + f
        for d, v in m["daily"].items():
            daily[d] = daily.get(d, Decimal(0)) + v
        paid += m["paid"]; value += m["value"]
        first = min(m["daily"]) if m["daily"] else None
        start = first if start is None or (first and first < start) else start
    return {"flows": sorted(flows.items()), "daily": daily, "paid": paid, "value": value, "gain": value - paid, "start": start, "unmodeled": unmodeled}


# ---------------------------------------------------------------- wklejony tekst -> pola formularza (nic nie zapisuje)
_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzacelnoszz")
_AMOUNT = re.compile(r"(?<![\d.,])-?\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,2})?(?![\d])|(?<![\d.,])-?\d+(?:[.,]\d{1,2})?(?![\d.,])")
_DATE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")


def _norm(s):
    """Jak fn_ppk_normalize_label w bazie: male litery, bez polskich znakow i interpunkcji, pojedyncze spacje."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").translate(_PL).lower()).strip()


def _dates(s):
    out = []
    for m in _DATE.finditer(s):
        try:
            out.append(date(int(m[3]), int(m[2]), int(m[1])) if m[1] else date(int(m[4]), int(m[5]), int(m[6])))
        except ValueError:
            pass
    return out


def parse_text(cur, text):
    """Tekst skopiowany ze strony instytucji (albo podsumowanie) -> propozycja pol formularza: data wyceny, wartosc calkowita, kwoty kategorii,
    instytucja, data przystapienia. Wiersz kategorii: etykieta + dwie kwoty (wplaty, wartosc - kolejnosc kolumn jak na stronie PKO TFI).
    Wiersz z jedna kwota albo nierozpoznana etykieta -> uwaga (nie zgadujemy). Nic nie jest zapisywane."""
    text = (text or "")[:20000]
    raw = [l.strip() for l in text.splitlines()]
    lines = [l for l in raw if l]
    cur.execute("select alias_normalized, category_code from ppk_category_aliases where institution_id is null")
    aliases = sorted(cur.fetchall(), key=lambda a: -len(a[0]))
    names = {c["kod"]: c["nazwa"] for c in categories(cur)}

    def amounts(s):
        vals = []
        for tok in _AMOUNT.findall(s):
            cur.execute("select fn_ppk_parse_amount(%s)", (tok,))
            v = cur.fetchone()[0]
            if v is not None:
                vals.append(Decimal(v).quantize(Decimal("0.01")))
        return vals

    def after(i, pred):
        """Wartosc z tej samej linii, a gdy jej brak - z nastepnej (uklad "etykieta / kwota pod spodem")."""
        v = pred(lines[i])
        if not v and i + 1 < len(lines):
            v = pred(lines[i + 1])
        return v

    out = {"linie": {}, "uwagi": []}
    for i, line in enumerate(lines):
        n = _norm(line)
        hit = next((code for alias, code in aliases if n == alias or n.startswith(alias + " ")), None)
        if hit:
            label_cut = re.sub(r"^[^\d]*", "", line)                  # kwoty po etykiecie (etykiety nie maja cyfr)
            vals = amounts(label_cut)
            if len(vals) >= 2:
                if hit in out["linie"]:
                    out["uwagi"].append(f"„{names[hit]}” występuje w tekście więcej niż raz – użyto pierwszego wiersza.")
                    continue
                out["linie"][hit] = {"wplaty": str(vals[0]), "wartosc": str(vals[1])}
                if len(vals) > 2:
                    out["uwagi"].append(f"„{names[hit]}”: więcej niż dwie kwoty w wierszu – użyto dwóch pierwszych (wpłaty, wartość); sprawdź.")
            else:
                out["uwagi"].append(f"„{names[hit]}”: w wierszu jest {'jedna kwota' if vals else 'brak kwot'} – wpisz wpłaty i wartość ręcznie.")
            continue
        if "wartosc_calkowita" not in out and ("wartosc calkowita" in n or "wartosc rachunku" in n or n.startswith("wartosc srodkow")):
            v = after(i, amounts)
            if v:
                out["wartosc_calkowita"] = str(v[0])
        elif "data_wyceny" not in out and ("data wyceny" in n or n.startswith("wycena")):
            v = after(i, _dates)
            if v:
                out["data_wyceny"] = v[0].isoformat()
        elif "data_przystapienia" not in out and ("data otwarcia" in n or "data przystapienia" in n or "data zawarcia" in n):
            v = after(i, _dates)
            if v:
                out["data_przystapienia"] = v[0].isoformat()
        elif "instytucja" not in out and (n.startswith("instytucja") or n.startswith("instytucja finansowa")) and ":" in line:
            name = line.split(":", 1)[1].strip().rstrip(".")
            if name:
                out["instytucja"] = name[:80]
    if "data_wyceny" not in out:                                     # "PKO TFI · wycena 17.11.2026"
        for line in lines:
            if "wycena" in _norm(line):
                v = _dates(line)
                if v:
                    out["data_wyceny"] = v[0].isoformat(); break
    if not out["linie"]:
        out["uwagi"].append("Nie znalazłem wierszy składek (np. „Składka podstawowa pracownika 1 234,56 1 500,00”) – wpisz kwoty ręcznie.")
    if out["linie"] and "wartosc_calkowita" in out:
        vals = [Decimal(x["wartosc"]) for x in out["linie"].values()]
        diff = Decimal(out["wartosc_calkowita"]) - sum(vals, Decimal(0))
        if diff:
            out["uwagi"].append(f"Suma wartości kategorii różni się od wartości całkowitej o {diff} zł – sprawdź, czy wszystkie wiersze zostały odczytane.")
    return out
