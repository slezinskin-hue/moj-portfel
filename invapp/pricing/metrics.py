"""
Wskazniki inwestorskie liczone z dziennej serii portfela (pricing.history.build_series). Czysta logika (bez bazy), wiec latwa do przetestowania.

Podstawa: PAPIERY WARTOSCIOWE (jak wykres). Przeplyw dnia = zmiana "wplat netto" (zakup = wplata, sprzedaz = wyplata); wolne srodki poza seria.
Dywidendy i odsetki sa "dochodami dodatkowymi": wchodza tylko do wskaznikow oznaczonych "z dochodami" (XIRR z dochodami, stopa dywidendy).

Definicje (opisane tez w interfejsie):
* Zysk (zl)            = wartosc papierow - wplaty netto  (= niezrealizowany + zrealizowany zysk z inwestycji).
* Zysk % od zakupow    = zysk / suma wszystkich zakupow.
* TWR (stopa zwrotu wazona czasem) - lancuch dziennych stop zwrotu r_t = V_t / (V_{t-1} + F_t) - 1, gdzie F_t to przeplyw dnia (zakup > 0, sprzedaz < 0);
  nie zalezy od tego, kiedy i ile dokupiono (porownywalna z funduszem/indeksem). Roczna (CAGR) = (1+TWR)^(365/dni) - 1, pokazywana od okresu >= 365 dni.
* XIRR (stopa wazona pieniadzem) - roczna stopa, dla ktorej suma zdyskontowanych przeplywow (zakup = -, sprzedaz = +, wartosc koncowa = +) wynosi 0;
  uwzglednia moment i wielkosc wplat (Twoj faktyczny wynik). Pokazywana od okresu >= 90 dni.
* Obsuniecie maksymalne - najwiekszy spadek indeksu TWR od szczytu do dolka (bez wplywu wplat/wyplat), w PROCENTACH; dla portfela rosnacego dzis
  ten sam procent oznacza wieksza kwote niz kiedys, wiec obok sa epizody obsuniec z ZMIANA ZYSKU w zl oraz najwiekszy spadek zysku w zl
  (zysk = wartosc papierow - wplaty netto).
* Zmiennosc roczna     - odchylenie standardowe dziennych stop zwrotu (dni robocze) x pierwiastek z 252.
* Zwrot / ryzyko       - roczny TWR / zmiennosc roczna (BEZ stopy wolnej od ryzyka, wiec to nie jest klasyczny wskaznik Sharpe'a).
* Stopa dywidendy      - dywidendy netto z ostatnich 12 miesiecy / biezaca wartosc papierow (oraz do kosztu otwartych pozycji).
* Mapa miesiecznych zwrotow (monthly) - TWR kazdego miesiaca kalendarzowego (lancuch dziennych stop dni tego miesiaca); iloczyn miesiecy = TWR roku.
* Wykres "Zwrot %" (return_series): TWR, TWR z dywidendami, zysk / wplaty netto, zysk / suma wplat, XIRR od poczatku do dnia, roczny TWR (CAGR).
  To tylko inne SPOSOBY pokazania tych samych liczb (wartosc, wplaty, dywidendy) - zadna kwota w aplikacji sie nie zmienia.
"""
from collections import defaultdict
from datetime import date, timedelta
from math import sqrt

XIRR_MIN_DAYS = 90
CAGR_MIN_DAYS = 365


def _d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def _points(series):
    """Lista (data, wartosc, przeplyw dnia) jako float. Przeplyw pierwszego dnia = wplaty netto z pierwszego dnia."""
    out, prev_inv = [], 0.0
    for p in series:
        inv = float(p["invested_net"])
        out.append((_d(p["date"]), float(p["value"]), inv - prev_inv, float(p["invested_gross"])))
        prev_inv = inv
    return out


def twr_returns(points):
    """Dzienne stopy zwrotu wazone czasem: [(data, r)] od drugiego dnia. Gdy podstawa <= 0 (portfel pusty) r = 0."""
    res = []
    for i in range(1, len(points)):
        d, v, f, _ = points[i]
        base = points[i - 1][1] + f
        res.append((d, (v / base - 1.0) if base > 1e-9 else 0.0))
    return res


