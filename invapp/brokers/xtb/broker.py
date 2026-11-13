"""
Broker XTB: eksporty .xlsx (IKE, IKZE, konto z produktami My Trades / Investment Plans). Nazwa pliku niesie numer konta (ETYKIETA_numer_od_do.xlsx).
Konta zakladaja sie same z nazwy pliku i produktow w pliku; partie wg Position ID (derive.rebuild); waluty instrumentow ze specyfikacji XTB OMI
(pobieranej automatycznie) albo z jawnej reguly uzytkownika (.PL = PLN - pytanie w interfejsie).
"""
import os, tempfile
from datetime import datetime, timezone

from invapp.brokers.base import Broker
from invapp.brokers.xtb import parser as xtb, rules
from invapp.brokers.xtb.importer import XtbImporter
from invapp.core import derive
from invapp.core.common import ImportFailed

MAX_BYTES = 40 * 1024 * 1024
LABEL_PRODUCT = {"IKE": "IKE", "IKZE": "IKZE", "PLN": "My Trades"}      # etykieta w nazwie pliku -> glowny produkt konta
PRODUCTS = {   # etykieta produktu -> (nazwa, product_type, tax_regime)
    "IKE": ("IKE (XTB)", "IKE", "IKE"),
    "IKZE": ("IKZE (XTB)", "IKZE", "IKZE"),
    "My Trades": ("Konto XTB - My Trades", "STANDARD", "PIT38"),
    "Investment Plans": ("Konto XTB - Investment Plans", "STANDARD_SUB", "PIT38"),
}
SCOPES = ("BROKER:XTB", "INVESTMENT_ALL", "NET_WORTH")
OMI_URL = "https://www.xtb.com/int/Specification_Table_Organised_Market_Instruments_OMI.pdf"
SUGGESTED_CURRENCY = {"PL": ("PLN", "GPW (Warszawa)")}                  # pytania do uzytkownika: sufiks -> (waluta, rynek); odpowiedz zapisuje JAWNA regule


# ---------------------------------------------------------------- konta z pliku
def _products_in(pf):
    prods = {xtb.norm_product(r.get("Product")) for r in pf.cash if xtb.norm_product(r.get("Product"))}
    prods |= {xtb.norm_product(r.get("Product")) for r in pf.closed if xtb.norm_product(r.get("Product"))}
    prods |= {xtb.norm_product(r.get("Product")) for r in pf.open_rows if xtb.norm_product(r.get("Product"))}
    prods |= {xtb.norm_product(s["product"]) for s in pf.open_summary if xtb.norm_product(s["product"])}
    return prods


def _plans_account_no(pf):
    """Numer podkonta Planow wynika z komentarzy przelewow 'Transfer from A to B' (A lub B inne niz numer konta z pliku)."""
    for r in pf.cash:
        m = xtb.RX_SUB_TR.match((r.get("Comment") or "").strip())
        if m:
            for n in (m["frm"], m["to"]):
                if n != pf.account_no:
                    return n
    return f"{pf.account_no}-PLANS"


