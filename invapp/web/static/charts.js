/* Wykresy aplikacji (SVG, bez zewnetrznych bibliotek - dziala offline):
 *  TimeChart - szereg czasowy z przyblizaniem: zaznacz zakres myszka (przeciagnij), kolko myszy = przyblizenie wokol kursora,
 *              Shift + przeciaganie = przesuwanie, podwojne klikniecie = caly okres, suwak (nawigator) pod wykresem;
 *              opcja markers: [{t, v, kind: 'buy'|'sell'}] - znaczniki transakcji.
 *  donut     - wykres pierscieniowy (alokacja) z legenda i podswietleniem.
 *  bars      - slupki miesieczne (warstwowe, z wartosciami ujemnymi ponizej osi).
 */
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const MONTHS = ['sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'];
  const DAY = 86400000;
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function niceStep(span, count) {
    const raw = span / Math.max(1, count), p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }
  function yTicks(min, max, count) {
    const st = niceStep(max - min || 1, count), out = [];
    for (let v = Math.ceil(min / st) * st; v <= max + st * 1e-9; v += st) out.push(+v.toFixed(10));
    return out;
  }
  function xTicks(t0, t1, px) {
    const span = t1 - t0, want = Math.max(2, Math.floor(px / 95)), out = [], d0 = new Date(t0);
    if (span / DAY > 900) {                                        // lata
      const stepY = Math.max(1, Math.ceil(span / DAY / 365 / want));
      for (let y = d0.getFullYear(); ; y++) { const t = new Date(y, 0, 1).getTime(); if (t > t1) break; if (t >= t0 && (y % stepY === 0)) out.push([t, String(y)]); }
    } else if (span / DAY > 50) {                                  // miesiace
      const months = Math.round(span / DAY / 30.4), stepM = [1, 2, 3, 4, 6, 12].find(s => months / s <= want) || 12;
      for (let y = d0.getFullYear(), m = d0.getMonth(); ; m++) {
        const d = new Date(y, m, 1), t = d.getTime(); if (t > t1) break;
        if (t >= t0 && d.getMonth() % stepM === 0) out.push([t, d.getMonth() === 0 ? String(d.getFullYear()) : MONTHS[d.getMonth()]]);
      }
    } else {                                                       // dni
      const stepD = [1, 2, 3, 5, 7, 10, 14].find(s => span / DAY / s <= want) || 14;
      const start = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate()).getTime();
      for (let t = start; t <= t1; t += stepD * DAY) if (t >= t0) { const d = new Date(t); out.push([t, d.getDate() + ' ' + MONTHS[d.getMonth()]]); }
    }
    return out;
  }
  const fmtDate = t => { const d = new Date(t); return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear(); };

  class TimeChart {
    constructor(box, opts) {
      this.box = box; this.o = Object.assign({height: 320, navigator: true, zeroBased: false, fmtY: v => v.toFixed(0), fmtTip: null, minSpan: 7 * DAY}, opts);
      this.hidden = new Set(); this.data = []; this.win = null; this.onWindow = null;
      box.classList.add('chart-box'); box.innerHTML = '';
      this.svg = el('svg', {}, box); this.tip = document.createElement('div'); this.tip.className = 'tip'; box.appendChild(this.tip);
      this.ro = new ResizeObserver(() => this.render()); this.ro.observe(box);
      this._bind();
    }
    setSeries(series) { this.o.series = series; this.render(); }
    setData(points, keepWindow) {
      this.data = points || [];
      if (!keepWindow || !this.win) this.win = this.data.length ? [this.data[0].t, this.data[this.data.length - 1].t] : null;
      this.render();
    }
    domain() { return this.data.length ? [this.data[0].t, this.data[this.data.length - 1].t] : [0, 1]; }
    isZoomed() { const [a, b] = this.domain(); return this.win && (this.win[0] > a + DAY / 2 || this.win[1] < b - DAY / 2); }
    setWindow(t0, t1, silent) {
      if (!this.data.length) return;
      const [a, b] = this.domain();
      let span = Math.max(this.o.minSpan, t1 - t0); span = Math.min(span, b - a);
      t0 = clamp(t0, a, b - span); t1 = t0 + span;
      this.win = [t0, t1]; this.render();
      if (!silent && this.onWindow) this.onWindow(t0, t1);
    }
    reset() { const [a, b] = this.domain(); this.setWindow(a, b); }
    toggle(key) { this.hidden.has(key) ? this.hidden.delete(key) : this.hidden.add(key); this.render(); }
    _layout() {
      const W = this.box.clientWidth || 600, H = this.o.height, navH = this.o.navigator ? 46 : 0;
      return {W, H, L: 66, R: 14, T: 12, B: 26, navH, navT: H - navH + 4, plotB: H - navH - (navH ? 30 : 26)};
    }
    _visible() {
      const [t0, t1] = this.win; const d = this.data; let i0 = 0, i1 = d.length - 1;
      while (i0 < d.length - 1 && d[i0 + 1].t < t0) i0++;
      while (i1 > 0 && d[i1 - 1].t > t1) i1--;
      return [i0, i1];
    }
    render() {
      const svg = this.svg; while (svg.firstChild) svg.removeChild(svg.firstChild);
      const lay = this._layout(); this.lay = lay; svg.setAttribute('height', lay.H); svg.setAttribute('viewBox', `0 0 ${lay.W} ${lay.H}`);
      if (!this.data.length || !this.win) { el('text', {x: lay.W / 2, y: lay.H / 2, 'text-anchor': 'middle', style: 'fill:var(--muted);font-size:13px'}, svg).textContent = 'Brak danych do wykresu'; return; }
      const series = this.o.series.filter(s => !this.hidden.has(s.key));
      const [i0, i1] = this._visible(); const pts = this.data.slice(i0, i1 + 1);
      // serie "rebase" (poziom indeksu): zwrot liczony od pierwszego widocznego dnia z wartoscia - przy przyblizeniu porownanie startuje od zera
      this.base = {};
      for (const s of series) if (s.rebase) { const f = pts.find(p => p[s.key] !== null && p[s.key] !== undefined && p[s.key] > 0); this.base[s.key] = f ? f[s.key] : null; }
      this.baseT = pts.length ? pts[0].t : null;
      let lo = Infinity, hi = -Infinity;
      for (const p of pts) for (const s of series) { const v = this.val(p, s); if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (this.o.zeroBased || this.o.includeZero || series.some(s => s.signed)) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      const pad = (hi - lo || Math.abs(hi) || 1) * 0.07; lo -= (lo === 0 && this.o.zeroBased ? 0 : pad); hi += pad;
      const {L, R, T, plotB, W} = lay, [t0, t1] = this.win;
      const x = t => L + (W - L - R) * (t - t0) / Math.max(1, t1 - t0), y = v => T + (plotB - T) * (1 - (v - lo) / (hi - lo));
      this.x = x; this.y = y; this.xInv = px => t0 + (px - L) / (W - L - R) * (t1 - t0);
      const g = el('g', {}, svg);
      for (const v of yTicks(lo, hi, Math.max(3, Math.floor((plotB - T) / 55)))) {
        el('line', {x1: L, x2: W - R, y1: y(v), y2: y(v), style: 'stroke:var(--line);stroke-width:1' + (v === 0 ? ';stroke:var(--line2)' : '')}, g);
        el('text', {x: L - 8, y: y(v) + 4, 'text-anchor': 'end', style: 'fill:var(--muted);font-size:11px'}, g).textContent = this.o.fmtY(v);
      }
      for (const [t, lab] of xTicks(t0, t1, W - L - R)) {
        el('line', {x1: x(t), x2: x(t), y1: plotB, y2: plotB + 4, style: 'stroke:var(--line2)'}, g);
        el('text', {x: x(t), y: plotB + 17, 'text-anchor': 'middle', style: 'fill:var(--muted);font-size:11px'}, g).textContent = lab;
      }
      const clipId = 'c' + Math.random().toString(36).slice(2);
      const cp = el('clipPath', {id: clipId}, el('defs', {}, svg)); el('rect', {x: L, y: T, width: W - L - R, height: plotB - T}, cp);
      const plot = el('g', {'clip-path': `url(#${clipId})`}, svg);
      for (const s of series) {
        let dpath = '', started = false;
        for (const p of pts) { const v = this.val(p, s); if (v === null) { if (!s.connect) started = false; continue; } dpath += (started ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(v).toFixed(1); started = true; }
        if (!dpath) continue;
        if (s.area || s.signed) {
          const base = y(s.signed ? 0 : Math.max(lo, Math.min(0, hi)));
          const first = pts.find(p => this.val(p, s) !== null), last = [...pts].reverse().find(p => this.val(p, s) !== null);
          const area = dpath + `L${x(last.t).toFixed(1)},${base}L${x(first.t).toFixed(1)},${base}Z`;
          if (s.signed) {
            const y0 = y(0), ida = clipId + 'a', idb = clipId + 'b', defs = svg.querySelector('defs');
            el('rect', {x: L, y: T, width: W - L - R, height: Math.max(0, y0 - T)}, el('clipPath', {id: ida}, defs));
            el('rect', {x: L, y: y0, width: W - L - R, height: Math.max(0, plotB - y0)}, el('clipPath', {id: idb}, defs));
            el('path', {d: area, 'clip-path': `url(#${ida})`, style: 'fill:var(--pos);opacity:.16'}, plot);
            el('path', {d: area, 'clip-path': `url(#${idb})`, style: 'fill:var(--neg);opacity:.16'}, plot);
            el('path', {d: dpath, 'clip-path': `url(#${ida})`, style: 'fill:none;stroke:var(--pos);stroke-width:2'}, plot);
            el('path', {d: dpath, 'clip-path': `url(#${idb})`, style: 'fill:none;stroke:var(--neg);stroke-width:2'}, plot);
            continue;
          }
          const gid = clipId + s.key, grad = el('linearGradient', {id: gid, x1: 0, x2: 0, y1: 0, y2: 1}, svg.querySelector('defs'));
          el('stop', {offset: '0%', style: `stop-color:${s.color};stop-opacity:.28`}, grad); el('stop', {offset: '100%', style: `stop-color:${s.color};stop-opacity:.02`}, grad);
          el('path', {d: area, fill: `url(#${gid})`}, plot);
        }
        el('path', {d: dpath, style: `fill:none;stroke:${s.color};stroke-width:${s.width || 2};stroke-linejoin:round` + (s.dash ? ';stroke-dasharray:5 4' : '')}, plot);
      }
      // znaczniki (np. kupno / sprzedaz na wykresie pozycji): trojkat pod linia (kupno, w gore) albo nad linia (sprzedaz, w dol)
      for (const m of this.o.markers || []) {
        if (m.t < t0 || m.t > t1 || m.v === null || m.v === undefined) continue;
        const X = x(m.t), Y = y(m.v), sell = m.kind === 'sell';
        el('path', {d: sell ? `M${X - 6},${Y - 13}L${X + 6},${Y - 13}L${X},${Y - 3}Z` : `M${X - 6},${Y + 13}L${X + 6},${Y + 13}L${X},${Y + 3}Z`,
                    style: `fill:var(--${sell ? 'neg' : 'pos'});stroke:var(--card);stroke-width:1`}, plot);
      }
      this.cross = el('g', {style: 'display:none'}, svg);
      el('line', {y1: T, y2: plotB, style: 'stroke:var(--muted);stroke-dasharray:3 3'}, this.cross);
      this.dots = series.map(s => el('circle', {r: 4, style: `fill:var(--card);stroke:${s.signed ? 'var(--ink2)' : s.color};stroke-width:2`}, this.cross));
      this.sel = el('rect', {y: T, height: plotB - T, style: 'fill:var(--accent);opacity:.12;display:none'}, svg);
      this.overlay = el('rect', {x: L, y: T, width: W - L - R, height: plotB - T, style: 'fill:transparent;cursor:crosshair'}, svg);
      this.visSeries = series; this.visPts = pts;
      if (this.o.navigator) this._renderNav();
      if (this.o.onRender) this.o.onRender(this);
    }
    val(p, s) {
      const v = p[s.key];
      if (v === null || v === undefined || isNaN(v)) return null;
      if (!s.rebase) return v;
      const b = this.base && this.base[s.key];
      return b ? v / b - 1 : null;
    }
    _renderNav() {
      const {L, R, W, H, navT} = this.lay, s0 = this.o.series.find(s => !this.hidden.has(s.key) && !s.dash) || this.o.series[0], [a, b] = this.domain();
      const g = el('g', {}, this.svg), h = H - navT - 2;
      el('rect', {x: L, y: navT, width: W - L - R, height: h, rx: 6, style: 'fill:var(--card2);stroke:var(--line)'}, g);
      let lo = Infinity, hi = -Infinity; for (const p of this.data) { const v = p[s0.key]; if (v !== null && v !== undefined) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
      if (!isFinite(lo)) return;
      if (s0.signed) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      const nx = t => L + (W - L - R) * (t - a) / Math.max(1, b - a), ny = v => navT + 3 + (h - 6) * (1 - (v - lo) / ((hi - lo) || 1));
      this.nx = nx; this.nxInv = px => a + (px - L) / (W - L - R) * (b - a);
      let d = ''; this.data.forEach((p, i) => { const v = p[s0.key]; if (v !== null && v !== undefined) d += (d ? 'L' : 'M') + nx(p.t).toFixed(1) + ',' + ny(v).toFixed(1); });
      el('path', {d, style: `fill:none;stroke:${s0.signed ? 'var(--muted)' : s0.color};stroke-width:1.2;opacity:.8`}, g);
      const [t0, t1] = this.win, bx = nx(t0), bw = Math.max(6, nx(t1) - nx(t0));
      el('rect', {x: L, y: navT, width: Math.max(0, bx - L), height: h, style: 'fill:var(--bg);opacity:.55'}, g);
      el('rect', {x: bx + bw, y: navT, width: Math.max(0, W - R - bx - bw), height: h, style: 'fill:var(--bg);opacity:.55'}, g);
      this.brush = el('rect', {x: bx, y: navT, width: bw, height: h, rx: 4, style: 'fill:var(--accent);opacity:.10;stroke:var(--accent);stroke-width:1.5;cursor:grab'}, g);
      this.hL = el('rect', {x: bx - 4, y: navT + h / 2 - 11, width: 8, height: 22, rx: 3, style: 'fill:var(--card);stroke:var(--accent);stroke-width:1.5;cursor:ew-resize'}, g);
      this.hR = el('rect', {x: bx + bw - 4, y: navT + h / 2 - 11, width: 8, height: 22, rx: 3, style: 'fill:var(--card);stroke:var(--accent);stroke-width:1.5;cursor:ew-resize'}, g);
      this.navBg = g.firstChild;
    }
    _pt(ev) { const r = this.svg.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }
    _hover(px) {
      if (!this.visPts || !this.visPts.length) return;
      const t = this.xInv(px), pts = this.visPts; let lo = 0, hi = pts.length - 1;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m].t < t) lo = m; else hi = m; }
      const p = Math.abs(pts[lo].t - t) < Math.abs(pts[hi].t - t) ? pts[lo] : pts[hi];
      const X = this.x(p.t); this.cross.style.display = ''; const ln = this.cross.firstChild; ln.setAttribute('x1', X); ln.setAttribute('x2', X);
      this.visSeries.forEach((s, i) => { const v = this.val(p, s); const c = this.dots[i]; if (v === null) { c.style.display = 'none'; return; } c.style.display = ''; c.setAttribute('cx', X); c.setAttribute('cy', this.y(v)); });
      const html = this.o.fmtTip ? this.o.fmtTip(p, this.visSeries, this) : '<b>' + fmtDate(p.t) + '</b>' + this.visSeries.map(s => `<div class="r"><span><span class="sw" style="background:${s.color}"></span>${s.label}</span><b>${this.o.fmtY(this.val(p, s))}</b></div>`).join('');
      this.tip.innerHTML = html; this.tip.style.display = 'block';
      const bw = this.box.clientWidth, tw = this.tip.offsetWidth; let left = X + 14; if (left + tw > bw) left = X - tw - 14;
      this.tip.style.left = Math.max(0, left) + 'px'; this.tip.style.top = (this.lay.T + 4) + 'px';
    }
    _bind() {
      const svg = this.svg; let drag = null;
      svg.addEventListener('pointerdown', ev => {
        if (!this.lay || !this.data.length) return;
        const [px, py] = this._pt(ev), {L, R, W, T, plotB, navT} = this.lay;
        if (this.o.navigator && py >= navT) {
          const bx = this.nx(this.win[0]), bx2 = this.nx(this.win[1]);
          let mode = Math.abs(px - bx) < 8 ? 'l' : Math.abs(px - bx2) < 8 ? 'r' : (px > bx && px < bx2) ? 'move' : 'jump';
          if (mode === 'jump') { const span = this.win[1] - this.win[0], c = this.nxInv(px); this.setWindow(c - span / 2, c + span / 2); mode = 'move'; }
          drag = {kind: 'nav', mode, x0: px, w0: this.win.slice()};
        } else if (px >= L && px <= W - R && py >= T && py <= plotB) {
          drag = ev.shiftKey ? {kind: 'pan', x0: px, w0: this.win.slice()} : {kind: 'sel', x0: px};
        } else return;
        svg.setPointerCapture(ev.pointerId); ev.preventDefault();
      });
      svg.addEventListener('pointermove', ev => {
        if (!this.lay) return;
        const [px, py] = this._pt(ev);
        if (!drag) { const {L, R, W, T, plotB} = this.lay; if (px >= L && px <= W - R && py >= T && py <= plotB) this._hover(px); else this._leave(); return; }
        if (drag.kind === 'sel') {
          const a = clamp(Math.min(drag.x0, px), this.lay.L, this.lay.W - this.lay.R), b = clamp(Math.max(drag.x0, px), this.lay.L, this.lay.W - this.lay.R);
          this.sel.style.display = ''; this.sel.setAttribute('x', a); this.sel.setAttribute('width', b - a); this._hover(px);
        } else if (drag.kind === 'pan') {
          const dt = (this.xInv(drag.x0) - this.xInv(px)); this.setWindow(drag.w0[0] + dt, drag.w0[1] + dt);
        } else {
          const [a, b] = this.domain(), dt = (px - drag.x0) / (this.lay.W - this.lay.L - this.lay.R) * (b - a);
          if (drag.mode === 'move') this.setWindow(drag.w0[0] + dt, drag.w0[1] + dt);
          else if (drag.mode === 'l') this.setWindow(Math.min(drag.w0[0] + dt, drag.w0[1] - this.o.minSpan), drag.w0[1]);
          else this.setWindow(drag.w0[0], Math.max(drag.w0[1] + dt, drag.w0[0] + this.o.minSpan));
        }
      });
      const end = ev => {
        if (!drag) return;
        if (drag.kind === 'sel') {
          const [px] = this._pt(ev); this.sel.style.display = 'none';
          if (Math.abs(px - drag.x0) > 6) { const a = this.xInv(Math.min(drag.x0, px)), b = this.xInv(Math.max(drag.x0, px)); this.setWindow(a, b); }
        }
        drag = null;
      };
      svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', end);
      svg.addEventListener('pointerleave', () => { if (!drag) this._leave(); });
      svg.addEventListener('dblclick', () => this.reset());
      svg.addEventListener('wheel', ev => {
        if (!this.lay || !this.data.length) return;
        const [px, py] = this._pt(ev), {L, R, W, T, plotB} = this.lay;
        if (px < L || px > W - R || py < T || py > plotB) return;
        ev.preventDefault();
        const c = this.xInv(px), k = ev.deltaY > 0 ? 1.25 : 0.8, [t0, t1] = this.win;
        this.setWindow(c - (c - t0) * k, c + (t1 - c) * k);
      }, {passive: false});
    }
    _leave() { if (this.cross) this.cross.style.display = 'none'; this.tip.style.display = 'none'; }
  }

  function donut(box, items, opts) {
    opts = opts || {}; box.innerHTML = '';
    const total = items.reduce((a, i) => a + Math.max(0, i.value), 0);
    const wrap = document.createElement('div'); wrap.style.cssText = 'display:flex;gap:18px;align-items:center;flex-wrap:wrap'; box.appendChild(wrap);
    const S = opts.size || 210, r = S / 2 - 6, ri = r * 0.62, svg = el('svg', {width: S, height: S, viewBox: `0 0 ${S} ${S}`});
    wrap.appendChild(svg);
    const center = el('g', {}, svg);
    const t1 = el('text', {x: S / 2, y: S / 2 - 4, 'text-anchor': 'middle', style: 'fill:var(--muted);font-size:12px'}, center);
    const t2 = el('text', {x: S / 2, y: S / 2 + 16, 'text-anchor': 'middle', style: 'fill:var(--ink);font-size:16px;font-weight:700'}, center);
    const setCenter = (a, b) => { t1.textContent = a; t2.textContent = b; };
    setCenter(opts.centerLabel || 'Razem', opts.fmt ? opts.fmt(total) : total.toFixed(0));
    const list = document.createElement('div'); list.style.cssText = 'flex:1;min-width:200px;display:grid;gap:4px;font-size:.84rem;max-height:' + S + 'px;overflow:auto'; wrap.appendChild(list);
    let a0 = -Math.PI / 2; const paths = [];
    items.forEach((it, i) => {
      const frac = total ? Math.max(0, it.value) / total : 0, a1 = a0 + frac * Math.PI * 2;
      const large = a1 - a0 > Math.PI ? 1 : 0, c = S / 2;
      const p = (rad, ang) => [c + rad * Math.cos(ang), c + rad * Math.sin(ang)];
      const [x0, y0] = p(r, a0), [x1, y1] = p(r, Math.max(a0 + 1e-4, a1 - 0.004)), [x2, y2] = p(ri, Math.max(a0 + 1e-4, a1 - 0.004)), [x3, y3] = p(ri, a0);
      const path = frac >= 0.9999 ? `M${c},${c - r}A${r},${r} 0 1 1 ${c - 0.01},${c - r}L${c - 0.01},${c - ri}A${ri},${ri} 0 1 0 ${c},${c - ri}Z`
        : `M${x0},${y0}A${r},${r} 0 ${large} 1 ${x1},${y1}L${x2},${y2}A${ri},${ri} 0 ${large} 0 ${x3},${y3}Z`;
      const pe = el('path', {d: path, style: `fill:${it.color};cursor:pointer;transition:opacity .15s`}, svg); paths.push(pe);
      const row = document.createElement('div'); row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:2px 4px;border-radius:6px;cursor:default';
      row.innerHTML = `<span style="width:10px;height:10px;border-radius:3px;background:${it.color};flex:none"></span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${it.label}</span><b class="num">${(frac * 100).toLocaleString('pl-PL', {maximumFractionDigits: 1})}%</b>`;
      list.appendChild(row);
      const on = () => { paths.forEach(q => q.style.opacity = q === pe ? 1 : .35); row.style.background = 'var(--card2)'; setCenter(it.label, opts.fmt ? opts.fmt(it.value) : it.value); };
      const off = () => { paths.forEach(q => q.style.opacity = 1); row.style.background = ''; setCenter(opts.centerLabel || 'Razem', opts.fmt ? opts.fmt(total) : total.toFixed(0)); };
      pe.addEventListener('mouseenter', on); pe.addEventListener('mouseleave', off); row.addEventListener('mouseenter', on); row.addEventListener('mouseleave', off);
      a0 = a1;
    });
  }

  function bars(box, rows, stacks, opts) {
    opts = opts || {}; box.innerHTML = ''; box.classList.add('chart-box');
    const W = box.clientWidth || 600, H = opts.height || 230, L = 58, R = 8, T = 10, B = 26;
    const svg = el('svg', {height: H, viewBox: `0 0 ${W} ${H}`}, box), tip = document.createElement('div'); tip.className = 'tip'; box.appendChild(tip);
    if (!rows.length) { el('text', {x: W / 2, y: H / 2, 'text-anchor': 'middle', style: 'fill:var(--muted);font-size:13px'}, svg).textContent = 'Brak dochodów w tym zakresie'; return; }
    let lo = 0, hi = 0;
    rows.forEach(r => { let p = 0, n = 0; stacks.forEach(s => { const v = r[s.key] || 0; if (v >= 0) p += v; else n += v; }); hi = Math.max(hi, p); lo = Math.min(lo, n); });
    hi *= 1.08; lo *= 1.08; if (hi === lo) hi = 1;
    const y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo)), bw = (W - L - R) / rows.length;
    for (const v of yTicks(lo, hi, 4)) {
      el('line', {x1: L, x2: W - R, y1: y(v), y2: y(v), style: 'stroke:var(--line)' + (v === 0 ? ';stroke:var(--line2)' : '')}, svg);
      el('text', {x: L - 8, y: y(v) + 4, 'text-anchor': 'end', style: 'fill:var(--muted);font-size:11px'}, svg).textContent = opts.fmtY ? opts.fmtY(v) : v;
    }
    const every = Math.max(1, Math.ceil(rows.length / Math.floor((W - L - R) / 48)));
    rows.forEach((r, i) => {
      const x0 = L + i * bw + bw * 0.14, w = Math.max(2, bw * 0.72); let p = 0, n = 0;
      const g = el('g', {style: 'cursor:default'}, svg);
      stacks.forEach(s => {
        const v = r[s.key] || 0; if (!v) return;
        const a = v >= 0 ? p : n, b = a + v; if (v >= 0) p = b; else n = b;
        el('rect', {x: x0, y: Math.min(y(a), y(b)), width: w, height: Math.max(1, Math.abs(y(a) - y(b))), rx: 2, style: `fill:${s.color}`}, g);
      });
      el('rect', {x: L + i * bw, y: T, width: bw, height: H - T - B, style: 'fill:transparent'}, g);
      if (i % every === 0 || (opts.force && opts.force(r) && i % every !== every - 1)) el('text', {x: x0 + w / 2, y: H - 8, 'text-anchor': 'middle', style: 'fill:var(--muted);font-size:10.5px'}, svg).textContent = opts.label ? opts.label(r) : r.label;
      g.addEventListener('mousemove', ev => {
        const rect = box.getBoundingClientRect(); tip.style.display = 'block';
        const tot = stacks.reduce((a, s) => a + (r[s.key] || 0), 0);
        tip.innerHTML = '<b>' + (opts.title ? opts.title(r) : r.label) + '</b>' + stacks.filter(s => r[s.key]).map(s => `<div class="r"><span><span class="sw" style="background:${s.color}"></span>${s.label}</span><b>${opts.fmt(r[s.key])}</b></div>`).join('') + `<div class="r" style="border-top:1px solid var(--line);margin-top:4px;padding-top:4px"><span>Razem</span><b>${opts.fmt(tot)}</b></div>`;
        let left = ev.clientX - rect.left + 12; if (left + tip.offsetWidth > box.clientWidth) left = ev.clientX - rect.left - tip.offsetWidth - 12;
        tip.style.left = left + 'px'; tip.style.top = '6px'; g.style.opacity = .85;
      });
      g.addEventListener('mouseleave', () => { tip.style.display = 'none'; g.style.opacity = 1; });
    });
  }

  window.Charts = {TimeChart, donut, bars, fmtDate, DAY};
})();