def chain(returns, start=None, end=None):
    """Zlozona stopa zwrotu z dziennych stop w przedziale (start, end] (start = zamkniecie dnia start). None gdy brak dni."""
    acc, n = 1.0, 0
    for d, r in returns:
        if (start is None or d > start) and (end is None or d <= end):
            acc *= 1.0 + r; n += 1
    return (acc - 1.0) if n else None


def annualize(total, days):
    if total is None or days < CAGR_MIN_DAYS or total <= -1.0:
        return None
    return (1.0 + total) ** (365.0 / days) - 1.0


def xirr(flows, lo=-0.9999, hi=100.0):
    """Roczna stopa XIRR dla [(data, kwota)]; None, gdy brak zmiany znaku lub rozwiazania."""
    flows = [(d, a) for d, a in flows if abs(a) > 1e-9]
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    d0 = min(d for d, _ in flows)

    def f(rate):
        return sum(a / (1.0 + rate) ** ((d - d0).days / 365.0) for d, a in flows)
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0; fm = f(mid)
        if abs(fm) < 1e-9:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def max_drawdown(returns, first_date):
    """Najwiekszy spadek indeksu TWR od szczytu: (glebokosc <= 0, data szczytu, data dolka) oraz biezace obsuniecie od szczytu."""
    idx, peak, peak_d = 1.0, 1.0, first_date
    worst, w_peak_d, w_trough_d = 0.0, first_date, first_date
    for d, r in returns:
        idx *= 1.0 + r
        if idx > peak:
            peak, peak_d = idx, d
        dd = idx / peak - 1.0
        if dd < worst:
            worst, w_peak_d, w_trough_d = dd, peak_d, d
    return {"depth": worst, "peak": w_peak_d.isoformat(), "trough": w_trough_d.isoformat(), "current": idx / peak - 1.0}


def drawdown_episodes(returns, gains, first_date, top=5):
    """Epizody obsuniecia indeksu TWR: od szczytu do powrotu na ten szczyt (lub do dzis). Dla kazdego: glebokosc %, zmiana ZYSKU w zl miedzy szczytem a dolkiem
    (zysk = wartosc - wplaty netto, wiec wplaty i wyplaty go nie zmieniaja), data powrotu. Zwraca `top` najglebszych."""
    idx, peak, peak_d = 1.0, 1.0, first_date
    eps, cur = [], None
    for d, r in returns:
        idx *= 1.0 + r
        if idx >= peak - 1e-12:
            if cur is not None:
                cur["recovered"] = d.isoformat(); eps.append(cur); cur = None
            peak, peak_d = max(peak, idx), d
        else:
            dd = idx / peak - 1.0
            if cur is None:
                cur = {"peak": peak_d.isoformat(), "trough": d.isoformat(), "depth": dd, "recovered": None}
            elif dd < cur["depth"]:
                cur["trough"], cur["depth"] = d.isoformat(), dd
    if cur is not None:
        eps.append(cur)
    for e in eps:
        gp, gt = gains.get(date.fromisoformat(e["peak"])), gains.get(date.fromisoformat(e["trough"]))
        e["profit_change"] = (gt - gp) if (gp is not None and gt is not None) else None
        e["ongoing"] = e["recovered"] is None
    eps.sort(key=lambda e: e["depth"])
    return eps[:top]


def profit_drawdown(gains_list):
    """Najwiekszy spadek ZYSKU w zl od szczytu do dolka ([(data, zysk)]) oraz obecna odleglosc od szczytu zysku."""
    peak_g, peak_d = None, None
    worst = {"amount": 0.0, "peak": None, "trough": None, "peak_gain": None, "trough_gain": None}
    for d, g in gains_list:
        if peak_g is None or g > peak_g:
            peak_g, peak_d = g, d
        if g - peak_g < worst["amount"]:
            worst = {"amount": g - peak_g, "peak": peak_d.isoformat(), "trough": d.isoformat(), "peak_gain": peak_g, "trough_gain": g}
    cur = (gains_list[-1][1] - peak_g) if gains_list else 0.0
    worst["current"] = cur
    return worst