def ensure_accounts(conn, pf):
    """Zaklada konta, aliasy i przypisania do zakresow dla konta z pliku i jego produktow (idempotentnie). Zwraca liste utworzonych kont."""
    cur = conn.cursor(); created = []
    cur.execute("select broker_id from financial_institutions where code = 'XTB'"); broker = cur.fetchone()[0]
    base = LABEL_PRODUCT.get(pf.label)
    prods = _products_in(pf)
    unknown = [p for p in prods if p not in PRODUCTS]
    if unknown:
        raise ImportFailed(f"Nieznany produkt w pliku: {unknown[0]}. Nie zgaduję typu konta - zgłoś ten format.")
    if base is None and not prods:
        raise ImportFailed(f"Nie rozpoznaję typu konta z nazwy pliku ({pf.label}) i nie ma w nim operacji, z których dałoby się to ustalić.")
    wanted = ([base] if base else []) + sorted(p for p in prods if p != base)
    ids = {}
    for prod in wanted:
        display, ptype, tax = PRODUCTS[prod]
        ext_no = _plans_account_no(pf) if prod == "Investment Plans" else pf.account_no
        cur.execute("select account_id from accounts where broker_id = %s and external_no = %s", (broker, ext_no))
        row = cur.fetchone()
        if row:
            ids[prod] = row[0]; continue
        parent = ids.get("My Trades") if prod == "Investment Plans" else None
        if prod == "Investment Plans" and parent is None:
            cur.execute("select account_id from accounts where broker_id = %s and external_no = %s", (broker, pf.account_no)); r = cur.fetchone(); parent = r[0] if r else None
        cur.execute("""insert into accounts(broker_id,external_no,external_no_source,display_name,product_type,account_class,tracking_mode,broker_product_label,tax_regime,parent_account_id)
                       values (%s,%s,%s,%s,%s,'INVESTMENT','FULL',%s,%s,%s) returning account_id""",
                    (broker, ext_no, "FILE", display, ptype, prod, tax, parent))
        ids[prod] = cur.fetchone()[0]; created.append(f"{display} (nr {ext_no})")
        cur.execute("insert into account_identifiers(account_id,scheme,value) values (%s,'BROKER_ACCOUNT_NO',%s) on conflict do nothing", (ids[prod], ext_no))
        for code in SCOPES:
            cur.execute("insert into scope_accounts select scope_id, %s from scopes where code = %s on conflict do nothing", (ids[prod], code))
    main = ids.get(base) or next(iter(ids.values()))
    cur.execute("""insert into account_aliases(broker_id,alias_kind,alias_value,context_account_no,account_id) values (%s,'FILE_ACCOUNT_NO',%s,'',%s) on conflict do nothing""", (broker, pf.account_no, main))
    for prod in wanted:
        cur.execute("""insert into account_aliases(broker_id,alias_kind,alias_value,context_account_no,account_id) values (%s,'PRODUCT_LABEL',%s,%s,%s) on conflict do nothing""", (broker, prod, pf.account_no, ids[prod]))
        if prod == "Investment Plans":
            cur.execute("""insert into account_aliases(broker_id,alias_kind,alias_value,context_account_no,account_id) values (%s,'PRODUCT_LABEL','Investment Plan',%s,%s) on conflict do nothing""", (broker, pf.account_no, ids[prod]))
    return created


