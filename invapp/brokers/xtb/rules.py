"""
JAWNE reguly uzytkownika (broker_symbol_suffix_rules, np. .PL = PLN) zastosowane do juz zaimportowanych danych:
  1. notowania z pusta waluta (bez wpisu w specyfikacji brokera), ktorych sufiks ma regule -> waluta + status POTWIERDZONY_PRZEZ_UZYTKOWNIKA + zrodlo + data;
  2. transakcje BUY/SELL tych notowan z price_currency = 'XXX' -> waluta reguly, kurs efektywny (tylko gdy zgodny z waluta: PLN 1,00 +/- 0,5%).
Idempotentne. Kwoty, koszt, wartosc i wynik nie zmieniaja sie.
"""
from decimal import Decimal

TOL = Decimal("0.005")


def apply(conn):
    cur = conn.cursor()
    res = {"listings_updated": 0, "transactions_updated": 0, "transactions_skipped": []}
    cur.execute("select broker_id, symbol_suffix, value, source, confirmed_at from broker_symbol_suffix_rules where attribute='TRADING_CURRENCY'")
    for broker, suffix, ccy, source, at in cur.fetchall():
        cur.execute("""select l.listing_id, la.broker_symbol from listings l join listing_aliases la on la.listing_id=l.listing_id and la.valid_to is null
                       where la.broker_id=%s and la.broker_symbol like %s and l.trading_currency is null""", (broker, "%." + suffix))
        for lst, symbol in cur.fetchall():
            cur.execute("""update listings set trading_currency=%s, currency_status='POTWIERDZONY_PRZEZ_UZYTKOWNIKA', currency_source=%s, currency_checked_at=%s,
                           currency_evidence = coalesce(currency_evidence, '{}'::jsonb) || jsonb_build_object('rule','broker_symbol_suffix_rules','suffix',%s::text)
                           where listing_id=%s""", (ccy, source, at, suffix, lst))
            res["listings_updated"] += 1
        cur.execute("""select t.txn_id, t.external_id, abs(t.cash_amount) / (t.quantity * t.price) from transactions t
                       join listing_aliases la on la.listing_id=t.listing_id and la.valid_to is null
                       where t.txn_type in ('BUY','SELL') and t.price_currency='XXX' and la.broker_id=%s and la.broker_symbol like %s""", (broker, "%." + suffix))
        for tid, ext, eff in cur.fetchall():
            if ccy == "PLN" and abs(eff - 1) > TOL:
                res["transactions_skipped"].append({"external_id": ext, "effective_rate": str(eff)}); continue
            cur.execute("""update transactions set price_currency=%s, fx_rate=%s, fx_rate_kind='EFFECTIVE_IMPLIED',
                           details = details || jsonb_build_object('currency_source','broker_symbol_suffix_rules') where txn_id=%s""",
                        (ccy, eff.quantize(Decimal("0.0000000001")), tid))
            res["transactions_updated"] += 1
    return res
