"""
Dostawcy danych. Wymienny interfejs: kazdy dostawca zwraca slowniki o tym samym ksztalcie; siec jest wstrzykiwana (fetch), wiec testy dzialaja bez internetu.
  * YahooProvider - notowania i kursy walut (nieoficjalny endpoint wykresow; bez klucza; dokument 20). Wysylany jest tylko symbol.
  * NbpProvider   - oficjalne kursy srednie NBP (tabela A), raz dziennie.
"""
import json, urllib.request
from datetime import datetime, timezone
from decimal import Decimal

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) investing-app-local"


def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


class ProviderError(Exception):
    pass


WORKERS = 6         # rownolegle zapytania do dostawcy (zamiast jednego po drugim z przerwa) - kilkanascie razy szybciej, nadal umiarkowane obciazenie


def fetch_all(fn, items, workers=WORKERS):
    """Wywoluje fn(item) rownolegle (siec); zwraca {item: (wynik, wyjatek)}. Zapis do bazy robi wywolujacy, po kolei, w jednym watku."""
    from concurrent.futures import ThreadPoolExecutor
    items = list(dict.fromkeys(items))
    out = {}

    def one(it):
        try:
            return it, (fn(it), None)
        except Exception as e:  # noqa: BLE001
            return it, (None, e)
    if not items:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        for it, res in ex.map(one, items):
            out[it] = res
    return out


class YahooProvider:
    code = "YAHOO"
    # sufiks symbolu XTB -> sufiks Yahoo (konfiguracja dostawcy, nie dane uzytkownika); kazde powiazanie i tak jest weryfikowane
    SUFFIX = {"PL": ".WA", "NL": ".AS", "DE": ".DE", "UK": ".L", "US": "", "FR": ".PA", "IT": ".MI", "ES": ".MC", "BE": ".BR", "PT": ".LS",
              "CH": ".SW", "DK": ".CO", "SE": ".ST", "NO": ".OL", "FI": ".HE", "AT": ".VI", "IE": ".IR"}

    def __init__(self, fetch=http_get):
        self.fetch = fetch

    def symbol_for(self, broker_symbol):
        if "." not in broker_symbol:
            return None
        base, suffix = broker_symbol.rsplit(".", 1)
        if suffix not in self.SUFFIX:
            return None
        return base + self.SUFFIX[suffix]

    def quote(self, provider_symbol):
        body = self.fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}?interval=1m&range=1d")
        try:
            meta = json.loads(body)["chart"]["result"][0]["meta"]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Nieczytelna odpowiedz Yahoo dla {provider_symbol}") from e
        price, t, ccy = meta.get("regularMarketPrice"), meta.get("regularMarketTime"), meta.get("currency")
        if price is None or t is None or not ccy:
            raise ProviderError(f"Brak ceny, czasu lub waluty w odpowiedzi Yahoo dla {provider_symbol}")
        unit = "MINOR_1_100" if ccy in ("GBp", "GBX", "ZAc", "ILA") else "MAJOR"
        currency = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(ccy, ccy)
        return {"symbol": provider_symbol, "price": Decimal(str(price)), "currency": currency, "price_unit": unit, "raw_currency": ccy,
                "quoted_at": datetime.fromtimestamp(int(t), timezone.utc), "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
                "market_state": meta.get("marketState"), "instrument_type": meta.get("instrumentType")}

    def search(self, query):
        """Wyszukiwanie symboli po ISIN (nieoficjalny endpoint wyszukiwania Yahoo): lista kandydatow; wynik NIE jest dowodem - kazdy kandydat jest weryfikowany cena."""
        from urllib.parse import quote as urlquote
        body = self.fetch(f"https://query2.finance.yahoo.com/v1/finance/search?q={urlquote(query)}&quotesCount=8&newsCount=0")
        try:
            items = json.loads(body)["quotes"]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Nieczytelna odpowiedz wyszukiwania Yahoo dla {query}") from e
        return [{"symbol": i["symbol"], "exchange": i.get("exchDisp") or i.get("exchange"), "name": i.get("longname") or i.get("shortname"), "type": i.get("quoteType")}
                for i in items if i.get("symbol") and i.get("quoteType") in ("EQUITY", "ETF")]

    def search_profile(self, provider_symbol):
        """Profil notowania z wyszukiwarki Yahoo (tylko dokladnie ten symbol): {type, sector, industry, exchange, name} albo None."""
        from urllib.parse import quote as urlquote
        body = self.fetch(f"https://query2.finance.yahoo.com/v1/finance/search?q={urlquote(provider_symbol)}&quotesCount=5&newsCount=0")
        try:
            items = json.loads(body)["quotes"]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Nieczytelna odpowiedz wyszukiwania Yahoo dla {provider_symbol}") from e
        q = next((i for i in items if i.get("symbol") == provider_symbol), None)
        if q is None:
            return None
        return {"type": q.get("quoteType"), "sector": q.get("sector") or None, "industry": q.get("industry") or None,
                "exchange": q.get("exchDisp") or q.get("exchange"), "name": q.get("longname") or q.get("shortname")}

    def splits(self, provider_symbol, start_ts=946684800):
        """Podzialy akcji wg Yahoo (zdarzenia z endpointu wykresow): [(data, licznik, mianownik)] - np. 10:1 = (data, 10, 1)."""
        import time as _t
        body = self.fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}?interval=1d&period1={int(start_ts)}&period2={int(_t.time()) + 86400}&events=split")
        try:
            res = json.loads(body)["chart"]["result"][0]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Nieczytelna odpowiedz Yahoo dla {provider_symbol}") from e
        out = []
        for ev in ((res.get("events") or {}).get("splits") or {}).values():
            try:
                out.append((datetime.fromtimestamp(int(ev["date"]), timezone.utc).date(), Decimal(str(ev["numerator"])), Decimal(str(ev["denominator"]))))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(out)

    def fx(self, base, quote="PLN"):
        q = self.quote(f"{base}{quote}=X")
        return {"base": base, "quote": quote, "rate": q["price"], "quoted_at": q["quoted_at"]}


class NbpProvider:
    code = "NBP_A"

    def __init__(self, fetch=http_get):
        self.fetch = fetch

    def table_a(self):
        body = self.fetch("https://api.nbp.pl/api/exchangerates/tables/A/?format=json")
        try:
            t = json.loads(body)[0]
        except Exception as e:  # noqa: BLE001
            raise ProviderError("Nieczytelna odpowiedz NBP") from e
        return {"table": t["no"], "date": t["effectiveDate"], "rates": {r["code"]: Decimal(str(r["mid"])) for r in t["rates"]}}