# ---------------------------------------------------------------- specyfikacja instrumentow XTB (OMI)
def ensure_omi(conn, pages_provider=None, valid_from=None):
    """Jesli tabela specyfikacji brokera jest pusta - pobiera oficjalna specyfikacje XTB OMI (PDF) i laduje ja ze zrodlem, hashem i data."""
    import urllib.request
    from psycopg2.extras import execute_values
    from invapp.brokers.xtb import omi_parser
    cur = conn.cursor()
    cur.execute("select count(*) from broker_instrument_specs s join financial_institutions f using (broker_id) where f.code = 'XTB'")
    if cur.fetchone()[0] > 0:
        return {"skipped": True}
    if pages_provider is None:
        import shutil
        tmpdir = tempfile.mkdtemp(prefix="invapp_omi_"); tmp = os.path.join(tmpdir, "omi.pdf")
        try:
            req = urllib.request.Request(OMI_URL, headers={"User-Agent": "Mozilla/5.0 investing-app-local"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                f.write(r.read())
            sha = omi_parser.sha256_file(tmp); pages = omi_parser.pdf_pages_text(tmp); name = "Specification_Table_OMI.pdf"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)          # pobrany PDF nie zostaje w katalogu tymczasowym
    else:
        pages, sha, name = pages_provider()
    try:
        rows, unparsed, stats = omi_parser.parse_pages(pages)
    except ValueError as e:
        raise ImportFailed(f"Pobrany dokument nie wygląda na specyfikację instrumentów XTB: {e}")
    if unparsed:
        raise ImportFailed("Nie udało się odczytać całej specyfikacji instrumentów XTB (zmieniony format).")
    cur.execute("select broker_id from financial_institutions where code = 'XTB'"); broker = cur.fetchone()[0]
    execute_values(cur, """insert into broker_instrument_specs(broker_id,symbol,instrument_name,isin,currency,instrument_type,source_doc_name,source_doc_sha256,source_doc_url,valid_from)
                           values %s on conflict do nothing""",
                   [(broker, r["symbol"], r["name"], r["isin"], r["currency"], r["type"], name, sha, OMI_URL, valid_from or datetime.now(timezone.utc).date()) for r in rows], page_size=1000)
    return {"loaded": len(rows), "conflicting": stats["conflicting_symbols"]}


def apply_specs(conn):
    """Notowania XTB utworzone, zanim specyfikacja OMI byla w bazie (np. pierwszy import bez internetu): uzupelnia walute (i ISIN, jesli wolny)
    ze specyfikacji - tak samo jak przy imporcie - oraz walute ceny transakcji 'XXX' z kursem efektywnym. Idempotentne; kwoty bez zmian."""
    from decimal import Decimal
    from psycopg2.extras import Json
    cur = conn.cursor(); res = {"listings_updated": 0, "transactions_updated": 0}
    cur.execute("""select l.listing_id, l.security_id, la.broker_symbol, s.instrument_name, s.isin, s.currency, s.source_doc_name, s.source_doc_sha256, s.valid_from
                   from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                   join financial_institutions f on f.broker_id = la.broker_id and f.code = 'XTB'
                   join lateral (select * from broker_instrument_specs b where b.broker_id = la.broker_id and b.symbol = la.broker_symbol
                                 order by b.valid_from desc, b.spec_id desc limit 1) s on true
                   where l.trading_currency is null""")
    for lst, sec, symbol, spec_name, isin, ccy, doc, sha, valid_from in cur.fetchall():
        src = f"XTB OMI ({doc}) wazna od {valid_from}, sha256 {sha[:8]}"
        cur.execute("""update listings set trading_currency=%s, currency_status='ZGODNY_SPECYFIKACJA_BROKERA', currency_source=%s, currency_checked_at=now(), currency_evidence=%s
                       where listing_id=%s and trading_currency is null""", (ccy, src, Json({"source": src, "symbol": symbol, "spec_currency": ccy, "applied": "after_import"}), lst))
        res["listings_updated"] += 1
        cur.execute("select 1 from securities where isin=%s", (isin,))
        if isin and not cur.fetchone():
            cur.execute("""update securities set isin=%s, isin_status='ZGODNY_SPECYFIKACJA_BROKERA', isin_evidence=%s where security_id=%s and isin is null""",
                        (isin, Json({"source": f"XTB OMI {doc}", "sha256": sha, "valid_from": str(valid_from), "spec_name": spec_name, "symbol": symbol}), sec))
        cur.execute("""select txn_id, details->>'effective_cash_per_price_unit' from transactions where listing_id=%s and txn_type in ('BUY','SELL') and price_currency='XXX'""", (lst,))
        for tid, eff in cur.fetchall():
            cur.execute("""update transactions set price_currency=%s, fx_rate=%s, fx_rate_kind=%s where txn_id=%s""",
                        (ccy, Decimal(eff).quantize(Decimal("0.0000000001")) if eff else None, "EFFECTIVE_IMPLIED" if eff else None, tid))
            res["transactions_updated"] += 1
    return res


class Xtb(Broker):
    code, key, name = "XTB", "xtb", "XTB"
    accept = (".xlsx",)
    free_funds = True                                           # saldo gotowki kont XTB = wolne srodki (zakladka Gotowka)
    omi_loader = staticmethod(ensure_omi)                     # podmieniane w testach (bez pobierania z internetu)
    file_help = ("<strong>XTB</strong>: pliki <strong>.xlsx</strong> pobrane z XTB (IKE, IKZE, konto z planami i inwestycjami) – "
                 "nie zmieniaj ich nazw, numer konta jest w nazwie pliku.")

    def matches(self, filename, head):
        return filename.lower().endswith(".xlsx") and bool(xtb.FILE_RE.match(os.path.basename(filename)))

    def validate_upload(self, filename, data):
        if not xtb.FILE_RE.match(filename):
            raise ImportFailed("Nazwa pliku nie ma formatu eksportu XTB (ETYKIETA_numer-konta_data-od_data-do.xlsx). Nie zmieniaj nazwy pliku po pobraniu z XTB.")
        if len(data) > MAX_BYTES or len(data) < 1000:
            raise ImportFailed("Plik ma nieprawidłowy rozmiar.")
        if data[:2] != b"PK":
            raise ImportFailed("To nie jest prawidłowy plik .xlsx.")

    def import_path(self, conn, path, display_name=None):
        pf = xtb.parse(path)
        omi_note = None
        cur = conn.cursor()
        # Specyfikacja OMI (waluty instrumentow) NIE jest pobierana w trakcie importu: pobranie i odczyt PDF trwa ~35 s i blokowal pierwszy import.
        # Gdy jej jeszcze nie ma, pobiera ja zadanie w tle, a apply_specs uzupelnia waluty - liczby sa takie same jak przy imporcie ze specyfikacja (test).
        cur.execute("select exists (select 1 from broker_instrument_specs s join financial_institutions f using (broker_id) where f.code = 'XTB')")
        if not cur.fetchone()[0]:
            omi_note = "Specyfikacja instrumentów XTB nie została jeszcze pobrana; waluty instrumentów zagranicznych uzupełnią się automatycznie w tle."
        created = ensure_accounts(conn, pf)
        rep = XtbImporter(conn).import_file(path, pf)            # ten sam odczyt pliku (arkusz czytany raz)
        rep["rules"] = rules.apply(conn)                       # jawne reguly uzytkownika (np. .PL = PLN) takze dla nowych notowan
        rep["derived"] = self.rebuild_derived(conn)
        rep["created_accounts"] = created
        if omi_note:
            rep.setdefault("info", []).append(omi_note)
        if display_name:
            rep["file"] = display_name
        return rep

    def rebuild_derived(self, conn):
        return derive.rebuild(conn, "XTB")

    def relink(self, conn, report):
        imp = XtbImporter(conn)
        imp.link_wht(report); imp.link_sec_corrections(report); imp.group_orders(report); imp.link_transfers(report)
        imp.classify_flows(report); imp.detect_corporate_actions(report)

    def pricing_steps(self, conn, provider):
        """Kroki szybkie (przed pierwszym pobraniem notowan - uzytkownik szybko widzi wartosci) i wolne (specyfikacja PDF, historia)."""
        from invapp.pricing import history, service
        cur = conn.cursor()
        cur.execute("select exists (select 1 from import_batches b join financial_institutions f using (broker_id) where f.code = 'XTB')")
        if not cur.fetchone()[0]:
            return []                                          # brak plikow XTB - nie pobieramy specyfikacji XTB (PDF ~35 s) na darmo
        verify = lambda: [r["status"] for r in service.verify_mappings(conn, provider, broker_code="XTB")]  # noqa: E731
        return [
            ("Symbole XTB u dostawcy cen (weryfikacja)", verify, "quick"),
            ("Specyfikacja instrumentów XTB (OMI)", lambda: ensure_omi(conn), "slow"),
            ("Waluty instrumentów XTB ze specyfikacji", lambda: apply_specs(conn), "slow"),
            ("Symbole XTB u dostawcy cen (nowe waluty)", verify, "slow"),
            ("Symbole historii XTB (weryfikacja)", lambda: [r["status"] for r in history.verify_history_mappings(conn, history.HistoryYahoo(), broker_code="XTB")], "history"),
        ]

    # ---- pytania: waluta notowan bez specyfikacji (np. .PL) oraz splity niewidoczne w eksporcie
    def _split_hypotheses(self, conn):
        cur = conn.cursor()
        cur.execute("""select a.action_id, la.broker_symbol, a.ratio_new, a.applies_to_lots_opened_before, a.evidence
                       from corporate_actions a join listing_aliases la on la.listing_id = a.listing_id and la.valid_to is null
                       join financial_institutions f on f.broker_id = la.broker_id
                       where f.code = 'XTB' and a.action_type = 'SPLIT' and a.status = 'HYPOTHESIS' order by a.action_id""")
        return cur.fetchall()

    def pending_questions(self, conn):
        out = self._currency_questions(conn)
        for aid, symbol, k, cutoff, ev in self._split_hypotheses(conn):
            n = len((ev or {}).get("positions", []))
            k = int(k) if k == int(k) else k
            last = cutoff.date().isoformat() if cutoff else None
            out.append({"id": f"xtb-split-{aid}", "kategoria": "split", "tytul": f"Podział akcji {symbol} {k}:1?",
                        "tresc": f"W danych XTB zamknięto {k}× więcej akcji {symbol}, niż kupiono ({n} poz.). Wygląda to na podział akcji (split) {k}:1, "
                                 f"którego XTB nie pokazał w eksporcie. Bez potwierdzenia aplikacja nie stosuje korekty, a kontrola liczb pokazuje ujemny stan. "
                                 f"Sprawdź komunikat spółki (raport ESPI/EBI) albo KDPW i podaj dzień podziału" + (f" (najpóźniej {last})." if last else "."),
                        "pola": [{"nazwa": "data", "typ": "date", "etykieta": "Dzień podziału", "max": last},
                                 {"nazwa": "zrodlo", "typ": "text", "etykieta": "Źródło (np. raport ESPI nr …)"}],
                        "odpowiedzi": [{"wartosc": "auto", "etykieta": "Zdecyduj za mnie", "bez_pol": True,
                                        "opis": "Aplikacja przyjmie split: dzień podziału z danych Yahoo (jeśli są), a gdy ich brak – granica z Twoich transakcji. "
                                                "Oznaczy go jako niesprawdzony oficjalnym źródłem."},
                                       {"wartosc": "tak", "etykieta": f"Potwierdzam split {k}:1 (podaję dzień i źródło)"},
                                       {"wartosc": "nie", "etykieta": "To nie był split", "bez_pol": True,
                                        "potwierdz": "Na pewno to nie był podział akcji? Ujemny stan zostanie, a kontrola liczb pokaże uwagę. "
                                                     "Decyzję zmienisz później przyciskiem w „Kontroli liczb”."}]})
        return out

    split_provider = None                                     # podmieniane w testach (bez internetu)

    def _auto_split(self, conn, aid, symbol, k, cutoff, ev):
        """Decyzja za uzytkownika: split CONFIRMED (niezweryfikowany oficjalnie). Dzien podzialu z Yahoo (zdarzenia korporacyjne), jesli Yahoo zna
        split o tym samym wspolczynniku najpozniej w dniu granicy z transakcji; inaczej granica z transakcji (pierwsza sprzedaz po podziale)."""
        from decimal import Decimal
        from psycopg2.extras import Json
        from invapp.pricing.providers import YahooProvider
        cur = conn.cursor()
        cur.execute("""select coalesce((select m.provider_symbol from listing_provider_symbols m join corporate_actions a on a.listing_id = m.listing_id
                                        where a.action_id = %s and m.status = 'VERIFIED' order by m.provider limit 1), '')""", (aid,))
        prov = self.split_provider or YahooProvider()
        sym = cur.fetchone()[0] or prov.symbol_for(symbol)
        # ostatni zakup pozycji, ktore zamknieto w ilosci k x kupiona (kupione PRZED podzialem): dzien podzialu musi byc pozniej, inaczej korekta ich nie obejmie
        refs = [p.get("position") for p in (ev or {}).get("positions", []) if p.get("position")]
        cur.execute("select max(occurred_at) from transactions where txn_type = 'BUY' and broker_position_ref = any(%s)", (refs,))
        last_buy = cur.fetchone()[0]
        found, note = None, None
        if sym:
            try:
                for d, num, den in prov.splits(sym):
                    if den and (num / den) == Decimal(k) and (cutoff is None or d <= cutoff.date()) and (last_buy is None or d > last_buy.date()):
                        found = d
            except Exception as e:  # noqa: BLE001
                note = f"Yahoo niedostępne ({type(e).__name__})"
        now = datetime.now(timezone.utc)
        hist = list((ev or {}).get("history", [])) + [{"at": now.isoformat(), "from": "HYPOTHESIS", "to": "CONFIRMED", "by": "użytkownik: zdecyduj za mnie",
                                                     "split_date_source": f"Yahoo Finance ({sym})" if found else "granica z transakcji", "note": note}]
        if found:
            cur.execute("""update corporate_actions set status='CONFIRMED', source='EXTERNAL', effective_date=%s, effective_date_status='HYPOTHESIS',
                                  applies_to_lots_opened_before=NULL, evidence = evidence || %s where action_id=%s""",
                        (found, Json({"history": hist, "auto_decision": {"source": f"Yahoo Finance zdarzenia ({sym})", "date": found.isoformat()}}), aid))
        else:
            cur.execute("""update corporate_actions set status='CONFIRMED', evidence = evidence || %s where action_id=%s""",
                        (Json({"history": hist, "auto_decision": {"source": "granica z transakcji (pierwsza sprzedaż po podziale)"}}), aid))
        cur.execute("select listing_id from corporate_actions where action_id = %s", (aid,))
        cur.execute("""update data_quality_issues set status='RESOLVED', resolved_at=now() where code='DQ-06_IMPLICIT_SPLIT' and entity_ref=%s and status='OPEN'""",
                    (f"listing:{cur.fetchone()[0]}",))
        self.rebuild_derived(conn)
        return {"zapisano": True, "split": True, "dzien": found.isoformat() if found else None,
                "zrodlo_daty": f"Yahoo Finance ({sym})" if found else "transakcje"}

    def _answer_split(self, conn, aid, answer, fields):
        from datetime import date
        from psycopg2.extras import Json
        from invapp.tools.confirm_corporate_action import confirm
        hyp = next((h for h in self._split_hypotheses(conn) if h[0] == aid), None)
        if hyp is None:
            raise ImportFailed("Ta korekta została już rozstrzygnięta.")
        _, symbol, k, cutoff, ev = hyp
        if answer == "nie":
            hist = list((ev or {}).get("history", [])) + [{"at": datetime.now(timezone.utc).isoformat(), "from": "HYPOTHESIS", "to": "REJECTED", "by": "użytkownik (aplikacja)"}]
            cur = conn.cursor()
            cur.execute("update corporate_actions set status='REJECTED', evidence = evidence || %s where action_id=%s", (Json({"history": hist}), aid))
            return {"zapisano": True, "split": False}
        if answer == "auto":
            return self._auto_split(conn, aid, symbol, k, cutoff, ev)
        if answer != "tak":
            raise ImportFailed("Nieznana odpowiedź.")
        try:
            d = date.fromisoformat((fields or {}).get("data", ""))
        except ValueError:
            raise ImportFailed("Podaj dzień podziału (z komunikatu spółki).")
        if cutoff and d > cutoff.date():
            raise ImportFailed(f"Dzień podziału nie może być późniejszy niż pierwsza sprzedaż po podziale ({cutoff.date()}).")
        ref = ((fields or {}).get("zrodlo") or "").strip()[:200]
        if not ref:
            raise ImportFailed("Podaj źródło (np. numer raportu ESPI), żeby było wiadomo, skąd pochodzi potwierdzenie.")
        confirm(conn, symbol, k, d.isoformat(), ref, None, "potwierdzone przez użytkownika w aplikacji")
        return {"zapisano": True, "split": True}

    def _currency_questions(self, conn):
        cur = conn.cursor(); out = []
        for suffix, (ccy, market) in SUGGESTED_CURRENCY.items():
            cur.execute("""select count(*) from listings l join listing_aliases la on la.listing_id = l.listing_id and la.valid_to is null
                           join financial_institutions f on f.broker_id = la.broker_id
                           where f.code = 'XTB' and la.broker_symbol like %s and l.trading_currency is null""", ("%." + suffix,))
            n = cur.fetchone()[0]
            cur.execute("""select 1 from broker_symbol_suffix_rules r join financial_institutions f using (broker_id) where f.code='XTB' and r.symbol_suffix=%s""", (suffix,))
            if n and not cur.fetchone():
                out.append({"id": f"xtb-currency-{suffix}", "kategoria": "waluta", "tytul": f"Waluta instrumentów .{suffix} ({market})",
                            "tresc": f"W Twoich plikach XTB jest {n} instrumentów z końcówką .{suffix}, których waluty nie podaje specyfikacja XTB. "
                                     f"Czy instrumenty .{suffix} są notowane w {ccy}? Bez tej informacji nie da się ich wycenić cenami rynkowymi "
                                     f"(aplikacja nie zgaduje waluty z końcówki symbolu).",
                            "odpowiedzi": [{"wartosc": "tak", "etykieta": f"Tak, w {ccy}"}, {"wartosc": "nie", "etykieta": "Nie wiem / nie"}]})
        return out

    # ---- rozwiazanie ujemnego stanu z kontroli liczb (decyzja uzytkownika 2026-11-12: "ma byc opcja resolve - aplikacja zaklada split
    #      i przelicza tak, zeby sie wszystko zgadzalo"): split wynikajacy z danych, takze gdy wczesniej odpowiedziano "to nie byl split"
    def split_evidence(self, cur, listing_id):
        """Pozycje XTB (Position ID) tego notowania zamkniete w ilosci k x kupiona: {k, cutoff, positions}, gdy wszystkie takie pozycje
        maja ten sam calkowity iloraz k >= 2 (jak przy tworzeniu hipotezy w imporcie); inaczej None (dane nie wskazuja splitu)."""
        from decimal import Decimal
        cur.execute("""select b.broker_position_ref, b.volume, b.closed_at,
                              (select sum(t.quantity) from transactions t where t.txn_type = 'BUY' and t.status = 'ACTIVE'
                                 and t.account_id = b.account_id and t.broker_position_ref = b.broker_position_ref)
                       from broker_closed_positions b where b.listing_id = %s and b.close_origin is distinct from 'Correction'""", (listing_id,))
        items = []
        for ref, vol, closed, bought in cur.fetchall():
            if bought and bought > 0:
                r = Decimal(vol) / Decimal(bought)
                if r >= 2 and abs(r - r.to_integral_value()) < Decimal("0.0001"):
                    items.append((int(r.to_integral_value()), ref, vol, bought, closed))
        ks = {i[0] for i in items}
        if len(ks) != 1:
            return None
        return {"k": ks.pop(), "cutoff": min(i[4] for i in items),
                "positions": [{"position": str(i[1]), "closed_volume": str(i[2]), "bought_quantity": str(i[3])} for i in items]}

    def negative_state_fix(self, cur, listing_id):
        """Dla diagnozy kontroli liczb: propozycja przyjecia splitu wynikajacego z danych (jedno klikniecie) albo None."""
        ev = self.split_evidence(cur, listing_id)
        if not ev:
            return None
        cur.execute("""select status from corporate_actions where listing_id = %s and action_type = 'SPLIT' and ratio_new = %s and ratio_old = 1
                       order by action_id desc limit 1""", (listing_id, ev["k"]))
        r = cur.fetchone()
        if r and r[0] == "CONFIRMED":
            return None
        return {"broker": self.key, "pytanie": f"xtb-resolve-split-{listing_id}", "odpowiedz": "auto", "k": ev["k"], "pozycje": len(ev["positions"]),
                "odrzucony": bool(r and r[0] == "REJECTED")}

    def _resolve_split(self, conn, listing_id):
        from psycopg2.extras import Json
        cur = conn.cursor()
        ev = self.split_evidence(cur, listing_id)
        if not ev:
            raise ImportFailed("Dane nie wskazują jednoznacznie podziału akcji – najpewniej brakuje pliku eksportu ze starszymi zakupami.")
        k = ev["k"]
        cur.execute("""select action_id, status from corporate_actions where listing_id = %s and action_type = 'SPLIT' and ratio_new = %s and ratio_old = 1
                       order by action_id desc limit 1""", (listing_id, k))
        row = cur.fetchone()
        now = datetime.now(timezone.utc).isoformat()
        if row and row[1] == "CONFIRMED":
            raise ImportFailed("Ten podział akcji jest już przyjęty.")
        if row:                                                   # hipoteza z importu albo wczesniej odrzucona - wraca jako hipoteza, potem decyzja "za mnie"
            aid, status = row
            cur.execute("select evidence from corporate_actions where action_id = %s", (aid,))
            evd = cur.fetchone()[0] or {}
            hist = list(evd.get("history", [])) + [{"at": now, "from": status, "to": "HYPOTHESIS",
                                                   "by": "użytkownik: rozwiąż z kontroli liczb" + (" (zmiana wcześniejszej decyzji „to nie był split”)" if status == "REJECTED" else "")}]
            cur.execute("""update corporate_actions set status = 'HYPOTHESIS', applies_to_lots_opened_before = coalesce(applies_to_lots_opened_before, %s),
                                  evidence = evidence || %s where action_id = %s""", (ev["cutoff"], Json({"history": hist}), aid))
        else:
            cur.execute("""insert into corporate_actions(listing_id,action_type,effective_date,effective_date_status,applies_to_lots_opened_before,ratio_new,ratio_old,source,status,evidence)
                           values (%s,'SPLIT',NULL,'UNKNOWN',%s,%s,1,'INFERRED','HYPOTHESIS',%s) returning action_id""",
                        (listing_id, ev["cutoff"], k, Json({"pattern": "ilość zamknięta = k × ilość kupiona (wg pozycji XTB)", "ratio": k, "positions": ev["positions"],
                                                             "history": [{"at": now, "to": "HYPOTHESIS", "by": "użytkownik: rozwiąż z kontroli liczb"}]})))
            aid = cur.fetchone()[0]
        cur.execute("select min(broker_symbol) from listing_aliases where listing_id = %s and valid_to is null", (listing_id,))
        symbol = cur.fetchone()[0]
        cur.execute("select applies_to_lots_opened_before, evidence from corporate_actions where action_id = %s", (aid,))
        cutoff, evd = cur.fetchone()
        return self._auto_split(conn, aid, symbol, k, cutoff, evd)

    def answer_question(self, conn, question_id, answer, fields=None):
        if question_id.startswith("xtb-resolve-split-"):
            try:
                listing_id = int(question_id.rsplit("-", 1)[-1])
            except ValueError:
                raise ImportFailed("Nieznane pytanie.")
            if answer != "auto":
                raise ImportFailed("Nieznana odpowiedź.")
            return self._resolve_split(conn, listing_id)
        if question_id.startswith("xtb-split-"):
            try:
                aid = int(question_id.rsplit("-", 1)[-1])
            except ValueError:
                raise ImportFailed("Nieznane pytanie.")
            return self._answer_split(conn, aid, answer, fields)
        if not question_id.startswith("xtb-currency-"):
            raise ImportFailed("Nieznane pytanie.")
        suffix = question_id.rsplit("-", 1)[-1]
        if suffix not in SUGGESTED_CURRENCY:
            raise ImportFailed("Nieznane pytanie.")
        if answer != "tak":
            return {"zapisano": False}
        ccy = SUGGESTED_CURRENCY[suffix][0]
        cur = conn.cursor()
        cur.execute("select broker_id from financial_institutions where code = 'XTB'"); broker = cur.fetchone()[0]
        cur.execute("""insert into broker_symbol_suffix_rules(broker_id,symbol_suffix,value,source,confirmed_at) values (%s,%s,%s,%s,now())
                       on conflict do nothing""",
                    (broker, suffix, ccy, f"Potwierdzenie użytkownika {datetime.now():%Y-%m-%d}: instrumenty .{suffix} są notowane w {ccy}"))
        res = rules.apply(conn)
        self.rebuild_derived(conn)
        return {"zapisano": True, **res}
