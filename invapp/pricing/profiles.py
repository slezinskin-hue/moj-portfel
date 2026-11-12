"""
Profile instrumentow do zakladki Ekspozycja (migracja 0019): typ (akcja/ETF), sektor, branza, gielda - z wyszukiwarki symboli Yahoo.
Pobierane w tle dla posiadanych instrumentow ze ZWERYFIKOWANYM symbolem u dostawcy; odswiezane co PROFILE_DAYS dni. Wysylany jest tylko symbol.
Dane opisowe - nie wplywaja na kwoty.
"""
from .providers import fetch_all

PROFILE_DAYS = 30


def refresh(conn, provider):
    cur = conn.cursor()
    cur.execute("""select distinct m.listing_id, m.provider_symbol from listing_provider_symbols m
                   join derived_lots dl on dl.listing_id = m.listing_id and dl.qty_remaining > 0
                   where m.provider = 'YAHOO' and m.status = 'VERIFIED'
                     and not exists (select 1 from listing_profile p where p.listing_id = m.listing_id and p.symbol = m.provider_symbol
                                     and p.fetched_at > now() - make_interval(days => %s))""", (PROFILE_DAYS,))
    rows = cur.fetchall()
    found = fetch_all(lambda s: provider.search_profile(s), [s for _, s in rows])
    saved = 0
    for lid, sym in rows:
        prof, err = found[sym]
        if err is not None or not prof:
            continue
        cur.execute("""insert into listing_profile(listing_id, provider, symbol, quote_type, sector, industry, exchange, long_name, fetched_at)
                       values (%s, 'YAHOO', %s, %s, %s, %s, %s, %s, now())
                       on conflict (listing_id) do update set symbol = excluded.symbol, quote_type = excluded.quote_type, sector = excluded.sector,
                         industry = excluded.industry, exchange = excluded.exchange, long_name = excluded.long_name, fetched_at = now()""",
                    (lid, sym, prof.get("type"), prof.get("sector"), prof.get("industry"), prof.get("exchange"), prof.get("name")))
        saved += 1
    return {"profile": saved, "do_pobrania": len(rows)}