def _xirr_newton(flows, guess=0.1):
    """XIRR metoda Newtona (szybka, do serii w czasie); None, gdy nie zbiega - wtedy wolajacy uzywa bisekcji (xirr)."""
    d0 = flows[0][0]
    ts = [((d - d0).days / 365.0, a) for d, a in flows]
    r = guess
    for _ in range(50):
        if r <= -0.9999:
            return None
        f = df = 0.0
        for t, a in ts:
            k = (1.0 + r) ** -t
            f += a * k; df -= t * a * k / (1.0 + r)
        if abs(df) < 1e-14:
            return None
        nr = r - f / df
        if abs(nr - r) < 1e-10:
            return nr if nr > -0.9999 else None
        r = nr
    return None


def return_series(pts, income=None, step=7):
    """Miary procentowe dzien po dniu (do wykresu "Zwrot %"), wyrownane z seria:
    * twr_inc  - indeks TWR z dywidendami: r_t = (V_t + D_t) / (V_{t-1} + F_t) - 1 (dywidenda netto dnia D_t jak zwrot);
    * gain_net - zysk / wplaty netto (prosta stopa zwrotu), gain_gross - zysk / suma wplat (jak kafelek "Zysk od wplat");
    * xirr     - XIRR od poczatku do dnia (co `step` dni i ostatni dzien; od XIRR_MIN_DAYS), cagr - roczny TWR (od CAGR_MIN_DAYS)."""
    inc = defaultdict(float)
    for d, a in (income or []):
        inc[d] += a
    first = pts[0][0]
    out = {"twr_inc": [1.0], "gain_net": [], "gain_gross": [], "xirr": [], "cagr": []}
    idx = idx_inc = 1.0
    flows, guess = [], 0.1
    for i, (d, v, f, gross) in enumerate(pts):
        if i:
            base = pts[i - 1][1] + f
            if base > 1e-9:
                idx *= v / base; idx_inc *= (v + inc.get(d, 0.0)) / base
            out["twr_inc"].append(idx_inc)
        if abs(f) > 1e-9:
            flows.append((d, -f))
        net = sum(-a for _, a in flows)
        g = v - net
        out["gain_net"].append(g / net if net > 1e-9 else None)
        out["gain_gross"].append(g / gross if gross > 1e-9 else None)
        days = (d - first).days
        out["cagr"].append(annualize(idx - 1.0, days))
        x = None
        if days >= XIRR_MIN_DAYS and (i == len(pts) - 1 or days % step == 0):
            fl = flows + [(d, v)]
            if any(a < 0 for _, a in fl) and any(a > 0 for _, a in fl):
                x = _xirr_newton(fl, guess)
                if x is None or i == len(pts) - 1:
                    x = xirr(fl)
                if x is not None:
                    guess = x
        out["xirr"].append(x)
    return out


def monthly_returns(returns, first, last):
    """Mapa miesiecznych stop zwrotu: dla kazdego roku 12 miesiecy (TWR z dziennych stop dni danego miesiaca; None = miesiac poza historia)
    i wynik roku. Te same dzienne stopy co "Rok do roku", wiec iloczyn miesiecy roku = TWR roku (kontrola w testach)."""
    acc = {}
    for d, r in returns:
        k = (d.year, d.month)
        acc[k] = acc.get(k, 1.0) * (1.0 + r)
    out = []
    for y in range(first.year, last.year + 1):
        months = []
        for m in range(1, 13):
            months.append((acc[(y, m)] - 1.0) if (y, m) in acc else None)
        tot = 1.0
        for v in months:
            if v is not None:
                tot *= 1.0 + v
        out.append({"year": y, "months": months, "total": (tot - 1.0) if any(v is not None for v in months) else None})
    return out


def volatility(returns, min_obs=30):
    xs = [r for d, r in returns if d.weekday() < 5]
    if len(xs) < min_obs:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return sqrt(var) * sqrt(252.0)


