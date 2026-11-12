"""
Diagnoza uwag z kontroli liczb (decyzja uzytkownika 2026-11-12: "jak cos jest do sprawdzenia, aplikacja powinna domyslic sie powodu i zaproponowac
rozwiazanie - nie dodajac ani nie odejmujac nic sztucznie").

Dla kazdej kontroli ze statusem innym niz OK dopisuje:
  przyczyna   - najbardziej prawdopodobny powod, wyznaczony z danych (waluta niepotwierdzona, odrzucony symbol u dostawcy, split niewidoczny w eksporcie,
                brakujacy plik, zmiana oprocentowania ...),
  rozwiazania - akcje do klikniecia: odpowiedz na pytanie (np. "Zdecyduj za mnie" przy splicie), pokaz pytanie, ponow pobieranie cen,
                wczytaj brakujacy eksport, przelicz wszystko od nowa (deterministycznie z danych z eksportow).
Rozwiazania korzystaja wylacznie z istniejacych, opisanych mechanizmow (pytania brokerow, pobieranie cen, przeliczenie partii) - zadna kwota
nie jest korygowana recznie. Tylko odczyt.
"""
from decimal import Decimal as D

ANSWER, SHOW, PRICES, REBUILD, IMPORT = "odpowiedz", "pytanie", "ceny", "przelicz", "import"


def _act(kind, label, **kw):
    return {"akcja": kind, "etykieta": label, **kw}


def _questions_by_prefix(questions, prefix):
    return [q for q in questions if q["id"].startswith(prefix)]


def _split_actions(questions, listing_symbols=None):
    out = []
    for q in _questions_by_prefix(questions, "xtb-split-"):
        if listing_symbols and not any(s in q["tytul"] for s in listing_symbols):
            continue
        sym = q["tytul"].replace("Podział akcji ", "").rstrip("?")
        out.append(_act(ANSWER, f"Zdecyduj za mnie: split {sym}", broker=q["broker"], pytanie=q["id"], odpowiedz="auto"))
        out.append(_act(SHOW, "Podam dzień i źródło sam", pytanie=q["id"]))
    return out


def _missing_prices(cur, check, questions, accounts):
    cur.execute("""select dl.listing_id, (select min(la.broker_symbol) from listing_aliases la where la.listing_id = dl.listing_id and la.valid_to is null),
                          l.trading_currency,
                          (select m.status from listing_provider_symbols m where m.listing_id = dl.listing_id and m.provider = 'YAHOO'),
                          (select m.evidence->>'reason' from listing_provider_symbols m where m.listing_id = dl.listing_id and m.provider = 'YAHOO'),
                          exists (select 1 from market_quotes q where q.listing_id = dl.listing_id)
                   from derived_lots dl join listings l using (listing_id)
                   where dl.account_id = any(%s) group by dl.listing_id, l.trading_currency having sum(dl.qty_remaining) > 0.00000001""", (accounts,))
    reasons, acts, no_ccy, rejected, waiting = [], [], [], [], []
    for lid, sym, ccy, status, why, has_q in cur.fetchall():
        if has_q and status == "VERIFIED":
            continue
        if ccy is None:
            no_ccy.append(sym)
        elif status in ("REJECTED", "PENDING"):
            rejected.append(f"{sym} ({why or 'symbol nieznany u dostawcy'})")
        else:
            waiting.append(sym)
    if no_ccy:
        reasons.append(f"Waluta notowań nie jest potwierdzona dla: {', '.join(no_ccy)} – bez niej aplikacja nie pobiera cen (nie zgaduje waluty z końcówki symbolu).")
        for q in _questions_by_prefix(questions, "xtb-currency-"):
            yes = next((o for o in q["odpowiedzi"] if o["wartosc"] == "tak"), None)
            if yes:
                acts.append(_act(ANSWER, f"{yes['etykieta']} – pobierz ceny", broker=q["broker"], pytanie=q["id"], odpowiedz="tak"))
    if rejected:
        reasons.append("Dostawca cen nie potwierdził symbolu: " + "; ".join(rejected) + ". Do czasu potwierdzenia pozycja jest wyceniana wartością z raportu XTB.")
    if waiting:
        reasons.append(f"Ceny jeszcze się nie pobrały (albo brak internetu): {', '.join(waiting)}.")
    if rejected or waiting or not acts:
        acts.append(_act(PRICES, "Pobierz ceny ponownie"))
    return " ".join(reasons) or "Część pozycji nie ma jeszcze ceny rynkowej.", acts


