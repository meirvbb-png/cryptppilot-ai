/* Paper Trader – browser-only backend. Demo only: never touches a real account.
   Market data: public REST of Bybit (linear perps) and MEXC (spot price feed) fetched directly from the browser.
   It exposes the same /api/* surface as the Python version via a fetch shim, so the UI is identical. */
(function () {
  'use strict';
  const FEE = 0.00055, MAINT = 0.9, START = 450, MAXLEV = 20;
  const SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'BNB/USDT:USDT'];
  const EXCHANGES = ['bybit', 'mexc'];
  const raw = s => s.split('/')[0] + 'USDT';
  const TFB = { '1m': '1', '5m': '5', '15m': '15', '1h': '60', '4h': '240' };
  const TFM = { '1m': '1m', '5m': '5m', '15m': '15m', '1h': '60m', '4h': '4h' };
  const MOCK = false;

  /* ---------- market data ---------- */
  async function jget(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(url.split('/')[2] + ' HTTP ' + r.status);
    return r.json();
  }
  async function ohlcv(ex, sym, tf, limit) {
    if (ex === 'bybit') {
      const d = await jget(`https://api.bybit.com/v5/market/kline?category=linear&symbol=${raw(sym)}&interval=${TFB[tf]}&limit=${limit}`);
      if (d.retCode !== 0) throw new Error('bybit: ' + d.retMsg);
      return d.result.list.map(c => [+c[0], +c[1], +c[2], +c[3], +c[4], +c[5]]).reverse();
    }
    const d = await jget(`https://api.mexc.com/api/v3/klines?symbol=${raw(sym)}&interval=${TFM[tf]}&limit=${limit}`);
    return d.map(c => [+c[0], +c[1], +c[2], +c[3], +c[4], +c[5]]);
  }
  async function book(ex, sym) {
    if (ex === 'bybit') {
      const d = await jget(`https://api.bybit.com/v5/market/orderbook?category=linear&symbol=${raw(sym)}&limit=5`);
      return { bid: +d.result.b[0][0], ask: +d.result.a[0][0] };
    }
    const d = await jget(`https://api.mexc.com/api/v3/depth?symbol=${raw(sym)}&limit=5`);
    return { bid: +d.bids[0][0], ask: +d.asks[0][0] };
  }
  const pcache = {};
  async function last(ex, sym) {
    const k = ex + sym, c = pcache[k];
    if (c && Date.now() - c.t < 2000) return c.p;
    let p;
    if (ex === 'bybit') {
      const d = await jget(`https://api.bybit.com/v5/market/tickers?category=linear&symbol=${raw(sym)}`);
      p = +d.result.list[0].lastPrice;
    } else p = +(await jget(`https://api.mexc.com/api/v3/ticker/price?symbol=${raw(sym)}`)).price;
    pcache[k] = { p, t: Date.now() };
    return p;
  }

  /* ---------- indicators ---------- */
  const sma = (a, n) => a.map((_, i) => i < n - 1 ? NaN : a.slice(i - n + 1, i + 1).reduce((x, y) => x + y, 0) / n);
  function ema(a, n) {
    const o = Array(a.length).fill(NaN); if (a.length < n) return o;
    let e = a.slice(0, n).reduce((x, y) => x + y, 0) / n; o[n - 1] = e; const k = 2 / (n + 1);
    for (let i = n; i < a.length; i++) { e = a[i] * k + e * (1 - k); o[i] = e; } return o;
  }
  function rma(a, n) {
    const o = Array(a.length).fill(NaN); if (a.length <= n) return o;
    let e = a.slice(1, n + 1).reduce((x, y) => x + y, 0) / n; o[n] = e;
    for (let i = n + 1; i < a.length; i++) { e = (e * (n - 1) + a[i]) / n; o[i] = e; } return o;
  }
  function rsi(c, n = 14) {
    const g = [0], l = [0];
    for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g.push(Math.max(d, 0)); l.push(Math.max(-d, 0)); }
    const ag = rma(g, n), al = rma(l, n);
    return ag.map((x, i) => al[i] === 0 ? 100 : 100 - 100 / (1 + x / al[i]));
  }
  function atr(h, l, c, n = 14) {
    const tr = h.map((x, i) => i === 0 ? x - l[i] : Math.max(x - l[i], Math.abs(x - c[i - 1]), Math.abs(l[i] - c[i - 1])));
    return rma(tr, n);
  }
  function macdHist(c) {
    const f = ema(c, 12), s = ema(c, 26), m = f.map((x, i) => x - s[i]);
    const valid = m.map(x => isNaN(x) ? 0 : x), start = m.findIndex(x => !isNaN(x));
    const sig = Array(c.length).fill(NaN), e = ema(valid.slice(start), 9);
    e.forEach((x, i) => sig[start + i] = x);
    return m.map((x, i) => x - sig[i]);
  }
  function pivots(h, l, w = 5) {
    const sup = [], res = [];
    for (let i = w; i < h.length - w; i++) {
      const lo = Math.min(...l.slice(i - w, i + w + 1)), hi = Math.max(...h.slice(i - w, i + w + 1));
      if (l[i] === lo) sup.push(l[i]); if (h[i] === hi) res.push(h[i]);
    } return { sup, res };
  }
  const col = (r, i) => r.map(x => x[i]);

  /* ---------- agents: TechnicalAnalyst -> Strategist -> RiskManager ---------- */
  function buildSignal(exName, symbol, rawLtf, rawHtf, equity) {
    const log = [];
    const C = col(rawLtf, 4), H = col(rawLtf, 2), L = col(rawLtf, 3), V = col(rawLtf, 5), n = C.length - 1;
    const e20 = ema(C, 20), e50 = ema(C, 50), R = rsi(C), A = atr(H, L, C), M = macdHist(C), vs = sma(V, 20);
    if ([e50[n], R[n], A[n], M[n]].some(isNaN)) return null;
    const HC = col(rawHtf, 4), h50 = ema(HC, 50), h200 = ema(HC, 200), m = HC.length - 1;
    let htf = HC[m] > h50[m] ? 1 : -1;
    if (!isNaN(h200[m])) htf = (HC[m] > h50[m] && h50[m] > h200[m]) ? 1 : (HC[m] < h50[m] && h50[m] < h200[m]) ? -1 : htf * 0.5;
    const ltf = e20[n] > e50[n] ? 1 : -1, price = C[n];
    const tail = Math.max(0, H.length - 150), pv = pivots(H.slice(tail), L.slice(tail));
    const supB = pv.sup.filter(s => s < price), resA = pv.res.filter(r => r > price);
    const a = { price, rsi: R[n], atr: A[n], hist: M[n], htf, ltf, vol: vs[n] ? V[n] / vs[n] : 1,
      support: supB.length ? Math.max(...supB) : null, resistance: resA.length ? Math.min(...resA) : null };
    log.push({ agent: 'אנליסט טכני', text: `מגמה בטווח גבוה: ${htf > 0 ? 'עולה' : htf < 0 ? 'יורדת' : 'צדדית'}; EMA20/50 ${ltf > 0 ? 'חיובי' : 'שלילי'}; RSI ${a.rsi.toFixed(0)}; MACD ${a.hist > 0 ? '+' : '-'}; נפח x${a.vol.toFixed(1)}; תמיכה ${a.support}, התנגדות ${a.resistance}` });
    let score = 2 * Math.sign(htf) + ltf + (a.hist > 0 ? 1 : -1);
    score += (a.rsi > 50 && a.rsi < 68) ? 1 : (a.rsi > 32 && a.rsi < 50) ? -1 : 0;
    if (a.vol > 1.2) score += ltf > 0 ? 1 : -1;
    const side = score >= 3 ? 'LONG' : score <= -3 ? 'SHORT' : null;
    if (!side) return null;
    const d = side === 'LONG' ? 1 : -1, entry = price;
    let sl = entry - d * 1.5 * a.atr;
    const lvl = side === 'LONG' ? a.support : a.resistance;
    if (lvl && Math.abs(entry - lvl) > 0.8 * a.atr && Math.abs(entry - lvl) < 2.5 * a.atr) sl = lvl - d * 0.25 * a.atr;
    const risk = Math.abs(entry - sl), tp1 = entry + d * 1.5 * risk, tp2 = entry + d * 3 * risk;
    const confidence = Math.min(95, 40 + Math.abs(score) * 9);
    log.push({ agent: 'אסטרטג', text: `${side} (ציון ${score}) כניסה ${entry}, SL ${sl.toPrecision(6)}, TP1 ${tp1.toPrecision(6)}, TP2 ${tp2.toPrecision(6)}` });
    const riskPct = confidence >= 70 ? 0.02 : confidence >= 55 ? 0.0175 : 0.015, riskUsd = equity * riskPct;
    const dist = risk / entry; let notional = riskUsd / (dist + 2 * FEE);
    let lev = Math.max(1, Math.ceil(notional / (equity * 0.5)));
    while (lev < MAXLEV && dist > 0.6 / lev) lev++;
    notional = Math.min(notional, equity * lev * 0.9);
    const margin = notional / lev;
    log.push({ agent: 'מנהל סיכונים', text: `סיכון ${(riskPct * 100).toFixed(2)}% = $${riskUsd.toFixed(2)} | גודל $${notional.toFixed(2)} | מינוף ${lev}x | מרג'ין $${margin.toFixed(2)}` });
    return { exchange: exName, symbol, side, entry, sl, tp1, tp2, score, confidence, risk_pct: riskPct,
      risk_usd: notional * dist + notional * 2 * FEE, notional, leverage: lev, margin, qty: notional / entry, ts: Date.now() / 1000, discussion: log };
  }

  /* ---------- storage ---------- */
  const KEY = 'papertrader.v1';
  let S;
  function load() {
    try { S = JSON.parse(localStorage.getItem(KEY)); } catch (e) { S = null; }
    if (!S || !S.positions) S = { balance: START, start: START, next: 1, positions: [] };
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { } }
  load();
  const dirn = s => s === 'LONG' ? 1 : -1;
  const pnl = (p, px) => dirn(p.side) * (px - p.entry) * p.qty;

  /* ---------- paper engine ---------- */
  async function account() {
    const open = S.positions.filter(p => p.status === 'open'), pend = S.positions.filter(p => p.status === 'pending');
    const used = open.concat(pend).reduce((s, p) => s + p.margin, 0);
    let upnl = 0;
    for (const p of open) { let px = p.entry; try { px = await last(p.exchange, p.symbol); } catch (e) { } upnl += pnl(p, px); }
    const closed = S.positions.filter(p => p.status === 'closed'), wins = closed.filter(p => p.realized > 0).length;
    return { balance: S.balance, start: S.start, used_margin: used, available: S.balance - used, upnl, equity: S.balance + upnl,
      trades: closed.length, wins, win_rate: closed.length ? wins / closed.length * 100 : null, total_pnl: S.balance - S.start };
  }
  function bad(msg) { const e = new Error(msg); e.user = true; throw e; }
  async function openTrade(t) {
    const side = String(t.side).toUpperCase();
    if (side !== 'LONG' && side !== 'SHORT') bad('כיוון לא חוקי');
    if (!(t.leverage >= 1 && t.leverage <= 100)) bad('מינוף חייב להיות בין 1 ל-100');
    if (!(t.margin > 0)) bad('סכום חייב להיות חיובי');
    const a = await account();
    if (t.margin + t.margin * t.leverage * FEE > a.available + 1e-9) bad(`אין מספיק יתרה פנויה ($${a.available.toFixed(2)})`);
    const d = dirn(side); let ref;
    if (t.order_type === 'limit') { if (!(t.limit_price > 0)) bad('חסר מחיר לימיט'); ref = t.limit_price; }
    else { const b = await book(t.exchange, t.symbol); ref = d === 1 ? b.ask : b.bid; }
    const sl = t.sl > 0 ? t.sl : null, tp1 = t.tp1 > 0 ? t.tp1 : null, tp2 = t.tp2 > 0 ? t.tp2 : null;
    if (sl && d * (ref - sl) <= 0) bad('Stop-Loss בצד הלא נכון של הכניסה');
    if ((tp1 && d * (tp1 - ref) <= 0) || (tp2 && d * (tp2 - ref) <= 0)) bad('Take-Profit בצד הלא נכון של הכניסה');
    const liq = ref * (1 - d * MAINT / t.leverage);
    if (sl && d * (sl - liq) <= 0) bad('ה-Stop-Loss מעבר למחיר הליקווידציה; הקטן מינוף');
    const qty = t.margin * t.leverage / ref, now = Date.now() / 1000;
    const p = { id: S.next++, exchange: t.exchange, symbol: t.symbol, side, status: t.order_type === 'limit' ? 'pending' : 'open', order_type: t.order_type,
      limit_price: t.limit_price || null, qty, init_qty: qty, entry: ref, leverage: t.leverage, margin: t.margin, sl, tp1, tp2, tp1_hit: 0, liq,
      realized: 0, fees: 0, opened: now, closed: null, close_price: null, close_reason: null, last_check: now, source: t.source || 'manual' };
    S.positions.push(p);
    if (p.status === 'open') chargeOpen(p);
    save(); return p.id;
  }
  function chargeOpen(p) { const f = p.entry * p.qty * FEE; S.balance -= f; p.fees += f; p.realized -= f; }
  function closePart(p, price, reason, qty) {
    qty = Math.min(qty || p.qty, p.qty);
    let gross = dirn(p.side) * (price - p.entry) * qty;
    const fee = price * qty * FEE;
    if (reason === 'LIQUIDATION') gross = -p.margin * (qty / p.init_qty);
    S.balance += gross - fee; p.realized += gross - fee; p.fees += fee;
    p.qty -= qty;
    if (p.qty <= p.init_qty * 1e-9) { p.qty = 0; p.status = 'closed'; p.closed = Date.now() / 1000; p.close_price = price; p.close_reason = reason; }
  }
  function events(p, hi, lo) {
    const d = dirn(p.side), hit = l => d === 1 ? lo <= l : hi >= l, reach = l => d === 1 ? hi >= l : lo <= l, out = [];
    if (hit(p.liq)) return [['LIQUIDATION', p.liq, p.qty]];
    if (p.sl != null && hit(p.sl)) return [[p.tp1_hit ? 'SL-BE' : 'SL', p.sl, p.qty]];
    if (p.tp1 && !p.tp1_hit && reach(p.tp1)) { if (p.tp2) out.push(['TP1', p.tp1, p.init_qty / 2]); else return [['TP', p.tp1, p.qty]]; }
    if (p.tp2 && reach(p.tp2)) out.push(['TP2', p.tp2, null]);
    return out;
  }
  async function processPos(p) {
    const now = Date.now() / 1000, c = await ohlcv(p.exchange, p.symbol, '1m', 15), since = p.last_check * 1000, series = [];
    for (const [t, , h0, l0, cl] of c) {
      if (t + 60000 <= since) continue;
      let h = h0, l = l0;
      if (t <= p.opened * 1000 && p.opened * 1000 < t + 60000) h = l = cl;   // entry candle: only trust close
      series.push([h, l]);
    }
    for (const [hi, lo] of series) {
      if (p.status === 'pending') {
        const d = dirn(p.side);
        if ((d === 1 && lo <= p.limit_price) || (d === -1 && hi >= p.limit_price)) { p.status = 'open'; p.opened = now; chargeOpen(p); }
        continue;
      }
      if (p.status !== 'open') break;
      for (const [reason, price, qty] of events(p, hi, lo)) {
        if (p.status !== 'open') break;
        closePart(p, price, reason, qty);
        if (reason === 'TP1') { p.tp1_hit = 1; p.sl = p.entry; }
      }
      if (p.status !== 'open') break;
    }
    p.last_check = now;
  }
  let ticking = false;
  async function tick() {
    if (ticking) return; ticking = true;
    try { for (const p of S.positions.filter(x => x.status === 'open' || x.status === 'pending')) { try { await processPos(p); } catch (e) { } } save(); }
    finally { ticking = false; }
  }

  /* ---------- signals loop ---------- */
  const ST = { signals: [], updated: null, errors: {} };
  let refreshing = false;
  async function refreshSignals() {
    if (refreshing) return; refreshing = true;
    try {
      const eq = (await account()).equity, sigs = [], errs = {};
      await Promise.all(EXCHANGES.map(async ex => {
        for (const s of SYMBOLS) {
          try {
            const [a, b] = await Promise.all([ohlcv(ex, s, '15m', 300), ohlcv(ex, s, '1h', 250)]);
            const g = buildSignal(ex, s, a, b, eq); if (g) { g.id = `${ex}:${s}:${Math.floor(g.ts)}`; sigs.push(g); }
          } catch (e) { errs[ex] = String(e.message || e).slice(0, 160); break; }
        }
      }));
      sigs.sort((x, y) => y.confidence - x.confidence);
      ST.signals = sigs; ST.updated = Date.now() / 1000; ST.errors = errs;
    } finally { refreshing = false; }
  }

  /* ---------- /api shim ---------- */
  const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
  async function route(url, opt) {
    const u = new URL(url, location.href), path = u.pathname.replace(/^.*\/api\//, ''), q = u.searchParams;
    const body = opt && opt.body ? JSON.parse(opt.body) : {};
    try {
      if (path === 'config') return json({ exchanges: EXCHANGES, symbols: SYMBOLS, mock: MOCK });
      if (path === 'state') {
        const positions = S.positions.filter(p => p.status === 'open' || p.status === 'pending').map(p => ({ ...p })).reverse();
        for (const p of positions) { try { p.mark = await last(p.exchange, p.symbol); p.upnl = p.status === 'open' ? pnl(p, p.mark) : 0; } catch (e) { p.mark = null; p.upnl = 0; } }
        return json({ account: await account(), positions, mock: MOCK, errors: ST.errors });
      }
      if (path === 'signals') return json({ signals: ST.signals, updated: ST.updated, errors: ST.errors, mock: MOCK });
      if (path === 'ohlcv') {
        try { return json(await ohlcv(q.get('exchange'), q.get('symbol'), q.get('tf') || '15m', 200)); }
        catch (e) { return json({ detail: 'שגיאת בורסה: ' + String(e.message || e).slice(0, 150) }, 502); }
      }
      if (path === 'trade') return json({ id: await openTrade(body) });
      if (path.startsWith('close/')) {
        const p = S.positions.find(x => x.id === +path.split('/')[1]);
        if (!p) bad('לא נמצא');
        if (p.status === 'pending') { p.status = 'cancelled'; p.closed = Date.now() / 1000; }
        else if (p.status === 'open') closePart(p, await last(p.exchange, p.symbol), 'MANUAL');
        else bad('הפוזיציה כבר סגורה');
        save(); return json({ ok: true });
      }
      if (path === 'history') {
        const trades = S.positions.filter(p => p.status === 'closed' || p.status === 'cancelled').sort((a, b) => b.closed - a.closed).slice(0, 200);
        let bal = S.start; const curve = [];
        trades.filter(t => t.status === 'closed').sort((a, b) => a.closed - b.closed).forEach(t => { bal += t.realized; curve.push([t.closed, bal]); });
        return json({ trades, curve });
      }
      if (path === 'reset') { S = { balance: START, start: START, next: 1, positions: [] }; save(); return json({ ok: true }); }
      return json({ detail: 'not found' }, 404);
    } catch (e) {
      if (e.user) return json({ detail: e.message }, 400);
      return json({ detail: String(e.message || e) }, 502);
    }
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = (url, opt) => /(^|\/)api\/(config|state|signals|ohlcv|trade|close|history|reset)/.test(String(url)) && !/^https?:\/\/(api\.)/.test(String(url)) ? route(String(url), opt) : realFetch(url, opt);

  setInterval(tick, 3000); tick();
  refreshSignals(); setInterval(refreshSignals, 120000);
  window.__PT = { buildSignal, ema, rsi, atr, macdHist, openTrade, tick, route, get S() { return S; }, set S(v) { S = v; } };
})();
