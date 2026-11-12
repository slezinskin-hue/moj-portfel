/* Strona portfela: dane z API (/api/zywo, /api/historia, /api/wskazniki, /api/dochody, /api/kontrola, /api/pytania) rysowane po stronie przegladarki.
 * Zapisy (import, odpowiedzi na pytania) tylko z naglowkiem X-Requested-With: invapp. */
(function () {
  const BOOT = JSON.parse(document.getElementById('boot').textContent);
  const SCOPE = BOOT.scope;
  // brakujacy element (np. pusty portfel bez sekcji wykresow) -> niepodpiety element zastepczy, zeby reszta strony (import) dzialala
  const $ = s => document.querySelector(s) || document.createElement('div'), $$ = s => Array.from(document.querySelectorAll(s));
  const esc = s => String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const nf2 = new Intl.NumberFormat('pl-PL', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const nf0 = new Intl.NumberFormat('pl-PL', {maximumFractionDigits: 0});
  const zl = v => (v === null || v === undefined || isNaN(v)) ? '—' : nf2.format(v) + ' zł';
  const zl0 = v => (v === null || v === undefined || isNaN(v)) ? '—' : nf0.format(v) + ' zł';
  const sgn = v => (v > 0 ? '+' : '');
  const zlS = v => (v === null || v === undefined) ? '—' : sgn(v) + zl(v);
  const pct = (v, d) => (v === null || v === undefined || isNaN(v)) ? '—' : sgn(v) + (v * 100).toLocaleString('pl-PL', {minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d}) + '%';
  const cls = v => (v === null || v === undefined) ? '' : v >= 0 ? 'pos' : 'neg';
  const num = (v, d) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString('pl-PL', {maximumFractionDigits: d === undefined ? 4 : d});
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const PAL = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)', 'var(--c6)', 'var(--c7)', 'var(--c8)', 'var(--c9)', 'var(--c10)'];
  const hhmm = s => s ? new Date(s).toLocaleTimeString('pl-PL', {hour: '2-digit', minute: '2-digit'}) : '—';
  const when = s => { if (!s) return '—'; const d = new Date(s), t = new Date(); return (d.toDateString() === t.toDateString() ? 'dziś' : d.toLocaleDateString('pl-PL', {day: '2-digit', month: '2-digit'})) + ' ' + hhmm(s); };
  const PRODUCT = {IKE: 'IKE', IKZE: 'IKZE', STANDARD: 'zwykłe', STANDARD_SUB: 'zwykłe (plany)'};
  const toast = msg => { const t = $('#toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 3500); };
  const getJSON = async url => { const r = await fetch(url); const d = await r.json(); if (!r.ok) throw new Error(d.blad || r.status); return d; };

  // ---------------------------------------------------------------- motyw
  const theme = (() => { try { return localStorage.getItem('invapp-theme'); } catch (e) { return null; } })();
  if (theme) document.documentElement.dataset.theme = theme;
  $('#theme').onclick = () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('invapp-theme', document.documentElement.dataset.theme); } catch (e) { /* brak pamieci przegladarki */ }
    redrawAll();
  };

  // ---------------------------------------------------------------- stan danych
  const S = {live: null, hist: null, met: null, inc: null, audit: null, version: null, alloc: 'pos', mode: 'value', range: 'ALL', sort: ['value', -1], q: '', incAll: false};

  // ---------------------------------------------------------------- KPI i pozycje
  async function loadLive(opts) {
    opts = opts || {};
    try {
      const d = await getJSON('/api/zywo?scope=' + SCOPE + (opts.force ? '&force=true' : '') + (opts.fast ? '&fast=true' : ''));
      S.live = d; renderKpis(); renderPositions(); renderAlloc(); renderDeposits();
      if (viewOf() === 'symulacje') window.renderSim();
    } catch (e) { $('#price-note').innerHTML = '<span class="warn">Nie udało się pobrać wartości (brak połączenia z bazą lub internetem).</span>'; }
  }
  function renderKpis() {
    const s = S.live && S.live.scope; if (!s) return;
    const assets = s.assets_value, total = s.total_result_live, base = assets - total;      // procent zysku: od kwot zainwestowanych (bez gotówki)
    const m = S.met && S.met.available ? S.met : null;
    const cash = +s.cash_value || 0, free = +s.xtb_free_value || 0;
    $('#k-value').textContent = zl(s.total_value !== undefined ? s.total_value : assets);
    const parts = [['papiery', s.securities_value]].concat(+s.deposits_value ? [['lokaty', s.deposits_value]] : [], cash ? [['gotówka', cash]] : [], free ? [['wolne środki XTB', free]] : []);
    $('#k-value-l').textContent = 'Wartość portfela' + (parts.length > 1 ? ' (' + parts.map(p => p[0]).join(' + ') + ')' : '');
    $('#k-value-s').innerHTML = parts.length > 1 ? parts.map(p => p[0] + ' ' + zl(p[1])).join(' · ') : 'ilość × aktualna cena z Yahoo × kurs';
    $('#k-day').innerHTML = m && m.day_change !== null ? `<span class="chip">${zlS(m.day_change)} (${pct(m.day_change_pct)}) od ${m.day_change_from.slice(8, 10)}.${m.day_change_from.slice(5, 7)}</span>` : '';
    $('#k-total').innerHTML = `<span class="${cls(total)}">${zlS(total)}</span>`;
    $('#k-total-s').innerHTML = (base > 0 ? `<span class="chip ${cls(total)}">${pct(total / base)}</span> od wpłaconych ` + zl0(base) : '') ;
    $('#k-inv').innerHTML = `<span class="${cls(s.investment_gain)}">${zlS(s.investment_gain)}</span>`;
    $('#k-inv-s').textContent = 'niezrealizowany ' + zlS(s.unrealized_gain) + ' · zrealizowany ' + zlS(s.realized_gain);
    const inc = s.additional_income;
    $('#k-inc').innerHTML = `<span class="${cls(inc)}">${zlS(inc)}</span>`;
    $('#k-inc-s').textContent = 'dywidendy ' + zl(s.dividends_net) + ' · odsetki ' + zl(s.interest_net) + (s.rewards ? ' · nagrody ' + zl(s.rewards) : '') + (s.fees ? ' · opłaty ' + zl(s.fees) : '');
    const missing = (s.snapshot_positions || 0) + (s.missing_positions || 0);
    const xtbMode = s.settings && s.settings.price_source === 'XTB_EXPORT', nbp = s.settings && s.settings.fx_source === 'NBP';
    $('#price-note').innerHTML = (s.positions ? (xtbMode ? 'Ceny XTB z ostatniego eksportu (ustawienie ⚙), pozostałe z Yahoo: ' : 'Ceny z Yahoo Finance: ') + when(s.oldest_quote) + (s.oldest_quote !== s.newest_quote ? ' – ' + when(s.newest_quote) : '') + (nbp ? ' · kursy walut: średnie NBP' : ' · kursy walut na żywo') + ' · odświeżanie co minutę' : 'Brak otwartych pozycji') +
      (missing ? ` · <span class="warn">${missing} poz. bez ceny rynkowej (pobieram w tle)</span>` : '');
  }
  function posRows() {
    const p = (S.live && S.live.positions) || [], tot = p.reduce((a, x) => a + (x.value || 0), 0);
    return p.map(x => Object.assign({}, x, {share: tot && x.value !== null ? x.value / tot : null, gain: x.value !== null && x.cost_left !== null ? x.value - x.cost_left : null,
      gainp: x.cost_left > 0 && x.value !== null ? (x.value - x.cost_left) / x.cost_left : null, acct: [...new Set((x.accounts || []).map(a => a.broker === 'TR' ? 'TR' : (PRODUCT[a.product_type] || a.product_type)))].join(', ')}));
  }
  function renderPositions() {
    const tb = $('#pos tbody'); if (!S.live) return;
    let rows = posRows(); const q = S.q.trim().toLowerCase();
    if (q) rows = rows.filter(r => (r.ticker + ' ' + r.instrument + ' ' + r.acct).toLowerCase().includes(q));
    const [k, dir] = S.sort;
    rows.sort((a, b) => { const x = a[k], y = b[k]; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1; return (typeof x === 'string' ? x.localeCompare(y, 'pl') : x - y) * dir; });
    $$('#pos th.sort').forEach(th => { th.classList.toggle('on', th.dataset.k === k); th.classList.toggle('asc', th.dataset.k === k && dir > 0); });
    const maxShare = Math.max(...rows.map(r => r.share || 0), 0.0001);
    tb.innerHTML = rows.map((r, i) => {
      const xtb = r.price_source === 'XTB_EXPORT', market = r.price_source === 'MARKET' || xtb, unit = r.price_unit === 'MINOR_1_100' ? ' (pensy)' : '';
      return `<tr data-i="${i}" style="cursor:pointer"><td><span class="tick">${esc(r.ticker)}</span><span class="inst">${esc(r.instrument)}</span></td>
        <td>${(r.acct || '').split(', ').filter(Boolean).map(a => `<span class="badge">${esc(a)}</span>`).join(' ')}</td>
        <td class="n">${num(r.qty)}</td><td class="n">${market && r.price_pln !== null ? zl(r.price_pln) : '<span class="warn">brak ceny</span>'}</td>
        <td class="n"><b>${r.value === null ? '—' : zl(r.value)}</b></td>
        <td class="n">${r.share === null ? '—' : pct(r.share, 1).replace('+', '')}<span class="bar"><i style="width:${(r.share || 0) / maxShare * 100}%"></i></span></td>
        <td class="n">${zl(r.cost_left)}</td><td class="n ${cls(r.gain)}">${zlS(r.gain)}</td><td class="n ${cls(r.gainp)}">${pct(r.gainp, 1)}</td></tr>
        <tr class="sub" data-sub="${i}" style="display:none"><td colspan="9">
          ${xtb ? `Wycena z ostatniego eksportu XTB (cena i kurs XTB${r.fx_used ? ' ' + num(r.fx_used, 4) : ''}) z ${when(r.price_time)} – ustawienie „ceny z eksportu XTB”. Po nowym zakupie lub sprzedaży (inna ilość niż w eksporcie) pozycja wraca do ceny z Yahoo.` :
            market ? `Cena na giełdzie: <b>${num(r.price, 4)} ${esc(r.currency)}${unit}</b> × ilość = ${num(r.value_native, 2)} ${esc(r.currency)}` + (r.currency !== 'PLN' ? ` · kurs ${esc(r.currency)}/PLN <b>${num(r.fx_used, 4)}</b> (${r.fx_source === 'NBP_A' ? 'NBP' : 'Yahoo'})` : '') + ` · cena z ${when(r.price_time)}${r.market_state ? ' (' + esc(r.market_state) + ')' : ''}` : '<span class="warn">Brak ceny rynkowej z API – wartość niepewna (cena z raportu brokera albo brak).</span>'}
          ${(r.accounts || []).length > 1 ? '<br>Na kontach: ' + r.accounts.map(a => esc(a.account) + ' ' + num(a.qty) + ' szt. (' + zl(a.value) + ')').join(' · ') : ''}
        </td></tr>`;
    }).join('') || '<tr><td colspan="9" class="muted">Brak otwartych pozycji.</td></tr>';
    const deps = (S.live.deposits || []);
    $('#pos tfoot').innerHTML = deps.map(d => `<tr><td><span class="tick">Lokata</span><span class="inst">${esc(d.account)} – gotówka ${(d.rate * 100).toLocaleString('pl-PL', {maximumFractionDigits: 2})}% rocznie, saldo z ${esc(d.ledger_as_of)}</span></td><td><span class="badge">${esc(d.broker)}</span></td>
       <td class="n">—</td><td class="n">—</td><td class="n"><b>${zl(d.value)}</b></td><td class="n">—</td><td class="n">—</td><td class="n">—</td><td class="n">—</td></tr>`).join('');
    tb.querySelectorAll('tr[data-i]').forEach(tr => tr.onclick = () => { const s = tb.querySelector(`tr[data-sub="${tr.dataset.i}"]`); s.style.display = s.style.display === 'none' ? '' : 'none'; });
  }
  $$('#pos th.sort').forEach(th => th.onclick = () => { const k = th.dataset.k; S.sort = [k, S.sort[0] === k ? -S.sort[1] : (th.classList.contains('n') ? -1 : 1)]; renderPositions(); });
  $('#pos-q').oninput = e => { S.q = e.target.value; renderPositions(); };

  function renderAlloc() {
    if (!S.live) return;
    const rows = posRows().filter(r => r.value > 0), deps = S.live.deposits || [];
    let items = [];
    if (S.alloc === 'pos') {
      rows.sort((a, b) => b.value - a.value);
      items = rows.slice(0, 9).map(r => ({label: r.ticker, value: r.value}));
      const rest = rows.slice(9).reduce((a, r) => a + r.value, 0); if (rest > 0) items.push({label: 'pozostałe (' + (rows.length - 9) + ')', value: rest});
      deps.forEach(d => items.push({label: 'Lokata ' + d.broker, value: d.value}));
    } else {
      const g = {}, add = (k, v) => { g[k] = (g[k] || 0) + v; };
      rows.forEach(r => (r.accounts || []).forEach(a => {
        if (!a.value) return;
        if (S.alloc === 'acct') add(a.broker === 'TR' ? 'Trade Republic' : (a.product_type === 'IKE' ? 'IKE' : a.product_type === 'IKZE' ? 'IKZE' : 'XTB zwykłe'), a.value);
        else if (S.alloc === 'broker') add(a.broker === 'TR' ? 'Trade Republic' : a.broker, a.value);
        else add(r.currency || '?', a.value);
      }));
      deps.forEach(d => add(S.alloc === 'acct' ? 'Lokaty' : S.alloc === 'broker' ? (d.broker === 'TR' ? 'Trade Republic' : d.broker) : 'PLN', d.value));
      const sc = S.live.scope || {};
      if (+sc.cash_value) add(S.alloc === 'acct' ? 'Gotówka (banki)' : S.alloc === 'broker' ? 'Banki' : 'gotówka', +sc.cash_value);
      if (+sc.xtb_free_value) add(S.alloc === 'acct' ? 'Wolne środki XTB' : S.alloc === 'broker' ? 'XTB' : 'PLN', +sc.xtb_free_value);
      items = Object.entries(g).map(([label, value]) => ({label, value})).sort((a, b) => b.value - a.value);
    }
    if (S.alloc === 'pos') {
      const sc = S.live.scope || {};
      if (+sc.cash_value) items.push({label: 'Gotówka (banki)', value: +sc.cash_value});
      if (+sc.xtb_free_value) items.push({label: 'Wolne środki XTB', value: +sc.xtb_free_value});
    }
    items.forEach((it, i) => it.color = PAL[i % PAL.length]);
    Charts.donut($('#alloc'), items, {fmt: zl, centerLabel: 'Wartość'});
  }
  $$('#alloc-seg button').forEach(b => b.onclick = () => { S.alloc = b.dataset.k; $$('#alloc-seg button').forEach(x => x.classList.toggle('on', x === b)); renderAlloc(); });

  function renderDeposits() {
    const deps = (S.live && S.live.deposits) || [], box = $('#deps');
    box.style.display = deps.length ? '' : 'none'; if (!deps.length) return;
    $('#deps-body').innerHTML = deps.map(d => `<div class="dep">
        <div class="m"><div class="l">Wartość lokaty</div><div class="v">${zl(d.value)}</div><div class="s">saldo gotówki z eksportu, stan na ${esc(d.ledger_as_of)}</div></div>
        <div class="m"><div class="l">Oprocentowanie</div><div class="v">${(d.rate * 100).toLocaleString('pl-PL', {maximumFractionDigits: 2})}% rocznie</div><div class="s">wyznaczone z wypłaconych odsetek · ostatnia wypłata ${esc(d.last_payout || '—')}</div></div>
        <div class="m"><div class="l">Naliczone, jeszcze niewypłacone</div><div class="v muted">ok. ${zl(d.unbooked_interest_net)}</div><div class="s">nie wliczone do wartości ani zysku – pojawią się po wypłacie i nowym eksporcie</div></div>
      </div>`).join('') + '<p class="note">Lokata = gotówka na koncie oprocentowana jak lokata (odsetki codziennie od salda, wypłacane 1. dnia miesiąca, po podatku Belki 19%). Wartość to <b>saldo z eksportu</b> – liczone jest tylko to, co zostało zaksięgowane i wypłacone. Wypłacone odsetki są w dochodach. Stopa wyznaczona z wypłaconych odsetek; model odtwarza każdą wypłatę z eksportu (kontrola liczb).</p>';
  }

  // ---------------------------------------------------------------- wykres glowny i wskazniki
  let main = null, under = null;
  const store = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* brak pamieci przegladarki */ } };
  const stored = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k)); return Array.isArray(v) ? v : d; } catch (e) { return d; } };
  // wykres "Zwrot %": miary do wyboru (kilka naraz) i indeksy do porownania
  const PCT = [
    {k: 'twr', key: 'twr_lvl', label: 'TWR', color: 'var(--c3)', rebase: true, width: 2.4, opis: 'Stopa zwrotu ważona czasem – nie zależy od tego, kiedy i ile wpłacałeś. Liczona od początku widocznego okresu; do porównań z indeksami.'},
    {k: 'twr_inc', key: 'twr_inc', label: 'TWR z dywidendami', color: 'var(--c4)', rebase: true, opis: 'TWR, w którym wypłacone dywidendy netto liczą się jako zwrot (jak w indeksach z dywidendami).'},
    {k: 'gain_net', key: 'gain_net', label: 'Zysk / wpłaty netto', color: 'var(--c1)', opis: 'Zysk (wartość − wpłaty netto) podzielony przez wpłaty netto w danym dniu – prosta stopa zwrotu od początku.'},
    {k: 'gain_gross', key: 'gain_gross', label: 'Zysk / suma wpłat', color: 'var(--c2)', opis: 'Zysk podzielony przez sumę wszystkich wpłat (bez odejmowania wypłat) – jak kafelek „Zysk od wpłat”.'},
    {k: 'xirr', key: 'xirr', label: 'XIRR (rocznie)', color: 'var(--c5)', connect: true, opis: 'Roczna stopa ważona pieniądzem od początku do danego dnia (co tydzień), uwzględnia moment i wielkość wpłat. Od 90 dni historii.'},
    {k: 'cagr', key: 'cagr', label: 'TWR rocznie (CAGR)', color: 'var(--c6)', opis: 'TWR od początku przeliczony na skalę roku. Od roku historii.'},
  ];
  const BCOL = {ndx: 'var(--c7)', spx: 'var(--c8)', msci: 'var(--c9)', wig20: 'var(--c10)'};
  S.pct = stored('invapp-pct', ['twr']); S.bench = stored('invapp-bench', []);
  const benchList = () => (S.hist && S.hist.porownanie && S.hist.porownanie.indeksy) || [];
  const benchHasData = b => { const lv = S.hist.porownanie.poziomy[b.key] || []; return lv.some(v => v !== null); };
  const MODES = {
    value: {series: () => [{key: 'value', label: 'Wartość portfela', color: 'var(--c1)', area: true}, {key: 'invested_net', label: 'Wpłaty netto', color: 'var(--c2)', width: 2}]
      .concat(S.hist && S.hist.series.some(p => Number(p.deposits)) ? [{key: 'securities', label: 'w tym papiery', color: 'var(--c4)', width: 1.5},
                                                                       {key: 'deposits', label: 'w tym lokaty (gotówka)', color: 'var(--c3)', width: 1.5}] : []), fmt: zl0},
    gain: {series: () => [{key: 'gain', label: 'Zysk (wartość − wpłaty netto)', color: 'var(--c4)', signed: true}], fmt: zl0},
    twr: {series: () => {
      const sel = PCT.filter(m => S.pct.includes(m.k)), bs = benchList().filter(b => S.bench.includes(b.key) && benchHasData(b));
      const single = sel.length === 1 && !bs.length && !sel[0].connect;
      return sel.map(m => Object.assign({}, m, {signed: single})).concat(bs.map(b => ({key: 'b_' + b.key, label: b.label, color: BCOL[b.key] || 'var(--c7)', rebase: true, dash: true, width: 1.8})));
    }, fmt: v => pct(v, Math.abs(v) < 0.1 ? 1 : 0)},
  };
  function mainPoints() {
    if (!S.hist || !S.hist.series) return [];
    const ri = {}, is = (S.met && S.met.index_series) || [], rs = S.met && S.met.return_series, bz = S.hist.porownanie;
    is.forEach(([d], j) => ri[d] = j);
    const pts = S.hist.series.map((p, j) => {
      const k = ri[p.date], o = {t: new Date(p.date + 'T00:00:00').getTime(), date: p.date, value: +p.value, invested_net: +p.invested_net, deposits: +p.deposits, securities: +p.securities,
        gain: +p.gain, invested_gross: +p.invested_gross, sold: +p.sold, positions: p.positions, est: p.estimated_positions,
        twr_lvl: k !== undefined ? is[k][1] : null};
      o.twr = o.twr_lvl === null ? null : o.twr_lvl - 1;
      if (rs && k !== undefined) PCT.slice(1).forEach(m => o[m.key] = rs[m.key] ? rs[m.key][k] : null);
      if (bz) bz.indeksy.forEach(b => o['b_' + b.key] = (bz.poziomy[b.key] || [])[j]);
      return o;
    });
    const s = S.live && S.live.scope;
    if (s && pts.length) { const last = pts[pts.length - 1]; last.value = +s.assets_value; last.gain = last.value - last.invested_net; last.securities = +s.securities_value; last.deposits = +s.deposits_value; }
    return pts;
  }
  function tipMain(p, series, chart) {
    const r = (c, l, v) => `<div class="r"><span><span class="sw" style="background:${c}"></span>${l}</span><b>${v}</b></div>`;
    let h = '<b>' + Charts.fmtDate(p.t) + '</b>';
    if (S.mode === 'twr') {
      series.forEach(s => { const v = chart.val(p, s); h += r(s.color, s.label, v === null ? '—' : pct(v)); });
      if (series.some(s => s.rebase)) h += `<div class="r muted"><span>TWR i indeksy od</span><span>${Charts.fmtDate(chart.baseT)}</span></div>`;
      return h;
    }
    if (S.mode === 'value') h += r('var(--c1)', 'Wartość', zl(p.value)) + (p.deposits ? r('var(--c4)', '· papiery', zl(p.securities)) + r('var(--c3)', '· lokaty', zl(p.deposits)) : '') + r('var(--c2)', 'Wpłaty netto', zl(p.invested_net));
    h += r(p.gain >= 0 ? 'var(--pos)' : 'var(--neg)', 'Zysk', zlS(p.gain));
    if (p.twr !== null) h += r('var(--c3)', 'Zwrot TWR', pct(p.twr));
    h += `<div class="r muted"><span>pozycji</span><span>${p.positions}${p.est ? ' (oszac. ' + p.est + ')' : ''}</span></div>`;
    return h;
  }
  function buildMain() {
    if (!main) {
      main = new Charts.TimeChart($('#chart'), {height: 380, series: MODES[S.mode].series(), fmtY: MODES[S.mode].fmt, fmtTip: tipMain});
      main.onWindow = (a, b) => { if (under) under.setWindow(a, b, true); $('#zoom-reset').style.display = main.isZoomed() ? 'inline' : 'none'; $$('#range button').forEach(x => x.classList.remove('on')); };
    }
    main.o.series = MODES[S.mode].series(); main.o.fmtY = MODES[S.mode].fmt; main.o.includeZero = S.mode === 'twr';
    const pts = mainPoints(); main.setData(pts, true); renderLegend(); renderPctTools();
    if (!under && S.met && S.met.index_series) {
      under = new Charts.TimeChart($('#under'), {height: 150, navigator: false, series: [{key: 'dd', label: 'Obsunięcie od szczytu', color: 'var(--neg)', area: true}], fmtY: v => pct(v, 0)});
      under.onWindow = (a, b) => main.setWindow(a, b);
    }
    if (under && S.met && S.met.index_series) under.setData(S.met.index_series.map(([d, i, dd]) => ({t: new Date(d + 'T00:00:00').getTime(), dd})), true);
    applyRange(S.range, true);
    const h = S.hist, last = pts[pts.length - 1];
    $('#chart-note').innerHTML = !pts.length ? 'Brak danych do wykresu.' : `Od ${esc(h.series[0].date)}: wartość ${zl(last.value)}, wpłaty netto ${zl(last.invested_net)}, zysk <span class="${cls(last.gain)}">${zlS(last.gain)}</span>.` +
      (h.estimated_days ? ` <span class="warn">${h.estimated_days} z ${h.series.length} dni ma oszacowane ceny części pozycji (brak historii instrumentu).</span>` : '') +
      (S.mode === 'twr' ? '<br>TWR i indeksy pokazują zwrot <b>od pierwszego dnia widocznego okresu</b> (przybliż lub wybierz 1R, YTD…, aby porównać inny okres); pozostałe miary liczone są od początku. ' +
        'Indeksy w PLN (kurs dnia z Yahoo), cenowe – bez dywidend, jak TWR; WIG20 to ETF Beta WIG20TR (z dywidendami), bo Yahoo nie podaje historii samego indeksu.' +
        (!main.o.series.length ? ' <span class="warn">Zaznacz co najmniej jedną miarę lub indeks.</span>' : '') : '');
  }
  function renderPctTools() {
    const box = $('#pct-tools'), on = S.mode === 'twr';
    box.style.display = on ? '' : 'none'; $('#legend').style.display = on ? 'none' : ''; if (!on) return;
    const chip = (k, label, color, active, title, dis, dash) => `<button data-k="${esc(k)}" class="${active ? 'on' : ''}" ${dis ? 'disabled' : ''} title="${esc(title || '')}"><span class="sw${dash ? ' dash' : ''}" style="--sw:${color}"></span>${esc(label)}</button>`;
    $('#pct-measures').innerHTML = PCT.map(m => chip(m.k, m.label, m.color, S.pct.includes(m.k), m.opis)).join('');
    const bl = benchList();
    $('#pct-bench').innerHTML = bl.length ? bl.map(b => { const ok = benchHasData(b); return chip(b.key, b.label, BCOL[b.key], ok && S.bench.includes(b.key), ok ? b.opis : 'Brak danych indeksu – pobieram w tle (wymaga internetu).', !ok, true); }).join('')
      : '<span class="muted" style="font-size:.8rem">indeksy pobierają się w tle…</span>';
    $$('#pct-measures button').forEach(b => b.onclick = () => { const k = b.dataset.k; S.pct = S.pct.includes(k) ? S.pct.filter(x => x !== k) : S.pct.concat(k); store('invapp-pct', S.pct); buildMain(); });
    $$('#pct-bench button').forEach(b => b.onclick = () => { const k = b.dataset.k; S.bench = S.bench.includes(k) ? S.bench.filter(x => x !== k) : S.bench.concat(k); store('invapp-bench', S.bench); buildMain(); });
  }
  function renderLegend() {
    $('#legend').innerHTML = MODES[S.mode].series().map(s => `<button data-k="${s.key}" class="${main.hidden.has(s.key) ? 'off' : ''}"><span class="sw" style="background:${s.signed ? 'linear-gradient(90deg,var(--pos),var(--neg))' : s.color}"></span>${s.label}</button>`).join('');
    $$('#legend button').forEach(b => b.onclick = () => { main.toggle(b.dataset.k); renderLegend(); });
  }
  function applyRange(k, silent) {
    S.range = k; if (!main || !main.data.length) return;
    const [a, b] = main.domain(), end = b; let start = a;
    const back = {'1M': 31, '3M': 92, '6M': 183, '1R': 366, '3L': 1096}[k];
    if (back) start = end - back * Charts.DAY; else if (k === 'YTD') start = new Date(new Date(end).getFullYear(), 0, 1).getTime();
    main.setWindow(Math.max(a, start), end, silent); if (under) under.setWindow(Math.max(a, start), end, true);
    $$('#range button').forEach(x => x.classList.toggle('on', x.dataset.k === k)); $('#zoom-reset').style.display = 'none';
  }
  $$('#range button').forEach(b => b.onclick = () => applyRange(b.dataset.k));
  $$('#mode button').forEach(b => b.onclick = () => { S.mode = b.dataset.k; $$('#mode button').forEach(x => x.classList.toggle('on', x === b)); buildMain(); });
  $('#zoom-reset').onclick = () => applyRange('ALL');

  async function loadHistory() {
    try {
      const [h, m] = await Promise.all([getJSON('/api/historia?scope=' + SCOPE), getJSON('/api/wskazniki?scope=' + SCOPE)]);
      S.hist = h; S.met = m; buildMain(); renderMetrics(); renderKpis();
      if (viewOf() === 'symulacje') window.renderSim();
    } catch (e) { $('#chart-note').textContent = 'Nie udało się wczytać historii (' + e.message + ').'; }
  }
  const INFO = {
    twr: 'Stopa zwrotu ważona czasem: łączy dzienne stopy zwrotu i nie zależy od tego, kiedy i ile wpłacałeś. Do porównań z indeksem lub funduszem.',
    xirr: 'Roczna stopa ważona pieniądzem: uwzględnia moment i wielkość każdej wpłaty i wypłaty – Twój faktyczny wynik w skali roku. Od 90 dni historii.',
    dd: 'Największy spadek indeksu TWR od szczytu do dołka (bez wpływu wpłat i wypłat).',
    pd: 'Największy spadek zysku w złotych (wartość − wpłaty netto) od szczytu do dołka; wpłaty go nie zmieniają.',
    vol: 'Roczne odchylenie dziennych zwrotów (dni robocze × √252). Zwrot/ryzyko = roczny TWR / zmienność (bez stopy wolnej od ryzyka).',
    dy: 'Dywidendy netto z ostatnich 12 miesięcy / bieżąca wartość portfela (oraz do kosztu otwartych pozycji).',
    gain: 'Zysk = wartość portfela − wpłaty netto. Bez dywidend i nagród (to dochody dodatkowe, osobno w kafelku powyżej).',
  };
  const info = k => `<span class="info" data-t="${esc(INFO[k])}">i</span>`;
  function renderMetrics() {
    const m = S.met, note = $('#met-note');
    if (!m || !m.available) { note.textContent = (m && m.reason) || 'Wskaźniki niedostępne (za mało historii).'; $('#met-tiles').innerHTML = ''; return; }
    note.innerHTML = `Okres ${esc(m.first_date)} – ${esc(m.last_date)} (${m.days} dni).` + (m.days < 365 ? ' Historia krótsza niż rok – stopy roczne nie są liczone.' : '') + (m.estimated_days ? ` <span class="warn">${m.estimated_days} dni z oszacowanymi cenami.</span>` : '');
    const t = (l, k, v, s, c) => `<div class="m"><div class="l">${l} ${k ? info(k) : ''}</div><div class="v ${c || ''}">${v}</div><div class="s">${s || ''}</div></div>`;
    $('#met-tiles').innerHTML =
      t('Zysk od wpłat', 'gain', pct(m.gain_pct_on_purchases), zlS(m.gain) + ' przy wpłatach ' + zl0(m.total_purchases), cls(m.gain)) +
      t('Zwrot TWR od początku', 'twr', pct(m.twr_total), m.twr_annualized === null ? 'rocznie: od 365 dni' : 'rocznie (CAGR) ' + pct(m.twr_annualized), cls(m.twr_total)) +
      t('XIRR (rocznie)', 'xirr', pct(m.xirr), m.xirr_with_income === null ? 'wg Twoich wpłat i wypłat' : 'z dywidendami ' + pct(m.xirr_with_income), cls(m.xirr)) +
      t('Od początku roku', 'twr', pct(m.windows.YTD), '12 miesięcy: ' + pct(m.windows['12M']), cls(m.windows.YTD)) +
      t('Maks. obsunięcie', 'dd', pct(m.drawdown.depth), m.drawdown.peak + ' → ' + m.drawdown.trough + ' · teraz ' + pct(m.drawdown.current), 'neg') +
      t('Największy spadek zysku', 'pd', zl0(m.profit_drawdown.amount), m.profit_drawdown.peak ? m.profit_drawdown.peak + ' → ' + m.profit_drawdown.trough : '', 'neg') +
      t('Zmienność roczna', 'vol', m.volatility === null ? '—' : (m.volatility * 100).toLocaleString('pl-PL', {maximumFractionDigits: 1}) + '%', 'zwrot / ryzyko: ' + (m.return_to_risk === null ? '—' : m.return_to_risk.toLocaleString('pl-PL', {maximumFractionDigits: 2}))) +
      t('Stopa dywidendy (12 m)', 'dy', pct(m.dividend_yield), 'do kosztu: ' + pct(m.yield_on_cost) + ' · ' + zl(m.income_12m));
    const names = [['1M', '1 mies.'], ['3M', '3 mies.'], ['6M', '6 mies.'], ['YTD', 'od pocz. roku'], ['12M', '12 mies.']];
    const vals = names.map(([k]) => m.windows[k]).concat([m.twr_total]).filter(v => v !== null), mx = Math.max(0.0001, ...vals.map(Math.abs));
    $('#met-windows').innerHTML = names.concat([['ALL', 'od początku']]).map(([k, l]) => { const v = k === 'ALL' ? m.twr_total : m.windows[k];
      return `<div style="display:grid;grid-template-columns:110px 1fr 80px;gap:10px;align-items:center;font-size:.86rem;padding:3px 0"><span class="muted">${l}</span>
        <span style="position:relative;height:10px;background:var(--card2);border-radius:5px">${v === null ? '' : `<i style="position:absolute;${v >= 0 ? 'left:50%' : 'right:50%'};width:${Math.abs(v) / mx * 50}%;top:0;bottom:0;border-radius:5px;background:var(--${v >= 0 ? 'pos' : 'neg'})"></i>`}<i style="position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line2)"></i></span>
        <b class="num ${cls(v)}" style="text-align:right">${v === null ? '—' : pct(v)}</b></div>`; }).join('');
    $('#met-years tbody').innerHTML = m.years.slice().reverse().map(y => `<tr><td>${y.year}${y.partial ? ' <span class="muted">(część)</span>' : ''}</td><td class="n">${zl(y.start_value)}</td><td class="n">${zl(y.net_contributions)}</td><td class="n">${zl(y.end_value)}</td><td class="n ${cls(y.gain)}">${zlS(y.gain)}</td><td class="n ${cls(y.twr)}">${pct(y.twr)}</td></tr>`).join('');
    $('#met-episodes tbody').innerHTML = m.drawdown_episodes.map(e => `<tr><td>${e.peak}</td><td>${e.trough}</td><td class="n neg">${pct(e.depth)}</td><td class="n neg">${zlS(e.profit_change)}</td><td>${e.ongoing ? '<span class="badge warn">trwa</span>' : e.recovered}</td></tr>`).join('');
  }

  // ---------------------------------------------------------------- dochody
  async function loadIncome() {
    try { S.inc = await getJSON('/api/dochody?scope=' + SCOPE); renderIncome(); } catch (e) { $('#inc-chart').textContent = 'Nie udało się wczytać dochodów.'; }
  }
  function renderIncome() {
    if (!S.inc) return;
    let months = S.inc.months.map(r => ({label: r.month, dividends: +r.dividends, interest: +r.interest, rewards: +r.rewards, fees: +r.fees}));
    if (months.length) {                                    // uzupelnij brakujace miesiace zerami (os czasu bez przerw)
      const out = [], [y0, m0] = months[0].label.split('-').map(Number), now = new Date(), map = Object.fromEntries(months.map(r => [r.label, r]));
      for (let y = y0, m = m0; y < now.getFullYear() || (y === now.getFullYear() && m <= now.getMonth() + 1); m++) { if (m > 12) { m = 1; y++; } const k = y + '-' + String(m).padStart(2, '0'); out.push(map[k] || {label: k, dividends: 0, interest: 0, rewards: 0, fees: 0}); if (y === now.getFullYear() && m === now.getMonth() + 1) break; }
      months = S.incAll ? out : out.slice(-24);
    }
    const M = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'];
    Charts.bars($('#inc-chart'), months, [{key: 'dividends', label: 'Dywidendy netto', color: 'var(--c4)'}, {key: 'interest', label: 'Odsetki netto', color: 'var(--c3)'},
      {key: 'rewards', label: 'Nagrody', color: 'var(--c6)'}, {key: 'fees', label: 'Opłaty', color: 'var(--c5)'}],
      {fmt: zl, fmtY: v => nf0.format(v), force: r => r.label.endsWith('-01'), label: r => { const [y, m] = r.label.split('-'); return m === '01' ? y : M[+m - 1]; }, title: r => { const [y, m] = r.label.split('-'); return M[+m - 1] + ' ' + y; }});
    const tot = months.reduce((a, r) => a + r.dividends + r.interest + r.rewards + r.fees, 0);
    $('#inc-note').textContent = (S.incAll ? 'Cały okres' : 'Ostatnie 24 miesiące') + ': ' + zl(tot) + ' (po podatkach pobranych przez brokera). Odsetki naliczone, ale jeszcze niewypłacone, nie są na wykresie.';
  }
  $('#inc-all').onclick = e => { S.incAll = !S.incAll; e.target.textContent = S.incAll ? 'Ostatnie 24 mies.' : 'Cały okres'; renderIncome(); };

  // ---------------------------------------------------------------- kontrola liczb
  async function loadAudit() {
    try {
      const a = await getJSON('/api/kontrola?scope=' + SCOPE); S.audit = a;
      const rows = a.checks || [], bad = rows.filter(c => c.status !== 'OK'), err = rows.some(c => c.status === 'BLAD');
      const badge = !rows.length ? '' : a.ok ? `<span class="badge ok">✓ ${rows.length}/${rows.length} zgodne</span>` : `<span class="badge ${err ? 'bad' : 'warn'}">${err ? '✗' : '!'} do sprawdzenia ${bad.length} z ${rows.length}</span>`;
      $('#audit-badge').innerHTML = badge; $('#audit-top').innerHTML = rows.length ? `<a href="#szczegoly">Kontrola liczb: ${badge}</a>` : '';
      const al = $('#audit-top a'); if (al.tagName === 'A') al.onclick = () => { if (viewOf() !== 'portfel') location.hash = '#/portfel'; $('#sec-audit').open = true; setTimeout(() => $('#sec-audit').scrollIntoView({behavior: 'smooth'}), 50); return false; };
      $('#audit tbody').innerHTML = rows.map((c, i) => `<tr><td style="white-space:normal">${esc(c.kontrola)}</td><td><span class="badge ${c.status === 'OK' ? 'ok' : c.status === 'BLAD' ? 'bad' : 'warn'}">${c.status === 'OK' ? '✓ zgodne' : c.status === 'BLAD' ? '✗ błąd' : '! uwaga'}</span></td><td class="muted" style="white-space:normal">${esc(c.szczegoly || '')}</td></tr>` +
        (c.przyczyna ? `<tr class="fix"><td colspan="3"><b>Prawdopodobna przyczyna:</b> ${esc(c.przyczyna)}<div class="acts">${(c.rozwiazania || []).map((r, j) => `<button class="btn ${j === 0 ? 'primary' : ''}" data-c="${i}" data-r="${j}">${esc(r.etykieta)}</button>`).join('')}</div></td></tr>` : '')).join('');
      $$('#audit tr.fix button').forEach(b => b.onclick = () => runFix(rows[+b.dataset.c].rozwiazania[+b.dataset.r], b));
      if (!a.ok && (err || bad.some(c => c.przyczyna))) $('#sec-audit').open = true;
    } catch (e) { $('#audit-badge').textContent = 'nie udało się wykonać kontroli'; }
  }

  // rozwiazania uwag z kontroli liczb: tylko istniejace mechanizmy (odpowiedz na pytanie, pobranie cen, przeliczenie z eksportow, wczytanie pliku)
  async function runFix(r, b) {
    if (r.akcja === 'import') { openDrawer(); return; }
    if (r.akcja === 'pytanie') {
      const q = document.querySelector(`.qcard[data-id="${CSS.escape(r.pytanie)}"]`);
      if (q) { q.scrollIntoView({behavior: 'smooth', block: 'center'}); q.classList.add('flash'); setTimeout(() => q.classList.remove('flash'), 1600); } else toast('To pytanie zostało już rozstrzygnięte.');
      return;
    }
    if (r.akcja === 'przelicz' && !confirm('Przeliczyć wszystko od nowa z wczytanych eksportów? (partie, korekty, klasyfikacja; dane z plików się nie zmieniają)')) return;
    b.disabled = true; const t = b.textContent; b.textContent = 'Pracuję…';
    try {
      if (r.akcja === 'odpowiedz') {
        const d = await post(`/api/pytania/${r.broker}/${r.pytanie}`, {odpowiedz: r.odpowiedz, pola: {}});
        toast(d.dzien ? `Zapisano: dzień podziału ${d.dzien} (${d.zrodlo_daty}) – przeliczam.` : 'Zapisano – przeliczam.'); setTimeout(() => location.reload(), 900);
      } else if (r.akcja === 'ceny') { await post('/api/ceny/odswiez'); toast('Pobieram ceny w tle – wynik pojawi się sam.'); }
      else if (r.akcja === 'przelicz') { await post('/api/przelicz'); toast('Przeliczono – odświeżam.'); setTimeout(() => location.reload(), 900); }
    } catch (e) { toast(e.message); } finally { b.disabled = false; b.textContent = t; }
  }

  // ---------------------------------------------------------------- pytania
  async function loadQuestions() {
    try {
      const d = await getJSON('/api/pytania'), box = $('#questions'); box.innerHTML = '';
      (d.pytania || []).forEach(q => {
        const c = document.createElement('div'); c.className = 'qcard'; c.dataset.id = q.id;
        c.innerHTML = `<h3>${esc(q.tytul)}</h3><p>${esc(q.tresc)}</p>` + ((q.pola || []).length ? '<div class="fields">' + q.pola.map(p => `<label>${esc(p.etykieta)}<input name="${esc(p.nazwa)}" type="${p.typ === 'date' ? 'date' : 'text'}" ${p.max ? 'max="' + esc(p.max) + '"' : ''}></label>`).join('') + '</div>' : '') +
          '<div class="acts">' + q.odpowiedzi.map((o, i) => `<button class="btn ${i === 0 ? 'primary' : ''}" data-a="${esc(o.wartosc)}" ${o.opis ? 'title="' + esc(o.opis) + '"' : ''}>${esc(o.etykieta)}</button>`).join('') + '</div>' +
          q.odpowiedzi.filter(o => o.opis).map(o => `<p class="muted small"><b>${esc(o.etykieta)}:</b> ${esc(o.opis)}</p>`).join('');
        c.querySelectorAll('button').forEach(b => b.onclick = async () => {
          const pola = {}; c.querySelectorAll('input').forEach(i => pola[i.name] = i.value);
          b.disabled = true;
          const r = await fetch(`/api/pytania/${q.broker}/${q.id}`, {method: 'POST', headers: {'X-Requested-With': 'invapp', 'Content-Type': 'application/json'}, body: JSON.stringify({odpowiedz: b.dataset.a, pola})});
          if (r.ok) {
            const w = await r.json().catch(() => ({}));
            toast(w.dzien ? `Zapisano: dzień podziału ${w.dzien} (${w.zrodlo_daty}) – przeliczam.` : 'Zapisano odpowiedź – przeliczam.');
            setTimeout(() => location.reload(), 900);
          } else { b.disabled = false; const e = await r.json(); toast(e.blad || 'Nie udało się zapisać odpowiedzi'); }
        });
        box.appendChild(c);
      });
    } catch (e) { /* brak pytan */ }
  }

  // ---------------------------------------------------------------- zakladki widokow (Portfel / Ekspozycja / Gotowka / Symulacje)
  const VIEWS = ['portfel', 'ekspozycja', 'gotowka', 'symulacje'];
  const viewOf = () => { const m = location.hash.match(/^#\/(\w+)/); return m && VIEWS.includes(m[1]) ? m[1] : 'portfel'; };
  function showView() {
    const v = viewOf();
    $$('.view').forEach(x => x.hidden = x.dataset.view !== v);
    $$('.vtab').forEach(a => a.classList.toggle('on', a.dataset.v === v));
    $('.secnav').style.display = v === 'portfel' ? '' : 'none';
    $$('nav.seg a').forEach(a => { a.href = a.href.split('#')[0] + (v === 'portfel' ? '' : '#/' + v); });     // zmiana zakresu zostaje w tej samej zakladce
    if (v === 'gotowka') loadCash();
    if (v === 'ekspozycja' && window.renderExposure) window.renderExposure();
    if (v === 'symulacje' && window.renderSim) window.renderSim();
    if (v === 'portfel') redrawAll();
  }
  window.addEventListener('hashchange', () => { if (location.hash.startsWith('#/')) { showView(); window.scrollTo(0, 0); } });

  // ---------------------------------------------------------------- ekspozycja (podzial wartosci papierow)
  const SECTOR_PL = {'Technology': 'Technologia', 'Financial Services': 'Finanse', 'Consumer Defensive': 'Dobra podstawowe', 'Consumer Cyclical': 'Dobra cykliczne',
    'Healthcare': 'Ochrona zdrowia', 'Industrials': 'Przemysł', 'Energy': 'Energia', 'Basic Materials': 'Surowce', 'Communication Services': 'Komunikacja i media',
    'Utilities': 'Użyteczność publiczna', 'Real Estate': 'Nieruchomości'};
  const MARKET = [[/^(WSE|Warsaw)/i, 'Polska (GPW)'], [/^(NASDAQ|Nasdaq|NYSE|NYQ|NMS|NGM|BATS|Cboe|OTC)/i, 'USA'], [/^(XETRA|Frankfurt|GER|Stuttgart|Munich)/i, 'Niemcy'],
    [/^(Amsterdam|AMS)/i, 'Holandia'], [/^(London|LSE|IOB)/i, 'Wielka Brytania'], [/^(Paris|PAR)/i, 'Francja'], [/^(Milan|MIL)/i, 'Włochy'], [/^(Swiss|SIX|Zurich)/i, 'Szwajcaria'],
    [/^(Madrid|MCE)/i, 'Hiszpania'], [/^(Brussels)/i, 'Belgia'], [/^(Lisbon)/i, 'Portugalia'], [/^(Vienna)/i, 'Austria'], [/^(Copenhagen)/i, 'Dania'], [/^(Stockholm)/i, 'Szwecja'],
    [/^(Oslo)/i, 'Norwegia'], [/^(Helsinki)/i, 'Finlandia'], [/^(Irish|Dublin)/i, 'Irlandia']];
  const marketOf = e => { if (!e) return 'brak danych'; const m = MARKET.find(([re]) => re.test(e)); return m ? m[1] : e; };
  const typeOf = p => { const t = (p.quote_type || '').toUpperCase(), a = (p.asset_class || '').toUpperCase(); return t === 'EQUITY' || (!t && a === 'STOCK') ? 'Akcje' : t === 'ETF' || a === 'ETF' ? 'ETF' : 'Inne'; };
  window.renderExposure = async () => {
    const box = $('#exp-grid'); box.innerHTML = '<span class="skeleton"></span>';
    let d; try { d = await getJSON('/api/ekspozycja?scope=' + SCOPE); } catch (e) { box.innerHTML = '<p class="warn">Nie udało się wczytać ekspozycji.</p>'; return; }
    const ps = (d.pozycje || []).map(p => Object.assign(p, {value: +p.value})).filter(p => p.value > 0), tot = ps.reduce((a, p) => a + p.value, 0);
    if (!ps.length) { box.innerHTML = '<p class="muted">Brak otwartych pozycji.</p>'; return; }
    const group = f => { const g = {}; ps.forEach(p => { const k = f(p); g[k] = (g[k] || 0) + p.value; }); return Object.entries(g).map(([label, value], i) => ({label, value})).sort((a, b) => b.value - a.value).map((x, i) => Object.assign(x, {color: PAL[i % PAL.length]})); };
    const dims = [
      ['Typ instrumentu', group(typeOf), 'akcje pojedynczych spółek vs fundusze ETF'],
      ['Sektor (akcje)', group(p => typeOf(p) === 'ETF' ? 'ETF (skład nierozpisany)' : (SECTOR_PL[p.sector] || p.sector || 'brak danych')), 'sektor spółki wg Yahoo Finance; dla ETF dostawca nie podaje składu'],
      ['Rynek notowania', group(p => marketOf(p.exchange || p.ref_exchange)), 'giełda, na której notowany jest instrument (nie kraj spółek w ETF)'],
      ['Waluta notowania', group(p => p.currency || 'brak danych'), 'waluta, w której notowany jest instrument – ryzyko kursowe wobec PLN'],
    ];
    const top = ps.slice(0, 10), top5 = ps.slice(0, 5).reduce((a, p) => a + p.value, 0) / tot;
    box.innerHTML = dims.map((x, i) => `<div><h3>${x[0]}</h3><div id="exp-d${i}"></div><p class="note">${x[2]}</p></div>`).join('') +
      `<div><h3>Koncentracja</h3><p class="muted" style="margin:0 0 8px;font-size:.86rem">Największa pozycja: <b>${esc(ps[0].ticker)}</b> ${pct(ps[0].value / tot, 1).replace('+', '')} · 5 największych: ${pct(top5, 1).replace('+', '')} · pozycji: ${ps.length}</p>` +
      top.map(p => `<div style="display:grid;grid-template-columns:110px 1fr 70px;gap:10px;align-items:center;font-size:.85rem;padding:2px 0"><span class="tick">${esc(p.ticker)}</span><span class="bar" style="width:100%;margin:0"><i style="width:${p.value / ps[0].value * 100}%"></i></span><b class="num" style="text-align:right">${pct(p.value / tot, 1).replace('+', '')}</b></div>`).join('') + '</div>';
    dims.forEach((x, i) => Charts.donut($('#exp-d' + i), x[1], {fmt: zl0, centerLabel: 'Papiery', size: 190}));
    $('#exp-note').textContent = `podział wartości papierów: ${zl(tot)} (bez lokat i gotówki) · sektory i giełdy wg Yahoo Finance`;
  };

  // ---------------------------------------------------------------- symulacje (tylko obliczenia w przegladarce; nic nie jest zapisywane)
  let simChart = null;
  const fnum = s => { const v = parseFloat(String(s).replace(/\s| /g, '').replace(',', '.')); return isNaN(v) ? 0 : v; };
  function simDefaults() {
    const s = (S.live && S.live.scope) || {}, m = S.met && S.met.available ? S.met : null, ser = (S.hist && S.hist.series) || [];
    let monthly = 0;                                                      // srednia miesieczna wplata netto z ostatnich 12 miesiecy
    if (ser.length > 30) { const last = ser[ser.length - 1], i0 = Math.max(0, ser.length - 366); monthly = Math.max(0, (+last.invested_net - +ser[i0].invested_net) / ((ser.length - 1 - i0) / 30.44)); }
    const r = m && m.twr_annualized !== null ? m.twr_annualized : m && m.xirr !== null ? m.xirr : 0.07;
    return {start: Math.round(+(s.total_value !== undefined ? s.total_value : s.assets_value) || 0), monthly: Math.round(monthly / 50) * 50, rate: Math.round(Math.min(Math.max(r, -0.2), 0.3) * 1000) / 10,
            twr: m && m.twr_annualized, xirr: m && m.xirr};
  }
  function project(start, monthly, rate, years, growth) {
    const rm = Math.pow(1 + rate, 1 / 12) - 1, out = [{m: 0, v: start, c: start}]; let v = start, c = start, pay = monthly;
    for (let k = 1; k <= Math.round(years * 12); k++) { v = v * (1 + rm) + pay; c += pay; if (k % 12 === 0) pay *= 1 + growth; out.push({m: k, v, c}); }
    return out;
  }
  window.renderSim = () => {
    const box = $('#sim');
    if (!box.dataset.ready) {
      const d = simDefaults();
      box.innerHTML = `<div class="card-h"><h2>Symulacje</h2><span class="hint">obliczenia orientacyjne – nic nie jest zapisywane ani wysyłane</span></div>
        <div class="seg" id="sim-mode" style="margin-bottom:14px"><button data-k="proj" class="on">Projekcja i cel</button><button data-k="idx">Co gdybym inwestował w indeks</button></div>
        <div id="sim-proj">
          <form class="iform" id="sim-form" style="border:0;padding:0;margin:0">
            <label>Wartość startowa (zł)<input name="start" inputmode="decimal" value="${d.start}"></label>
            <label>Wpłata miesięczna (zł)<input name="monthly" inputmode="decimal" value="${d.monthly}"></label>
            <label>Wzrost wpłat co rok (%)<input name="growth" inputmode="decimal" value="0"></label>
            <label>Stopa zwrotu rocznie (%)<input name="rate" inputmode="decimal" value="${String(d.rate).replace('.', ',')}"></label>
            <label>Lata<input name="years" inputmode="decimal" value="10"></label>
            <label>Inflacja (%)<input name="infl" inputmode="decimal" value="3"></label>
            <label>Cel (zł, opcjonalnie)<input name="goal" inputmode="decimal" placeholder="np. 1 000 000"></label>
            <label class="check" style="flex-direction:row;align-items:center;font-weight:600"><input type="checkbox" name="tax"> po podatku Belki 19%</label>
          </form>
          <p class="note">Podpowiedzi z Twojego portfela: TWR rocznie ${d.twr === null || d.twr === undefined ? '— (od roku historii)' : pct(d.twr)}, XIRR ${d.xirr === null || d.xirr === undefined ? '—' : pct(d.xirr)}, średnia wpłata netto z 12 mies. ${zl0(d.monthly)}. Scenariusze: stopa −3 / +3 punkty procentowe. Wpłata na koniec każdego miesiąca, kapitalizacja miesięczna.</p>
          <div class="mgrid" id="sim-tiles" style="margin-top:12px"></div>
          <div id="sim-chart" style="min-height:300px;margin-top:12px"></div>
          <div class="legend"><span><span class="sw" style="background:var(--c1)"></span>scenariusz bazowy</span><span><span class="sw" style="background:var(--c4)"></span>optymistyczny</span><span><span class="sw" style="background:var(--c5)"></span>pesymistyczny</span><span><span class="sw" style="background:var(--c2)"></span>suma wpłat</span></div>
        </div>
        <div id="sim-idx" style="display:none"></div>`;
    }
    if (!box.dataset.touched) {                                           // podpowiedzi z portfela (dane mogly dojsc po otwarciu zakladki)
      const d = simDefaults(), f = $('#sim-form');
      f.elements.start.value = d.start; f.elements.monthly.value = d.monthly; f.elements.rate.value = String(d.rate).replace('.', ',');
      f.parentNode.querySelector('.note').innerHTML = `Podpowiedzi z Twojego portfela: TWR rocznie ${d.twr === null || d.twr === undefined ? '— (od roku historii)' : pct(d.twr)}, XIRR ${d.xirr === null || d.xirr === undefined ? '—' : pct(d.xirr)}, średnia wpłata netto z 12 mies. ${zl0(d.monthly)}. Scenariusze: stopa −3 / +3 punkty procentowe. Wpłata na koniec każdego miesiąca, kapitalizacja miesięczna.`;
      box.dataset.ready = '1';
      $('#sim-form').onsubmit = e => e.preventDefault();
      box.querySelectorAll('#sim-form input').forEach(i => i.addEventListener('input', () => { box.dataset.touched = '1'; runSim(); }));
      box.querySelectorAll('#sim-mode button').forEach(b => b.onclick = () => { box.querySelectorAll('#sim-mode button').forEach(x => x.classList.toggle('on', x === b)); $('#sim-proj').style.display = b.dataset.k === 'proj' ? '' : 'none'; $('#sim-idx').style.display = b.dataset.k === 'idx' ? '' : 'none'; if (b.dataset.k === 'idx') runIdx(); else runSim(); });
    }
    if ($('#sim-idx').style.display === 'none') runSim(); else runIdx();
  };
  function runSim() {
    const f = $('#sim-form'), g = n => fnum(f.elements[n].value);
    const start = g('start'), monthly = g('monthly'), rate = g('rate') / 100, years = Math.min(Math.max(g('years'), 1), 60), infl = g('infl') / 100, growth = g('growth') / 100, goal = g('goal'), tax = f.elements.tax.checked;
    const base = project(start, monthly, rate, years, growth), opt = project(start, monthly, rate + 0.03, years, growth), pes = project(start, monthly, rate - 0.03, years, growth);
    const net = p => tax ? p.v - Math.max(0, p.v - p.c) * 0.19 : p.v, end = base[base.length - 1], real = net(end) / Math.pow(1 + infl, years);
    const now = new Date(), t0 = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    const pts = base.map((p, i) => ({t: new Date(new Date(t0).setMonth(new Date(t0).getMonth() + p.m)).getTime(), base: net(p), opt: net(opt[i]), pes: net(pes[i]), contrib: p.c}));
    let goalTxt = '';
    if (goal > 0) {
      const long = project(start, monthly, rate, 100, growth), hit = long.find(p => net(p) >= goal);
      const n = Math.round(years * 12), rm = Math.pow(1 + rate, 1 / 12) - 1, fvStart = start * Math.pow(1 + rm, n);
      const need = rm ? (goal - fvStart) * rm / (Math.pow(1 + rm, n) - 1) : (goal - start) / n;
      goalTxt = `<div class="m"><div class="l">Cel ${zl0(goal)}</div><div class="v">${hit ? (hit.m / 12).toLocaleString('pl-PL', {maximumFractionDigits: 1}) + ' lat' : 'ponad 100 lat'}</div><div class="s">przy obecnej wpłacie · aby zdążyć w ${years} lat: ${need <= 0 ? 'bez dalszych wpłat' : zl0(need) + ' / mies.'}${tax ? ' (przed podatkiem)' : ''}</div></div>`;
    }
    $('#sim-tiles').innerHTML = `<div class="m"><div class="l">Wartość za ${years} lat</div><div class="v">${zl0(net(end))}</div><div class="s">pesymistycznie ${zl0(net(pes[pes.length - 1]))} · optymistycznie ${zl0(net(opt[opt.length - 1]))}</div></div>
      <div class="m"><div class="l">Suma wpłat</div><div class="v">${zl0(end.c)}</div><div class="s">w tym wartość startowa ${zl0(start)}</div></div>
      <div class="m"><div class="l">Zysk</div><div class="v ${cls(net(end) - end.c)}">${sgn(net(end) - end.c) + zl0(net(end) - end.c)}</div><div class="s">${tax ? 'po podatku Belki 19% od zysku' : 'przed podatkiem (IKE/IKZE: bez Belki przy wypłacie zgodnej z zasadami)'}</div></div>
      <div class="m"><div class="l">W dzisiejszych złotych</div><div class="v">${zl0(real)}</div><div class="s">po inflacji ${(infl * 100).toLocaleString('pl-PL')}% rocznie</div></div>` + goalTxt;
    if (!simChart) simChart = new Charts.TimeChart($('#sim-chart'), {height: 300, navigator: false, minSpan: 31 * Charts.DAY, fmtY: zl0, series: [
      {key: 'opt', label: 'Optymistyczny', color: 'var(--c4)', width: 1.5}, {key: 'pes', label: 'Pesymistyczny', color: 'var(--c5)', width: 1.5},
      {key: 'contrib', label: 'Suma wpłat', color: 'var(--c2)', width: 1.5, dash: true}, {key: 'base', label: 'Bazowy', color: 'var(--c1)', area: true, width: 2.4}]});
    simChart.setData(pts);
  }
  function runIdx() {
    // te same wplaty netto (dzien i kwota z historii portfela) zainwestowane w indeks po cenie zamkniecia dnia (w PLN): ile byloby dzis?
    const box = $('#sim-idx'), h = S.hist, P = h && h.porownanie;
    if (!h || !P) { box.innerHTML = '<p class="muted">Brak danych indeksów – pobierają się w tle.</p>'; return; }
    const ser = h.series, now = S.live && S.live.scope ? +S.live.scope.assets_value : +ser[ser.length - 1].value;
    const rows = P.indeksy.map(b => {
      const lv = P.poziomy[b.key]; let units = 0, prevInv = 0, ok = true, last = null;
      ser.forEach((p, i) => { const f = +p.invested_net - prevInv; prevInv = +p.invested_net; if (lv[i]) last = lv[i]; if (Math.abs(f) > 1e-9) { if (!last) { ok = false; return; } units += f / last; } });
      const val = ok && last ? units * last : null;
      return {b, val, diff: val === null ? null : now - val};
    });
    const inv = +ser[ser.length - 1].invested_net;
    box.innerHTML = `<p class="note" style="margin-top:0">Każda Twoja wpłata netto (zakup = wpłata, sprzedaż = wypłata; dla kont z lokatą – przelewy) w tym samym dniu i tej samej kwocie trafia do indeksu po cenie zamknięcia w PLN, bez kosztów. Indeksy cenowe (bez dywidend), WIG20 = ETF Beta WIG20TR (z dywidendami). Porównanie z bieżącą wartością inwestycji (papiery + lokaty, bez gotówki z banków).</p>
      <div class="table-wrap"><table><thead><tr><th>Gdybym inwestował w</th><th class="n">Wartość dziś</th><th class="n">Zysk</th><th class="n">Mój portfel vs indeks</th></tr></thead><tbody>
      <tr><td><b>Mój portfel</b></td><td class="n"><b>${zl(now)}</b></td><td class="n ${cls(now - inv)}">${zlS(now - inv)}</td><td class="n">—</td></tr>
      ${rows.map(r => `<tr><td>${esc(r.b.label)} <span class="muted" style="font-size:.78rem">${esc(r.b.opis)}</span></td><td class="n">${r.val === null ? '<span class="muted">brak danych</span>' : zl(r.val)}</td><td class="n ${cls(r.val - inv)}">${r.val === null ? '—' : zlS(r.val - inv)}</td>
        <td class="n ${cls(r.diff)}">${r.diff === null ? '—' : (r.diff >= 0 ? 'lepiej o ' : 'gorzej o ') + zl(Math.abs(r.diff))}</td></tr>`).join('')}
      </tbody></table></div><p class="note">Wpłaty netto razem: ${zl(inv)}. Symulacja historyczna – nie gwarantuje przyszłych wyników.</p>`;
  }

  // ---------------------------------------------------------------- eksport: Excel (plik z serwera, okno "Zapisz jako") i PDF (drukowanie: "Zapisz jako PDF")
  $('#exp-xlsx').onclick = () => {
    const a = document.createElement('a'); a.href = '/api/eksport.xlsx?scope=' + SCOPE; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
    toast('Plik zawiera Twoje dane finansowe – zapisz go tylko tam, gdzie chcesz go mieć.');
  };
  $('#exp-pdf').onclick = () => {
    const v = viewOf(), t = document.title;
    if (v !== 'portfel') { location.hash = '#/portfel'; }
    document.title = `Mój Portfel – raport ${BOOT.title} ${new Date().toISOString().slice(0, 10)}`;   // domyslna nazwa pliku PDF
    $('#print-head').textContent = `Mój Portfel · ${BOOT.title} · stan na ${new Date().toLocaleString('pl-PL')}`;
    setTimeout(() => { redrawAll(); window.print(); document.title = t; }, 300);
  };

  // ---------------------------------------------------------------- gotowka
  const post = async (url, body) => {
    const r = await fetch(url, {method: 'POST', headers: {'X-Requested-With': 'invapp', 'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.blad || 'Nie udało się zapisać'); return d;
  };
  async function loadCash() {
    try { S.cash = await getJSON('/api/gotowka'); renderCash(); } catch (e) { $('#cash-tiles').innerHTML = '<p class="warn">Nie udało się wczytać gotówki (' + esc(e.message) + ').</p>'; }
  }
  function renderCash() {
    const c = S.cash, st = c.ustawienia, sumDep = (c.lokaty || []).reduce((a, d) => a + +d.value, 0);
    const t = (l, v, s) => `<div class="m"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s || ''}</div></div>`;
    const n = c.konta.length, kont = n === 1 ? 'konto' : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) ? 'konta' : 'kont';
    $('#cash-tiles').innerHTML = t('Konta bankowe', zl(c.suma_kont), (n ? n + ' ' + kont : 'dodaj konto poniżej') + ' · ' + (st.include_cash ? '<b>wliczane</b> do wartości' : 'niewliczane')) +
      t('Wolne środki XTB', zl(c.suma_wolnych), st.include_xtb_free ? '<b>wliczane</b> do wartości' : 'niewliczane') +
      (sumDep ? t('Lokaty u brokerów', zl(sumDep), 'zawsze wliczane (gotówka oprocentowana)') : '') +
      t('Razem gotówka', zl(+c.suma_kont + +c.suma_wolnych + sumDep), '');
    syncSettings(st);
    const sel = $('#cash-add select[name=waluta]'); if (!sel.options.length) sel.innerHTML = c.waluty.map(w => `<option>${w}</option>`).join('');
    const di = $('#cash-add input[name=na_dzien]'); if (!di.value) di.value = new Date().toISOString().slice(0, 10);
    $('#cash-acc tbody').innerHTML = c.konta.map(a => `<tr data-id="${a.id}"><td><b>${esc(a.nazwa)}</b></td><td>${esc(a.waluta)}</td><td class="n">${a.saldo === null ? '—' : nf2.format(a.saldo)}</td><td>${esc(a.na_dzien || '—')}</td>
      <td class="n">${a.wartosc_pln === null ? '<span class="warn">brak kursu</span>' : zl(a.wartosc_pln)}${a.kurs ? `<div class="muted" style="font-size:.75rem">kurs ${num(a.kurs, 4)}</div>` : ''}</td>
      <td class="n"><button class="btn ghost" data-a="edit">Zmień saldo</button> <button class="btn ghost" data-a="del">Usuń</button></td></tr>`).join('') ||
      '<tr><td colspan="6" class="muted">Brak kont. Dodaj konto bankowe, żeby widzieć całą swoją gotówkę w jednym miejscu.</td></tr>';
    $$('#cash-acc tr[data-id]').forEach(tr => {
      const id = tr.dataset.id, a = c.konta.find(k => String(k.id) === id);
      tr.querySelector('[data-a=del]').onclick = async () => {
        if (!confirm(`Usunąć konto „${a.nazwa}” z listy?`)) return;
        try { await post(`/api/gotowka/konto/${id}/usun`); toast('Usunięto konto.'); loadCash(); loadLive({fast: true}); } catch (e) { toast(e.message); }
      };
      tr.querySelector('[data-a=edit]').onclick = () => {
        tr.children[2].innerHTML = `<input name="saldo" inputmode="decimal" value="${esc(String(a.saldo).replace('.', ','))}">`;
        tr.children[3].innerHTML = `<input name="na_dzien" type="date" value="${new Date().toISOString().slice(0, 10)}">`;
        tr.children[5].innerHTML = '<button class="btn primary" data-a="save">Zapisz</button> <button class="btn ghost" data-a="cancel">Anuluj</button>';
        tr.querySelector('[data-a=cancel]').onclick = renderCash;
        tr.querySelector('[data-a=save]').onclick = async () => {
          try { await post(`/api/gotowka/konto/${id}/saldo`, {saldo: tr.querySelector('input[name=saldo]').value, na_dzien: tr.querySelector('input[name=na_dzien]').value}); toast('Zapisano saldo.'); loadCash(); loadLive({fast: true}); }
          catch (e) { toast(e.message); }
        };
      };
    });
    const rows = (c.wolne_xtb || []).map(w => `<tr><td>${esc(w.konto)}</td><td>XTB · wolne środki</td><td class="n">${zl(w.saldo)}</td><td class="muted">saldo gotówki z eksportu (wpłaty, zakupy, sprzedaże, dywidendy, podatki, opłaty)</td></tr>`)
      .concat((c.lokaty || []).map(d => `<tr><td>${esc(d.account)}</td><td>${esc(d.broker)} · lokata ${(d.rate * 100).toLocaleString('pl-PL', {maximumFractionDigits: 2})}%</td><td class="n">${zl(d.value)}</td><td class="muted">saldo z eksportu na ${esc(d.ledger_as_of)} · zawsze w wartości portfela</td></tr>`));
    $('#cash-brk tbody').innerHTML = rows.join('') || '<tr><td colspan="4" class="muted">Brak danych.</td></tr>';
  }
  // ustawienia (te same przelaczniki w zakladce Gotowka i w oknie ⚙): zapis, potem przeliczenie widoku
  function syncSettings(st) {
    if (!st) return; S.settings = st;
    $$('input[data-set]').forEach(i => { i.checked = !!st[i.dataset.set]; });
    $$('#set-modal input[type=radio]').forEach(i => { i.checked = st[i.name] === i.value; });
  }
  async function saveSetting(key, value) {
    const d = await post('/api/ustawienia', {klucz: key, wartosc: value}); syncSettings(d.ustawienia);
    await loadLive({fast: true}); loadAudit(); if (key === 'price_source' || key === 'fx_source') loadHistory();
    if (viewOf() === 'gotowka') loadCash();
  }
  $$('input[data-set]').forEach(i => i.onchange = async () => {
    try { await saveSetting(i.dataset.set, i.checked); toast(i.checked ? 'Włączono – wartość portfela obejmuje teraz tę gotówkę.' : 'Wyłączono.'); }
    catch (e) { i.checked = !i.checked; toast(e.message); }
  });
  $$('#set-modal input[type=radio]').forEach(i => i.onchange = async () => {
    try { await saveSetting(i.name, i.value); toast('Zapisano – wycena przeliczona.'); } catch (e) { toast(e.message); syncSettings(S.settings); }
  });
  $('#settings-btn').onclick = async () => {
    try { syncSettings((await getJSON('/api/gotowka')).ustawienia); } catch (e) { /* baza niedostepna */ }
    $('#set-modal').style.display = '';
  };
  $('#set-close').onclick = () => { $('#set-modal').style.display = 'none'; };
  $('#set-modal').addEventListener('click', e => { if (e.target.id === 'set-modal') $('#set-modal').style.display = 'none'; });
  $('#cash-add').onsubmit = async e => {
    e.preventDefault(); const f = e.target, b = f.querySelector('button'); b.disabled = true;
    try { await post('/api/gotowka/konto', Object.fromEntries(new FormData(f))); f.reset(); toast('Dodano konto.'); loadCash(); loadLive({fast: true}); }
    catch (err) { toast(err.message); } finally { b.disabled = false; }
  };

  // ---------------------------------------------------------------- import (panel boczny)
  const drawer = $('#drawer'), dbg = $('#drawer-bg');
  const openDrawer = () => { drawer.classList.add('open'); dbg.style.display = 'block'; };
  const closeDrawer = () => { drawer.classList.remove('open'); dbg.style.display = 'none'; };
  $$('.open-import').forEach(b => b.onclick = openDrawer); $('#drawer-x').onclick = closeDrawer; dbg.onclick = closeDrawer;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  const input = $('#files'), drop = $('#drop');
  $('#pick').onclick = () => input.click(); input.onchange = () => handle(Array.from(input.files));
  ['dragover', 'dragenter'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', ev => handle(Array.from(ev.dataTransfer.files)));
  document.addEventListener('dragover', ev => { if (ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('Files')) { ev.preventDefault(); openDrawer(); } });
  let busy = false;
  async function handle(files) {
    if (!files.length || busy) return; busy = true; $('#pick').disabled = true;
    const out = $('#import-results'); out.innerHTML = ''; let ok = 0;
    for (const f of files) {
      const row = document.createElement('div'); row.className = 'res'; row.innerHTML = `<b>${esc(f.name)}</b> – <span class="muted">wczytywanie…</span>`; out.appendChild(row);
      try {
        const r = await fetch('/api/import', {method: 'POST', headers: {'X-Requested-With': 'invapp', 'X-Filename': encodeURIComponent(f.name), 'Content-Type': 'application/octet-stream'}, body: f});
        const d = await r.json();
        if (!r.ok) { row.innerHTML = `<b>${esc(f.name)}</b> – <span class="neg">nie wczytano:</span> ${esc(d.blad || 'błąd')} <span class="muted">(nic nie zostało zapisane)</span>`; continue; }
        ok++;
        let h = `<b>${esc(d.plik)}</b> – <span class="pos">wczytano</span>: nowych ${d.nowe}, duplikatów ${d.duplikaty}` + (d.konflikty ? `, <span class="warn">konfliktów ${d.konflikty}</span>` : '') + (d.niezmapowane ? `, <span class="warn">nierozpoznanych ${d.niezmapowane}</span>` : '');
        if (d.nowe_konta.length) h += '<div class="note">Nowe konta: ' + d.nowe_konta.map(esc).join(', ') + '</div>';
        if (d.nowe === 0 && d.duplikaty > 0) h += '<div class="note">Ten plik był już wczytany – nic się nie zmieniło.</div>';
        const bad = d.kontrole.filter(k => k.status !== 'OK'); if (bad.length) h += '<div class="note warn">Kontrole do sprawdzenia: ' + bad.map(k => esc(k.kontrola)).join('; ') + '</div>';
        if ((d.informacje || []).length) h += '<div class="note">' + d.informacje.map(esc).join('<br>') + '</div>';
        if (d.ostrzezenia.length) h += `<details><summary class="muted" style="cursor:pointer">Ostrzeżenia (${d.ostrzezenia.length})</summary><div class="note">${d.ostrzezenia.map(esc).join('<br>')}</div></details>`;
        row.innerHTML = h;
      } catch (e) { row.innerHTML = `<b>${esc(f.name)}</b> – <span class="neg">brak połączenia z aplikacją</span>`; }
    }
    input.value = ''; busy = false; $('#pick').disabled = false;
    if (ok) { const n = document.createElement('div'); n.className = 'note'; n.innerHTML = '<b>Gotowe.</b> Odświeżam widok; ceny i historia dociągają się w tle (pasek u góry).'; out.appendChild(n); setTimeout(() => location.reload(), 1500); }
  }

  // usuniecie wczytanego pliku (stan jak bez tego pliku; kopia zapasowa przed zmiana)
  $$('.del-batch').forEach(b => b.onclick = async () => {
    if (!confirm(`Usunąć plik „${b.dataset.name}” z portfela?\n\nZniknie to, co pochodzi tylko z tego pliku (operacje obecne też w innym wczytanym pliku zostaną). Przed usunięciem zrobię kopię zapasową bazy. Plik możesz później wczytać ponownie.`)) return;
    b.disabled = true; b.textContent = 'Usuwam…';
    try {
      const r = await fetch(`/api/import/${b.dataset.id}/usun`, {method: 'POST', headers: {'X-Requested-With': 'invapp'}}), d = await r.json();
      if (!r.ok) { toast(d.blad || 'Nie udało się usunąć pliku'); b.disabled = false; b.textContent = 'Usuń'; return; }
      toast(`Usunięto: ${d.usuniete_operacje} operacji` + (d.usuniete_konta.length ? `, konta: ${d.usuniete_konta.join(', ')}` : '') + '.');
      setTimeout(() => location.reload(), 900);
    } catch (e) { toast('Brak połączenia z aplikacją'); b.disabled = false; b.textContent = 'Usuń'; }
  });

  // ---------------------------------------------------------------- status zadania w tle (ceny, historia)
  async function pollStatus() {
    try {
      const s = await getJSON('/api/import/status'), st = $('#status');
      if (s.state === 'running') {
        st.className = 'status busy'; st.querySelector('span:last-child').textContent = `Aktualizuję ceny ${s.step_no || ''}/${s.steps || ''}: ${s.step}`;
        $('#bg-progress').style.display = ''; $('#bg-progress i').style.width = (s.steps ? (s.step_no - 1) / s.steps * 100 : 5) + '%'; $('#bg-step').textContent = s.step;
      } else {
        st.className = 'status' + (s.state === 'error' ? ' err' : ''); st.querySelector('span:last-child').textContent = s.state === 'error' ? 'Ceny nie zostały pobrane' : 'Dane aktualne';
        $('#bg-progress').style.display = 'none';
      }
      if (S.version !== null && s.version !== S.version) { loadLive({fast: true}); loadHistory(); loadAudit(); if (s.state !== 'running') loadQuestions(); }
      S.version = s.version;
      setTimeout(pollStatus, s.state === 'running' ? 2000 : 15000);
    } catch (e) { setTimeout(pollStatus, 5000); }
  }

  // ---------------------------------------------------------------- aktualizacje (wydania na GitHubie) i zamykanie aplikacji
  // Nowa wersja: pytanie "Zaktualizowac?" (raz na uruchomienie). "Nie teraz" -> przycisk "⬆ Aktualizacja" w naglowku zostaje, wiec mozna to zrobic pozniej.
  let UPD = null;
  const updDismissed = v => { try { return sessionStorage.getItem('invapp-upd-later') === v; } catch (e) { return false; } };
  const openUpd = () => { if (UPD) $('#upd-modal').style.display = ''; };
  const closeUpd = () => { $('#upd-modal').style.display = 'none'; };
  function showUpdate(u, ask) {
    UPD = u;
    $('#upd-btn').style.display = ''; $('#upd-btn').textContent = '⬆ Aktualizacja ' + u.najnowsza;
    $('#upd-title').textContent = 'Dostępna nowa wersja ' + u.najnowsza;
    $('#upd-sub').textContent = 'Masz wersję ' + u.wersja + '. Zaktualizować teraz?';
    $('#upd-notes').textContent = (u.notatki || '').replace(/SHA256:\s*[0-9a-fA-F]{64}/g, '').trim();
    const pg = $('#upd-page'); pg.style.display = u.strona ? '' : 'none'; if (u.strona) pg.href = u.strona;
    if (ask) openUpd();
  }
  async function loadUpdate(force) {
    try {
      const u = await getJSON('/api/aktualizacja' + (force ? '?force=true' : ''));
      if (!u.dostepna) { $('#upd-btn').style.display = 'none'; UPD = null; return u; }
      showUpdate(u, force || !updDismissed(u.najnowsza));
      return u;
    } catch (e) { return null; }
  }
  $('#upd-btn').onclick = openUpd;
  $('#upd-later').onclick = () => { try { sessionStorage.setItem('invapp-upd-later', UPD.najnowsza); } catch (e) { /* brak pamieci */ } closeUpd(); toast('Dobrze – zaktualizujesz później przyciskiem „⬆ Aktualizacja” u góry.'); };
  $('#upd-modal').addEventListener('click', e => { if (e.target.id === 'upd-modal') $('#upd-later').click(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('#upd-modal').style.display === '') $('#upd-later').click(); });
  $('#upd-check').onclick = async e => {
    e.preventDefault(); toast('Sprawdzam aktualizacje…');
    const u = await loadUpdate(true);
    if (!u) toast('Nie udało się sprawdzić aktualizacji (brak internetu?).');
    else if (!u.dostepna) toast(u.powod || ('Masz najnowszą wersję (' + u.wersja + ').'));
  };
  $('#upd-go').onclick = async () => {
    const b = $('#upd-go'); b.disabled = true; $('#upd-later').disabled = true; b.textContent = 'Pobieram…';
    try {
      const r = await fetch('/api/aktualizacja/instaluj', {method: 'POST', headers: {'X-Requested-With': 'invapp'}});
      const d = await r.json();
      if (!r.ok) throw new Error(d.blad || 'Nie udało się pobrać aktualizacji');
      b.textContent = 'Instaluję… (aplikacja uruchomi się ponownie)'; waitForVersion(d.wersja);
    } catch (e) { b.disabled = false; $('#upd-later').disabled = false; b.textContent = 'Zaktualizuj teraz'; toast(e.message); }
  };
  function waitForVersion(v) {
    let n = 0;
    const tick = async () => {
      try { const h = await getJSON('/zdrowie'); if (h.wersja === v) { location.reload(); return; } } catch (e) { /* aplikacja w trakcie restartu */ }
      if (++n < 150) setTimeout(tick, 2000); else toast('Instalacja trwa dłużej niż zwykle – otwórz aplikację ze skrótu.');
    };
    setTimeout(tick, 4000);
  }
  $('#quit').onclick = async () => {
    if (!confirm('Zamknąć aplikację? Zatrzymam serwer i bazę danych. Uruchomisz ją ponownie skrótem „' + document.title + '”.')) return;
    try { await fetch('/api/zamknij', {method: 'POST', headers: {'X-Requested-With': 'invapp'}}); } catch (e) { /* juz zamknieta */ }
    document.body.innerHTML = '<div style="max-width:520px;margin:15vh auto;text-align:center;font:16px system-ui;color:var(--ink)"><div style="font-size:2.4rem">⏻</div><h2>Aplikacja zamknięta</h2><p style="color:var(--muted)">Możesz zamknąć tę kartę. Dane są zapisane na tym komputerze.</p></div>';
  };

  function redrawAll() { if (main) main.render(); if (under) under.render(); renderAlloc(); renderIncome(); }
  $('#refresh').onclick = async () => { $('#refresh').disabled = true; await loadLive({force: true}); $('#refresh').disabled = false; toast('Ceny odświeżone.'); };

  // ---------------------------------------------------------------- start: najpierw zapisane ceny (szybko), potem swieze
  if (BOOT.has_data) {
    loadLive({fast: true}).then(() => loadLive());
    loadHistory(); loadIncome(); loadAudit();
    setInterval(() => loadLive(), 60000);
  }
  if (BOOT.has_data) showView();
  loadQuestions(); pollStatus(); loadUpdate();
  setInterval(() => loadUpdate(), 3 * 3600 * 1000);          // aplikacja otwarta dlugo: nowa wersja tez sie pokaze (serwer pyta GitHuba co 6 h)
  if (BOOT.empty) openDrawer();
})();