def _broker_fixes(cur, listing_id):
    """Propozycje brokerow dla ujemnego stanu notowania (np. XTB: split wynikajacy z pozycji, takze wczesniej odrzucony)."""
    from invapp import brokers
    out = []
    for b in brokers.BROKERS:
        fn = getattr(b, "negative_state_fix", None)
        if fn:
            fix = fn(cur, listing_id)
            if fix:
                out.append(fix)
    return out


def _negative(cur, questions, accounts):
    cur.execute("""select x.listing_id, (select min(la.broker_symbol) from listing_aliases la where la.listing_id = x.listing_id and la.valid_to is null), x.b, coalesce(y.s, 0)
                   from (select account_id, listing_id, sum(qty_initial) b from derived_lots where account_id = any(%s) group by 1, 2) x
                   left join (select account_id, listing_id, sum(quantity) s from transactions where txn_type = 'SELL' and status = 'ACTIVE' group by 1, 2) y using (account_id, listing_id)
                   where x.b - coalesce(y.s, 0) < -0.0000001""", (accounts,))
    rows = cur.fetchall()
    syms = sorted({r[1] for r in rows if r[1]})
    acts = _split_actions(questions, syms)
    if acts:
        return (f"Sprzedano więcej {', '.join(syms)}, niż kupiono – iloraz ilości wskazuje na podział akcji (split), którego broker nie pokazał w eksporcie. "
                "Po potwierdzeniu splitu ilości z zakupów zostaną przeliczone (bez zmiany zapłaconych kwot)."), acts
    reasons, rest = [], []
    for lid, sym, bought, sold in rows:
        fixes = _broker_fixes(cur, lid)
        if not fixes:
            rest.append(sym)
            continue
        f = fixes[0]
        reasons.append(f"{sym}: w {f['pozycje']} poz. sprzedano dokładnie {f['k']}× więcej akcji, niż kupiono – to podział akcji (split) {f['k']}:1, którego broker "
                       f"nie pokazał w eksporcie" + (" (wcześniej oznaczony jako „to nie był split”)" if f["odrzucony"] else "") +
                       ". Po przyjęciu splitu ilości z zakupów zostaną przeliczone ×" + str(f["k"]) + " (cena ÷" + str(f["k"]) + "); zapłacone kwoty i koszt się nie zmieniają.")
        acts.append(_act(ANSWER, f"Przyjmij split {sym} {f['k']}:1 i przelicz", broker=f["broker"], pytanie=f["pytanie"], odpowiedz=f["odpowiedz"]))
    if rest:
        reasons.append(f"Sprzedaż {', '.join(rest)} bez odpowiadającego zakupu w danych i bez śladu podziału akcji – zwykle brakuje starszego pliku eksportu "
                       "(innego konta albo okresu). Wczytaj eksport obejmujący cały okres od pierwszego zakupu.")
        acts.append(_act(IMPORT, "Wczytaj brakujący eksport"))
    return " ".join(reasons), acts