def compute(series, income=None, cost_left=None):
    """series: lista dni z build_series; income: [(data, kwota)] dywidendy i odsetki netto (opcjonalnie); cost_left: koszt otwartych pozycji."""
    pts = _points(series)
    if len(pts) < 2:
        return {"available": False, "reason": "Za mało danych do wskaźników (potrzebne co najmniej 2 dni historii)."}
    first, last = pts[0][0], pts[-1][0]
    days = (last - first).days
    v_end, gross_end = pts[-1][1], pts[-1][3]
    invested_net_end = float(series[-1]["invested_net"])
    gain = v_end - invested_net_end
    rets = twr_returns(pts)
    twr_total = chain(rets)
    flows = [(d, -f) for d, v, f, g in pts if abs(f) > 1e-9] + [(last, v_end)]
    xirr_all = xirr(flows) if days >= XIRR_MIN_DAYS else None
    inc = [(d, a) for d, a in (income or []) if first <= d <= last]
    xirr_inc = xirr(flows + [(d, a) for d, a in inc]) if days >= XIRR_MIN_DAYS and inc else None

    # okresy kroczace (od zamkniecia dnia poczatkowego)
    windows = {}
    for key, n in (("1M", 30), ("3M", 91), ("6M", 182), ("12M", 365)):
        start = last - timedelta(days=n)
        windows[key] = chain(rets, start, last) if start >= first else None
    ytd_start = date(last.year - 1, 12, 31)
    windows["YTD"] = chain(rets, ytd_start, last) if ytd_start >= first else chain(rets, None, last)      # gdy historia zaczyna sie w tym roku: od poczatku

    # lata kalendarzowe
    years = []
    for y in range(first.year, last.year + 1):
        start = date(y - 1, 12, 31); end = min(date(y, 12, 31), last)
        s_val = next((v for d, v, f, g in pts if d == start), None) if start >= first else 0.0
        e_val = next((v for d, v, f, g in reversed(pts) if d <= end), 0.0)
        net_flow = sum(f for d, v, f, g in pts if start < d <= end)
        years.append({"year": y, "start_value": s_val or 0.0, "end_value": e_val, "net_contributions": net_flow,
                      "gain": e_val - (s_val or 0.0) - net_flow, "twr": chain(rets, start if start >= first else None, end),
                      "partial": y == first.year or (y == last.year and last < date(y, 12, 31))})

    gains_list = [(_d(p["date"]), float(p["value"]) - float(p["invested_net"])) for p in series]
    vol = volatility(rets)
    # indeks TWR i obsuniecie od szczytu dzien po dniu (wykres "pod woda"), zmiana z ostatniego dnia (bez wplat/wyplat)
    idx, peak, index_series = 1.0, 1.0, [[first.isoformat(), 1.0, 0.0]]
    for d, r in rets:
        idx *= 1.0 + r; peak = max(peak, idx)
        index_series.append([d.isoformat(), idx, idx / peak - 1.0])
    d_prev, v_prev = pts[-2][0], pts[-2][1]
    day_change = v_end - v_prev - pts[-1][2]
    ann_twr = annualize(twr_total, days)
    inc12 = sum(a for d, a in (income or []) if last - timedelta(days=365) < d <= last)
    return {
        "available": True, "first_date": first.isoformat(), "last_date": last.isoformat(), "days": days,
        "value": v_end, "invested_net": invested_net_end, "total_purchases": gross_end,
        "gain": gain, "gain_pct_on_purchases": (gain / gross_end) if gross_end > 1e-9 else None,
        "gain_pct_on_net": (gain / invested_net_end) if invested_net_end > 1e-9 else None,
        "twr_total": twr_total, "twr_annualized": ann_twr, "xirr": xirr_all, "xirr_with_income": xirr_inc,
        "windows": windows, "years": years, "monthly": monthly_returns(rets, first, last), "drawdown": max_drawdown(rets, first), "volatility": vol,
        "drawdown_episodes": drawdown_episodes(rets, dict(gains_list), first), "profit_drawdown": profit_drawdown(gains_list),
        "return_to_risk": (ann_twr / vol) if (ann_twr is not None and vol) else None,
        "income_12m": inc12, "dividend_yield": (inc12 / v_end) if v_end > 1e-9 else None,
        "yield_on_cost": (inc12 / cost_left) if cost_left and cost_left > 1e-9 else None,
        "estimated_days": sum(1 for p in series if p.get("estimated_positions")),
        "index_series": index_series, "return_series": return_series(pts, inc),
        "day_change": day_change, "day_change_pct": rets[-1][1] if rets else None, "day_change_from": d_prev.isoformat(),
    }
