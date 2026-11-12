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
    } catch (e) { $('#price-note').innerHTML = '<span class="warn">Nie udało się pobrać wartości (brak połączenia z bazą lub internetem).</span>'; }
  }
  function renderKpis() {
    const s = S.live && S.live.scope; if (!s) return;
    const assets = s.assets_value, total = s.total_result_live, base = assets - total;
    const m = S.met && S.met.available ? S.met : null;
    $('#k-value').textContent = zl(assets);
    $('#k-value-s').innerHTML = s.deposits_value ? 'papiery ' + zl(s.securities_value) + ' · lokaty ' + zl(s.deposits_value) : 'ilość × aktualna cena z Yahoo × kurs';
    $('#k-day').innerHTML = m && m.day_change !== null ? `<span class="chip">${zlS(m.day_change)} (${pct(m.day_change_pct)}) od ${m.day_change_from.slice(8, 10)}.${m.day_change_from.slice(5, 7)}</span>` : '';
    $('#k-total').innerHTML = `<span class="${cls(total)}">${zlS(total)}</span>`;
    $('#k-total-s').innerHTML = (base > 0 ? `<span class="chip ${cls(total)}">${pct(total / base)}</span> od wpłaconych ` + zl0(base) : '') ;
    $('#k-inv').innerHTML = `<span class="${cls(s.investment_gain)}">${zlS(s.investment_gain)}</span>`;
    $('#k-inv-s').textContent = 'niezrealizowany ' + zlS(s.unrealized_gain) + ' · zrealizowany ' + zlS(s.realized_gain);
    const inc = s.additional_income;
    $('#k-inc').innerHTML = `<span class="${cls(inc)}">${zlS(inc)}</span>`;
    $('#k-inc-s').textContent = 'dywidendy ' + zl(s.dividends_net) + ' · odsetki ' + zl(s.interest_net) + (s.rewards ? ' · nagrody ' + zl(s.rewards) : '') + (s.fees ? ' · opłaty ' + zl(s.fees) : '');
    const missing = (s.snapshot_positions || 0) + (s.missing_positions || 0);
    $('#price-note').innerHTML = (s.positions ? 'Ceny z Yahoo Finance: ' + when(s.oldest_quote) + (s.oldest_quote !== s.newest_quote ? ' – ' + when(s.newest_quote) : '') + ' · kursy walut na żywo · odświeżanie co minutę' : 'Brak otwartych pozycji') +
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
      const market = r.price_source === 'MARKET', unit = r.price_unit === 'MINOR_1_100' ? ' (pensy)' : '';
      return `<tr data-i="${i}" style="cursor:pointer"><td><span class="tick">${esc(r.ticker)}</span><span class="inst">${esc(r.instrument)}</span></td>
        <td>${(r.acct || '').split(', ').filter(Boolean).map(a => `<span class="badge">${esc(a)}</span>`).join(' ')}</td>
        <td class="n">${num(r.qty)}</td><td class="n">${market && r.price_pln !== null ? zl(r.price_pln) : '<span class="warn">brak ceny</span>'}</td>
        <td class="n"><b>${r.value === null ? '—' : zl(r.value)}</b></td>
        <td class="n">${r.share === null ? '—' : pct(r.share, 1).replace('+', '')}<span class="bar"><i style="width:${(r.share || 0) / maxShare * 100}%"></i></span></td>
        <td class="n">${zl(r.cost_left)}</td><td class="n ${cls(r.gain)}">${zlS(r.gain)}</td><td class="n ${cls(r.gainp)}">${pct(r.gainp, 1)}</td></tr>
        <tr class="sub" data-sub="${i}" style="display:none"><td colspan="9">
          ${market ? `Cena na giełdzie: <b>${num(r.price, 4)} ${esc(r.currency)}${unit}</b> × ilość = ${num(r.value_native, 2)} ${esc(r.currency)}` + (r.currency !== 'PLN' ? ` · kurs ${esc(r.currency)}/PLN <b>${num(r.fx_used, 4)}</b> (${r.fx_source === 'NBP_A' ? 'NBP' : 'Yahoo'})` : '') + ` · cena z ${when(r.price_time)}${r.market_state ? ' (' + esc(r.market_state) + ')' : ''}` : '<span class="warn">Brak ceny rynkowej z API – wartość niepewna (cena z raportu brokera albo brak).</span>'}
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
      items = Object.entries(g).map(([label, value]) => ({label, value})).sort((a, b) => b.value - a.value);
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
  const MODES = {
    value: {series: () => [{key: 'value', label: 'Wartość portfela', color: 'var(--c1)', area: true}, {key: 'invested_net', label: 'Wpłaty netto', color: 'var(--c2)', width: 2}]
      .concat(S.hist && S.hist.series.some(p => Number(p.deposits)) ? [{key: 'deposits', label: 'w tym lokaty', color: 'var(--c3)', width: 1.5, dash: true}] : []), fmt: zl0},
    gain: {series: () => [{key: 'gain', label: 'Zysk (wartość − wpłaty netto)', color: 'var(--c4)', signed: true}], fmt: zl0},
    twr: {series: () => [{key: 'twr', label: 'Zwrot TWR od początku', color: 'var(--c3)', signed: true}], fmt: v => pct(v, Math.abs(v) < 0.1 ? 1 : 0)},
  };
  function mainPoints() {
    if (!S.hist || !S.hist.series) return [];
    const idx = {}; if (S.met && S.met.index_series) S.met.index_series.forEach(([d, i]) => idx[d] = i - 1);
    const pts = S.hist.series.map(p => ({t: new Date(p.date + 'T00:00:00').getTime(), date: p.date, value: +p.value, invested_net: +p.invested_net, deposits: +p.deposits, securities: +p.securities,
      gain: +p.gain, invested_gross: +p.invested_gross, sold: +p.sold, positions: p.positions, est: p.estimated_positions, twr: idx[p.date] !== undefined ? idx[p.date] : null}));
    const s = S.live && S.live.scope;
    if (s && pts.length) { const last = pts[pts.length - 1]; last.value = +s.assets_value; last.gain = last.value - last.invested_net; last.securities = +s.securities_value; last.deposits = +s.deposits_value; }
    return pts;
  }
  function tipMain(p) {
    const r = (c, l, v) => `<div class="r"><span><span class="sw" style="background:${c}"></span>${l}</span><b>${v}</b></div>`;
    let h = '<b>' + Charts.fmtDate(p.t) + '</b>';
    if (S.mode === 'value') h += r('var(--c1)', 'Wartość', zl(p.value)) + (p.deposits ? r('var(--c3)', '· papiery', zl(p.securities)) + r('var(--c3)', '· lokaty', zl(p.deposits)) : '') + r('var(--c2)', 'Wpłaty netto', zl(p.invested_net));
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
    main.o.series = MODES[S.mode].series(); main.o.fmtY = MODES[S.mode].fmt;
    const pts = mainPoints(); main.setData(pts, true); renderLegend();
    if (!under && S.met && S.met.index_series) {
      under = new Charts.TimeChart($('#under'), {height: 150, navigator: false, series: [{key: 'dd', label: 'Obsunięcie od szczytu', color: 'var(--neg)', area: true}], fmtY: v => pct(v, 0)});
      under.onWindow = (a, b) => main.setWindow(a, b);
    }
    if (under && S.met && S.met.index_series) under.setData(S.met.index_series.map(([d, i, dd]) => ({t: new Date(d + 'T00:00:00').getTime(), dd})), true);
    applyRange(S.range, true);
    const h = S.hist, last = pts[pts.length - 1];
    $('#chart-note').innerHTML = !pts.length ? 'Brak danych do wykresu.' : `Od ${esc(h.series[0].date)}: wartość ${zl(last.value)}, wpłaty netto ${zl(last.invested_net)}, zysk <span class="${cls(last.gain)}">${zlS(last.gain)}</span>.` +
      (h.estimated_days ? ` <span class="warn">${h.estimated_days} z ${h.series.length} dni ma oszacowane ceny części pozycji (brak historii instrumentu).</span>` : '');
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
      $('#audit-badge').innerHTML = badge; $('#audit-top').innerHTML = rows.length ? `<a href="#szczegoly" onclick="document.getElementById('sec-audit').open=true">Kontrola liczb: ${badge}</a>` : '';
      $('#audit tbody').innerHTML = rows.map(c => `<tr><td style="white-space:normal">${esc(c.kontrola)}</td><td><span class="badge ${c.status === 'OK' ? 'ok' : c.status === 'BLAD' ? 'bad' : 'warn'}">${c.status === 'OK' ? '✓ zgodne' : c.status === 'BLAD' ? '✗ błąd' : '! uwaga'}</span></td><td class="muted" style="white-space:normal">${esc(c.szczegoly || '')}</td></tr>`).join('');
      if (!a.ok && err) $('#sec-audit').open = true;
    } catch (e) { $('#audit-badge').textContent = 'nie udało się wykonać kontroli'; }
  }

  // ---------------------------------------------------------------- pytania
  async function loadQuestions() {
    try {
      const d = await getJSON('/api/pytania'), box = $('#questions'); box.innerHTML = '';
      (d.pytania || []).forEach(q => {
        const c = document.createElement('div'); c.className = 'qcard';
        c.innerHTML = `<h3>${esc(q.tytul)}</h3><p>${esc(q.tresc)}</p>` + ((q.pola || []).length ? '<div class="fields">' + q.pola.map(p => `<label>${esc(p.etykieta)}<input name="${esc(p.nazwa)}" type="${p.typ === 'date' ? 'date' : 'text'}" ${p.max ? 'max="' + esc(p.max) + '"' : ''}></label>`).join('') + '</div>' : '') +
          '<div class="acts">' + q.odpowiedzi.map((o, i) => `<button class="btn ${i === 0 ? 'primary' : ''}" data-a="${esc(o.wartosc)}">${esc(o.etykieta)}</button>`).join('') + '</div>';
        c.querySelectorAll('button').forEach(b => b.onclick = async () => {
          const pola = {}; c.querySelectorAll('input').forEach(i => pola[i.name] = i.value);
          b.disabled = true;
          const r = await fetch(`/api/pytania/${q.broker}/${q.id}`, {method: 'POST', headers: {'X-Requested-With': 'invapp', 'Content-Type': 'application/json'}, body: JSON.stringify({odpowiedz: b.dataset.a, pola})});
          if (r.ok) { toast('Zapisano odpowiedź – przeliczam.'); setTimeout(() => location.reload(), 600); } else { b.disabled = false; const e = await r.json(); toast(e.blad || 'Nie udało się zapisać odpowiedzi'); }
        });
        box.appendChild(c);
      });
    } catch (e) { /* brak pytan */ }
  }

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
  async function loadUpdate() {
    try {
      const u = await getJSON('/api/aktualizacja'), box = $('#update-banner');
      if (!u.dostepna) { box.innerHTML = ''; return; }
      box.innerHTML = `<div class="upd"><div><b>Dostępna nowa wersja ${esc(u.najnowsza)}</b> <span class="muted">(masz ${esc(u.wersja)})</span><br>
        <span class="muted">Instalacja trwa około minuty: aplikacja zamknie się, zainstaluje nową wersję i uruchomi ponownie. Twoje dane zostają (przed zmianą schematu bazy robi się kopia zapasowa).</span></div>
        <span class="spacer"></span>${u.strona ? `<a class="btn ghost" href="${esc(u.strona)}" target="_blank" rel="noopener">Co nowego</a>` : ''}<button class="btn primary" id="upd-go">Zainstaluj aktualizację</button></div>`;
      $('#upd-go').onclick = async () => {
        const b = $('#upd-go'); b.disabled = true; b.textContent = 'Pobieram…';
        const r = await fetch('/api/aktualizacja/instaluj', {method: 'POST', headers: {'X-Requested-With': 'invapp'}});
        const d = await r.json();
        if (!r.ok) { b.disabled = false; b.textContent = 'Zainstaluj aktualizację'; toast(d.blad || 'Nie udało się pobrać aktualizacji'); return; }
        b.textContent = 'Instaluję…'; waitForVersion(d.wersja);
      };
    } catch (e) { /* brak informacji o aktualizacjach */ }
  }
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
  loadQuestions(); pollStatus(); loadUpdate();
  if (BOOT.empty) openDrawer();
})();