def _qty_vs_report(cur, questions, accounts):
    cur.execute("""select (select min(la.broker_symbol) from listing_aliases la where la.listing_id = sn.listing_id and la.valid_to is null), coalesce(dl.qty, 0), sn.vol
                   from (select s.account_id, s.listing_id, s.volume vol from broker_position_snapshots s join
                           (select account_id, max(as_of) mx from broker_position_snapshots where row_level = 'INSTRUMENT' group by 1) m on m.account_id = s.account_id and m.mx = s.as_of
                         where s.row_level = 'INSTRUMENT' and s.account_id = any(%s)) sn
                   left join (select account_id, listing_id, sum(qty_remaining) qty from derived_lots group by 1, 2) dl on dl.account_id = sn.account_id and dl.listing_id = sn.listing_id""",
                (accounts,))
    split, fewer, more = [], [], []
    for sym, led, rep in cur.fetchall():
        led, rep = D(led), D(rep or 0)
        if abs(led - rep) <= D("0.0002"):
            continue
        if led > 0 and rep > led and (rep / led) == (rep / led).to_integral_value():
            split.append(sym)
        elif led < rep:
            fewer.append(sym)
        else:
            more.append(sym)
    reasons, acts = [], []
    if split:
        reasons.append(f"W raporcie XTB jest całkowitą wielokrotność ilości z księgi ({', '.join(split)}) – podział akcji niewidoczny w historii operacji.")
        acts += _split_actions(questions, split)
    if fewer:
        reasons.append(f"W księdze brakuje części zakupów ({', '.join(fewer)}) – prawdopodobnie nie wczytano wszystkich plików (inne konto albo wcześniejszy okres).")
    if more:
        reasons.append(f"W księdze jest więcej papierów niż w raporcie ({', '.join(more)}) – raport jest starszy niż ostatnie operacje albo brakuje pliku ze sprzedażą.")
    if fewer or more or not acts:
        acts.append(_act(IMPORT, "Wczytaj nowszy / brakujący eksport XTB"))
    return " ".join(reasons), acts


def annotate(cur, audit, questions, scope_code):
    """Dopisuje przyczyne i rozwiazania do kontroli ze statusem innym niz OK (w miejscu)."""
    checks = audit.get("checks") or []
    if all(c["status"] == "OK" for c in checks):
        return audit
    cur.execute("select sa.account_id from scope_accounts sa join scopes sc using (scope_id) where sc.code = %s", (scope_code,))
    accounts = [r[0] for r in cur.fetchall()]
    for c in checks:
        if c["status"] == "OK":
            continue
        name = c["kontrola"]
        if name.startswith("Wszystkie pozycje mają cenę"):
            why, acts = _missing_prices(cur, c, questions, accounts)
        elif name.startswith("Brak ujemnych stanów"):
            why, acts = _negative(cur, questions, accounts)
        elif name.startswith("Ilości w księdze"):
            why, acts = _qty_vs_report(cur, questions, accounts)
        elif name.startswith("Lokata") and "model" in name:
            qs = _questions_by_prefix(questions, "tr-deposit-")
            why = "Oprocentowanie gotówki zmieniło się (wypłacone odsetki odpowiadają innej stopie niż zapisana)." if qs else \
                  "Wypłacone odsetki nie zgadzają się z modelem – np. zmiana stopy w trakcie miesiąca; pojawi się pytanie po kolejnej wypłacie."
            acts = [_act(ANSWER, q["odpowiedzi"][0]["etykieta"], broker=q["broker"], pytanie=q["id"], odpowiedz=q["odpowiedzi"][0]["wartosc"]) for q in qs]
        elif name.startswith("Lokata"):
            why = "W saldzie gotówki są operacje, których aplikacja nie rozpoznała jako wpłaty, wypłaty ani dochodu – mogą pochodzić z niepełnego pliku."
            acts = [_act(IMPORT, "Wczytaj aktualny eksport"), _act(REBUILD, "Przelicz wszystko od nowa")]
        elif name.startswith("Wartość każdej pozycji") or name.startswith("Wartość portfela"):
            why = "Ceny zmieniły się w trakcie sprawdzania albo przeliczenie jest nieaktualne."
            acts = [_act(REBUILD, "Przelicz wszystko od nowa"), _act(PRICES, "Pobierz ceny ponownie")]
        else:
            why = "Niespójność obliczeń (tożsamość księgowa się nie zgadza) – przeliczenie od nowa z danych z eksportów zwykle ją usuwa; jeśli nie, to błąd aplikacji."
            acts = [_act(REBUILD, "Przelicz wszystko od nowa")]
        c["przyczyna"], c["rozwiazania"] = why, acts
    return audit
