"""
Eksport do Excela (decyzja uzytkownika 2026-11-12): te same liczby co na stronie, w arkuszach: Podsumowanie, Pozycje, Historia, Dochody, Wskazniki, Gotowka.
Plik powstaje w pamieci i jest wysylany do okna aplikacji ("Zapisz jako") - aplikacja nie zapisuje go nigdzie sama i nigdzie go nie wysyla.
"""
import io
from datetime import date, datetime
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from invapp import APP_NAME, __version__
from . import dashboard

PLN = '#,##0.00 "zł"'
PCT = '0.00%'
QTY = '#,##0.########'
HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="2563EB")


def _num(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _sheet(wb, title, headers, rows, formats=None, widths=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font, c.fill, c.alignment = HEAD, FILL, Alignment(vertical="center")
    for r in rows:
        ws.append([_num(v) for v in r])
    for i, fmt in (formats or {}).items():
        for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
            for c in row:
                c.number_format = fmt
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(12, min(40, len(str(h)) + 4)))
    ws.freeze_panes = "A2"
    return ws


def build(scope_key="all"):
    scope_key = dashboard.normalize_scope(scope_key)
    title = dashboard.SCOPES[scope_key][1]
    live = dashboard.live_data(scope_key)
    s = live.get("scope") or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Podsumowanie"
    ws.append([f"{APP_NAME} – {title}"]); ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Stan na {datetime.now().strftime('%Y-%m-%d %H:%M')} · wersja {__version__} · kwoty w PLN"])
    ws.append([])
    rows = [("Wartość portfela", s.get("total_value", s.get("assets_value")), PLN), ("w tym papiery", s.get("securities_value"), PLN),
            ("w tym lokaty", s.get("deposits_value"), PLN), ("w tym gotówka z banków (gdy włączona)", s.get("cash_value"), PLN),
            ("w tym wolne środki XTB (gdy włączone)", s.get("xtb_free_value"), PLN), ("Zysk całkowity", s.get("total_result_live"), PLN),
            ("Zysk z inwestycji", s.get("investment_gain"), PLN), ("niezrealizowany", s.get("unrealized_gain"), PLN), ("zrealizowany", s.get("realized_gain"), PLN),
            ("Dywidendy, odsetki i inne (netto)", s.get("additional_income"), PLN), ("Koszt otwartych pozycji", s.get("securities_cost"), PLN)]
    for label, v, fmt in rows:
        ws.append([label, _num(v)])
        ws.cell(ws.max_row, 2).number_format = fmt
    st = s.get("settings") or {}
    ws.append([])
    ws.append(["Źródło cen", "eksport XTB" if st.get("price_source") == "XTB_EXPORT" else "Yahoo Finance (na żywo)"])
    ws.append(["Kursy walut", "NBP" if st.get("fx_source") == "NBP" else "rynkowe na żywo (Yahoo)"])
    ws.column_dimensions["A"].width = 42; ws.column_dimensions["B"].width = 22

    pos = []
    for r in live.get("positions") or []:
        gain = (r["value"] - r["cost_left"]) if r.get("value") is not None and r.get("cost_left") is not None else None
        pos.append([r.get("ticker"), r.get("instrument"), ", ".join(sorted({a["account"] for a in r.get("accounts") or []})), r.get("qty"), r.get("price_pln"),
                    r.get("currency"), r.get("value"), r.get("cost_left"), gain, (gain / r["cost_left"]) if gain is not None and r.get("cost_left") else None,
                    {"MARKET": "Yahoo", "XTB_EXPORT": "eksport XTB", "XTB_REPORT": "raport XTB (awaryjnie)"}.get(r.get("price_source"), "brak")])
    for d in live.get("deposits") or []:
        pos.append(["Lokata", d.get("account"), d.get("broker"), None, None, "PLN", d.get("value"), None, None, None, f"saldo z eksportu {d.get('ledger_as_of')}"])
    _sheet(wb, "Pozycje", ["Symbol", "Instrument", "Konto", "Ilość", "Cena (PLN)", "Waluta notowania", "Wartość", "Koszt", "Zysk", "Zysk %", "Źródło ceny"], pos,
           {4: QTY, 5: PLN, 7: PLN, 8: PLN, 9: PLN, 10: PCT}, {2: 40, 3: 24})

    h = dashboard.history_data(scope_key)
    m = dashboard.metrics_data(scope_key)
    idx = {d: i for d, i, _ in (m.get("index_series") or [])}
    _sheet(wb, "Historia", ["Data", "Wartość", "Papiery", "Lokaty", "Wpłaty netto", "Zysk", "Zwrot TWR od początku", "Pozycji", "Pozycji z oszacowaną ceną"],
           [[date.fromisoformat(p["date"]), p["value"], p["securities"], p["deposits"], p["invested_net"], p["gain"],
             (idx[p["date"]] - 1) if p["date"] in idx else None, p["positions"], p["estimated_positions"]] for p in h.get("series") or []],
           {2: PLN, 3: PLN, 4: PLN, 5: PLN, 6: PLN, 7: PCT}).column_dimensions["A"].width = 12

    inc = dashboard.income_monthly(scope_key)
    _sheet(wb, "Dochody", ["Miesiąc", "Dywidendy netto", "Odsetki netto", "Nagrody", "Opłaty"],
           [[r["month"], r["dividends"], r["interest"], r["rewards"], r["fees"]] for r in inc.get("months") or []], {2: PLN, 3: PLN, 4: PLN, 5: PLN})

    if m.get("available"):
        mrows = [("Zysk od wpłat", m.get("gain_pct_on_purchases"), PCT), ("Zwrot TWR od początku", m.get("twr_total"), PCT), ("TWR rocznie (CAGR)", m.get("twr_annualized"), PCT),
                 ("XIRR rocznie", m.get("xirr"), PCT), ("XIRR z dywidendami", m.get("xirr_with_income"), PCT), ("Maks. obsunięcie", (m.get("drawdown") or {}).get("depth"), PCT),
                 ("Zmienność roczna", m.get("volatility"), PCT), ("Stopa dywidendy (12 m)", m.get("dividend_yield"), PCT)]
        mrows += [(f"Zwrot {k}", v, PCT) for k, v in (m.get("windows") or {}).items()]
        ws2 = _sheet(wb, "Wskaźniki", ["Wskaźnik", "Wartość"], [(a, b) for a, b, _ in mrows], {2: PCT}, {1: 30})
        ws2.append([]); ws2.append(["Rok", "Początek", "Wpłaty netto", "Koniec", "Zysk", "TWR"])
        for c in ws2[ws2.max_row]:
            c.font = Font(bold=True)
        for y in m.get("years") or []:
            ws2.append([y["year"], y["start_value"], y["net_contributions"], y["end_value"], y["gain"], y["twr"]])
            for col, fmt in ((2, PLN), (3, PLN), (4, PLN), (5, PLN), (6, PCT)):
                ws2.cell(ws2.max_row, col).number_format = fmt

    try:
        c = dashboard.cash_data()
        _sheet(wb, "Gotówka", ["Konto", "Rodzaj", "Waluta", "Saldo", "Na dzień", "Wartość w PLN"],
               [[a["nazwa"], "konto bankowe", a["waluta"], a["saldo"], a["na_dzien"], a["wartosc_pln"]] for a in c["konta"]]
               + [[w["konto"], "XTB – wolne środki", "PLN", w["saldo"], None, w["saldo"]] for w in c["wolne_xtb"]], {4: '#,##0.00', 6: PLN}, {1: 30})
    except Exception:  # noqa: BLE001 - baza bez tabel gotowki
        pass

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"MojPortfel_{scope_key}_{date.today().isoformat()}.xlsx"
