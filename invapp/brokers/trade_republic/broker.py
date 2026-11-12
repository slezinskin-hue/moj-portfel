"""
Broker Trade Republic: eksport transakcji CSV (23 kolumny) z aplikacji TR. Plik nie zawiera numeru konta ani salda; konto zaklada sie samo
(jedno na wartosc account_type), partie liczone metoda FIFO, instrumenty po ISIN; waluta notowania i symbol u dostawcy cen ustalane
dla otwartych pozycji ze zrodla zewnetrznego ze sprawdzeniem ceny (pricing.py).
"""
from decimal import Decimal as D

from invapp.brokers.base import Broker
from invapp.brokers.trade_republic import parser as tr
from invapp.brokers.trade_republic.importer import TrImporter
from invapp.core import derive
from invapp.core.common import ImportFailed


TAX_RATE = D("0.19")           # podatek od odsetek (Belka) - podany przez uzytkownika i zgodny z potraceniami w ksiedze (~19%)


def set_deposit_terms(conn, account_id, treat_as_deposit, cand, decided_by):
    """Zapis decyzji (nowy wiersz). cand: wynik _deposit_candidates (stopa wyznaczona z wyplaconych odsetek)."""
    cur = conn.cursor()
    if treat_as_deposit:
        src = (f"wyznaczona z wypłaty odsetek z {cand['last_payout']} (saldo dzienne × stopa / 365); "
               f"model odtworzył {cand['reproduced']} z {cand['payouts']} wypłat z księgi")
        cur.execute("""insert into cash_deposit_terms(account_id,treat_as_deposit,annual_rate,tax_rate,day_count,capitalization,rate_source,decided_by)
                       values (%s,true,%s,%s,365,'MONTHLY',%s,%s)""", (account_id, cand["rate"], TAX_RATE, src, decided_by))
    else:
        cur.execute("insert into cash_deposit_terms(account_id,treat_as_deposit,decided_by) values (%s,false,%s)", (account_id, decided_by))
    return {"zapisano": True, "lokata": treat_as_deposit, "stopa": cand["rate"] if treat_as_deposit else None}


class TradeRepublic(Broker):
    code, key, name = "TR", "tr", "Trade Republic"
    accept = (".csv",)
    file_help = ("<strong>Trade Republic</strong>: plik <strong>.csv</strong> „Eksport transakcji” pobrany z aplikacji TR "
                 "(oryginalny, bez zapisywania w Excelu).")

    def matches(self, filename, head):
        if not filename.lower().endswith(".csv"):
            return False
        first = head[:600].decode("utf-8-sig", errors="replace").splitlines()[:1]
        return bool(first) and [c.strip().strip('"') for c in first[0].split(",")] == tr.COLUMNS

    def validate_upload(self, filename, data):
        if len(data) > 5_000_000 or len(data) < 100:
            raise ImportFailed("Plik ma nieprawidłowy rozmiar.")

    def import_path(self, conn, path, display_name=None):
        rep = TrImporter(conn).import_file(path)
        rep["derived"] = self.rebuild_derived(conn)
        if display_name:
            rep["file"] = display_name
        return rep

    def rebuild_derived(self, conn):
        return derive.rebuild_fifo(conn, "TR")

    # ---- pytanie: gotowka jako lokata (stopa wyznaczona z wyplaconych odsetek; uzytkownik potwierdza)
    def _deposit_candidates(self, conn):
        from invapp.core import deposits
        cur = conn.cursor(); out = []
        cur.execute("""select a.account_id, a.display_name from accounts a join financial_institutions f using (broker_id) where f.code = 'TR' order by 1""")
        for acc, name in cur.fetchall():
            txns = deposits.account_txns(cur, acc)
            rate = deposits.implied_rate(txns)
            if rate is None:
                continue                                       # brak wyplaconych odsetek - nie ma z czego wyznaczyc stopy, nie pytamy
            terms = deposits.current_terms(cur, acc)
            rep = deposits.reproduce_payouts(txns, rate)
            ok = sum(1 for r in rep if abs(r["diff"]) <= D("0.05"))
            out.append({"account_id": acc, "name": name, "rate": rate, "terms": terms, "reproduced": ok, "payouts": len(rep),
                        "last_payout": max(r["date"] for r in rep)})
        return out

    def pending_questions(self, conn):
        out = []
        for c in self._deposit_candidates(conn):
            t, pct = c["terms"], f"{c['rate'] * 100:.2f}".replace(".", ",")
            if t is None:
                out.append({"id": f"tr-deposit-{c['account_id']}", "tytul": "Gotówka w Trade Republic jako lokata",
                            "tresc": f"Gotówka na koncie „{c['name']}” jest oprocentowana. Z wypłaconych odsetek wynika {pct}% rocznie "
                                     f"(odsetki liczone codziennie od salda, wypłacane co miesiąc; model odtwarza {c['reproduced']} z {c['payouts']} wypłat). "
                                     f"Czy liczyć tę gotówkę jako lokatę ({pct}% rocznie, kapitalizacja co miesiąc, podatek Belki 19%)? Wtedy wartość aktywów = "
                                     f"papiery + saldo gotówki z eksportu (tylko kwoty zaksięgowane; odsetki trafiają do dochodów po wypłacie).",
                            "odpowiedzi": [{"wartosc": "tak", "etykieta": f"Tak, lokata {pct}%"}, {"wartosc": "nie", "etykieta": "Nie, pomijaj gotówkę"}]})
            elif t["treat_as_deposit"] and abs(D(str(t["annual_rate"])) - c["rate"]) >= D("0.0001"):
                old = f"{D(str(t['annual_rate'])) * 100:.2f}".replace(".", ",")
                out.append({"id": f"tr-deposit-{c['account_id']}", "tytul": "Zmiana oprocentowania gotówki w Trade Republic",
                            "tresc": f"Z ostatniej wypłaty odsetek ({c['last_payout']}) wynika {pct}% rocznie, a zapisane jest {old}%. "
                                     f"Zaktualizować oprocentowanie lokaty?",
                            "odpowiedzi": [{"wartosc": "tak", "etykieta": f"Tak, {pct}%"}, {"wartosc": "zostaw", "etykieta": f"Zostaw {old}%"}]})
        return out

    def answer_question(self, conn, question_id, answer, fields=None, decided_by="użytkownik (aplikacja)"):
        if not question_id.startswith("tr-deposit-"):
            raise ImportFailed("Nieznane pytanie.")
        try:
            acc = int(question_id.rsplit("-", 1)[-1])
        except ValueError:
            raise ImportFailed("Nieznane pytanie.")
        cand = next((c for c in self._deposit_candidates(conn) if c["account_id"] == acc), None)
        if cand is None:
            raise ImportFailed("Nie ma takiego konta Trade Republic z wypłaconymi odsetkami.")
        if answer == "zostaw":
            return {"zapisano": False}
        return set_deposit_terms(conn, acc, answer == "tak", cand, decided_by)

    def pricing_steps(self, conn, provider):
        from invapp.brokers.trade_republic import pricing
        return [("Symbole Trade Republic (ISIN → dostawca cen)", lambda: [r["status"] for r in pricing.resolve_tr_listings(conn, provider)], "quick")]
