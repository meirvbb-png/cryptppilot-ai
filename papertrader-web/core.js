/* Paper Trader core (server side, runs inside a Cloudflare Durable Object). Demo only: never touches a real account.
   io = { state, get(exchange, path) -> json, save() -> Promise }.  `state` is mutated in place and persisted by io.save(). */
function createCore(io) {
  'use strict';
  const S = io.state;
  const FEE = 0.00055, MAINT = 0.9, START = 450, MAXLEV = 20;
  const SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'BNB/USDT:USDT'];
  const EXCHANGES = ['bybit', 'mexc'];
  const raw = s => s.split('/')[0] + 'USDT';
  const TFB = { '1m': '1', '5m': '5', '15m': '15', '1h': '60', '4h': '240' };
  const TFM = { '1m': '1m', '5m': '5m', '15m': '15m', '1h': '60m', '4h': '4h' };
  const now = () => Date.now() / 1000;
  const bad = m => { const e = new Error(m); e.user = true; throw e; };
  const dirn = s => s === 'LONG' ? 1 : -1;
  const pnl = (p, px) => dirn(p.side) * (px - p.entry) * p.qty;
  if (!S.positions) Object.assign(S, { balance: START, start: START, next: 1, positions: [] });

  /* ---------- market data (public endpoints only) ---------- */
  async function ohlcv(ex, sym, tf, limit) {
    if (ex === 'bybit') {
      const d = await io.get('bybit', `v5/market/kline?category=linear&symbol=${raw(sym)}&interval=${TFB[tf]}&limit=${limit}`);
      if (d.retCode !== 0) throw new Error('bybit: ' + d.retMsg);
      return d.result.list.map(c => [+c[0], +c[1], +c[2], +c[3], +c[4], +c[5]]).reverse();
    }
    const d = await io.get('mexc', `api/v3/klines?symbol=${raw(sym)}&interval=${TFM[tf]}&limit=${Math.min(limit, 500)}`);
    return d.map(c => [+c[0], +c[1], +c[2], +c[3], +c[4], +c[5]]);
  }
  async function book(ex, sym) {
    if (ex === 'bybit') { const d = await io.get('bybit', `v5/market/orderbook?category=linear&symbol=${raw(sym)}&limit=5`); return { bid: +d.result.b[0][0], ask: +d.result.a[0][0] }; }
    const d = await io.get('mexc', `api/v3/depth?symbol=${raw(sym)}&limit=5`); return { bid: +d.bids[0][0], ask: +d.asks[0][0] };
  }
  const pcache = {};
  async function last(ex, sym) {
    const k = ex + sym, c = pcache[k]; if (c && Date.now() - c.t < 3000) return c.p;
    let p;
    if (ex === 'bybit') p = +(await io.get('bybit', `v5/market/tickers?category=linear&symbol=${raw(sym)}`)).result.list[0].lastPrice;
    else p = +(await io.get('mexc', `api/v3/ticker/price?symbol=${raw(sym)}`)).price;
    pcache[k] = { p, t: Date.now() }; return p;
  }

  /* ---------- composite agents (original 3-agent pipeline) ---------- */
  const T = PTStrat;
  function macdHist(c) { const f = T.ema(c, 12), s = T.ema(c, 26), m = f.map((x, i) => x - s[i]), st = m.findIndex(x => !isNaN(x)), sig = Array(c.length).fill(NaN); T.ema(m.slice(st), 9).forEach((x, i) => sig[st + i] = x); return m.map((x, i) => x - sig[i]); }
  function pivots(h, l, w = 5) { const sup = [], res = []; for (let i = w; i < h.length - w; i++) { if (l[i] === Math.min(...l.slice(i - w, i + w + 1))) sup.push(l[i]); if (h[i] === Math.max(...h.slice(i - w, i + w + 1))) res.push(h[i]); } return { sup, res }; }
  const col = (r, i) => r.map(x => x[i]);
  function sizeTrade(equity, entry, sl, riskPct) {
    const riskUsd = equity * riskPct, dist = Math.abs(entry - sl) / entry; let notional = riskUsd / (dist + 2 * FEE);
    let lev = Math.max(1, Math.ceil(notional / (equity * 0.5))); while (lev < MAXLEV && dist > 0.6 / lev) lev++;
    notional = Math.min(notional, equity * lev * 0.9);
    return { risk_pct: riskPct, risk_usd: notional * dist + notional * 2 * FEE, notional, leverage: lev, margin: notional / lev, qty: notional / entry };
  }
  function compositeSignal(exName, symbol, rawLtf, rawHtf, equity) {
    const log = [], C = col(rawLtf, 4), H = col(rawLtf, 2), L = col(rawLtf, 3), V = col(rawLtf, 5), n = C.length - 1;
    const e20 = T.ema(C, 20), e50 = T.ema(C, 50), R = T.rsi(C, 14), A = T.atr(H, L, C, 14), M = macdHist(C), vs = T.sma(V, 20);
    if ([e50[n], R[n], A[n], M[n]].some(isNaN)) return null;
    const HC = col(rawHtf, 4), h50 = T.ema(HC, 50), h200 = T.ema(HC, 200), m = HC.length - 1;
    let htf = HC[m] > h50[m] ? 1 : -1;
    if (!isNaN(h200[m])) htf = (HC[m] > h50[m] && h50[m] > h200[m]) ? 1 : (HC[m] < h50[m] && h50[m] < h200[m]) ? -1 : htf * 0.5;
    const ltf = e20[n] > e50[n] ? 1 : -1, price = C[n], tail = Math.max(0, H.length - 150), pv = pivots(H.slice(tail), L.slice(tail));
    const supB = pv.sup.filter(s => s < price), resA = pv.res.filter(r => r > price);
    const a = { price, rsi: R[n], atr: A[n], hist: M[n], htf, ltf, vol: vs[n] ? V[n] / vs[n] : 1, support: supB.length ? Math.max(...supB) : null, resistance: resA.length ? Math.min(...resA) : null };
    log.push({ agent: 'אנליסט טכני', text: `מגמה בטווח גבוה: ${htf > 0 ? 'עולה' : htf < 0 ? 'יורדת' : 'צדדית'}; EMA20/50 ${ltf > 0 ? 'חיובי' : 'שלילי'}; RSI ${a.rsi.toFixed(0)}; MACD ${a.hist > 0 ? '+' : '-'}; נפח x${a.vol.toFixed(1)}` });
    let score = 2 * Math.sign(htf) + ltf + (a.hist > 0 ? 1 : -1); score += (a.rsi > 50 && a.rsi < 68) ? 1 : (a.rsi > 32 && a.rsi < 50) ? -1 : 0; if (a.vol > 1.2) score += ltf > 0 ? 1 : -1;
    const side = score >= 3 ? 'LONG' : score <= -3 ? 'SHORT' : null; if (!side) return null;
    const d = side === 'LONG' ? 1 : -1, entry = price; let sl = entry - d * 1.5 * a.atr; const lvl = side === 'LONG' ? a.support : a.resistance;
    if (lvl && Math.abs(entry - lvl) > 0.8 * a.atr && Math.abs(entry - lvl) < 2.5 * a.atr) sl = lvl - d * 0.25 * a.atr;
    const risk = Math.abs(entry - sl), tp1 = entry + d * 1.5 * risk, tp2 = entry + d * 3 * risk, confidence = Math.min(95, 40 + Math.abs(score) * 9);
    log.push({ agent: 'אסטרטג', text: `${side} (ציון ${score}) כניסה ${entry}, SL ${sl.toPrecision(6)}, TP1 ${tp1.toPrecision(6)}, TP2 ${tp2.toPrecision(6)}` });
    const sz = sizeTrade(equity, entry, sl, confidence >= 70 ? 0.02 : confidence >= 55 ? 0.0175 : 0.015);
    log.push({ agent: 'מנהל סיכונים', text: `סיכון ${(sz.risk_pct * 100).toFixed(2)}% | גודל $${sz.notional.toFixed(2)} | מינוף ${sz.leverage}x | מרג'ין $${sz.margin.toFixed(2)}` });
    return { strategy: 'composite', strategy_name: 'סוכנים משולבים', exchange: exName, symbol, side, entry, sl, tp1, tp2, confidence, ...sz, ts: now(), discussion: log,
      bt: 'לא נבדק היסטורית' };
  }

  /* ---------- rule-based scalp/intraday strategies (see research/README.md for honest backtests) ---------- */
  const ENABLED = ['pullback'];   // the other 3 lost money after costs in the backtest -> disabled
  const BT = {
    pullback: { tf: '15m', note: 'בדיקה היסטורית 90 יום: מבחן +0.11R לעסקה, אך אימון -0.27R (תלוי משטר שוק). ניסיוני.' },
    rsi2: { tf: '15m', note: 'מפסיד אחרי עמלות (-0.11R)' }, breakout: { tf: '15m', note: 'מפסיד אחרי עמלות (-0.18R)' }, squeeze: { tf: '15m', note: 'מפסיד אחרי עמלות (-0.30R)' } };
  const PARAMS = { pullback: { slAtr: 1.8, tpR: 2.5 }, rsi2: { slAtr: 1.8, tpR: 2.5 }, breakout: { slAtr: 1.8, tpR: 1.5 }, squeeze: { slAtr: 1.8, tpR: 2.5 } };
  function ruleSignals(exName, symbol, c15, equity) {
    const out = [], n = c15.length, i = n - 2;   // last CLOSED candle
    if (n < 230) return out;
    const I = T.prep(c15);
    for (const key of ENABLED) {
      const st = T.STRATS[key], sg = st.signal(I, i, PARAMS[key]); if (!sg) continue;
      const d = sg.side === 'LONG' ? 1 : -1, entry = c15[n - 1][4], risk = Math.abs(entry - sg.sl);
      if (!(risk > 0) || d * (entry - sg.sl) <= 0 || risk / entry > 0.05) continue;
      const tp1 = entry + d * sg.tpR * risk, sz = sizeTrade(equity, entry, sg.sl, 0.015);
      out.push({ strategy: key, strategy_name: st.name, experimental: true, exchange: exName, symbol, side: sg.side, entry, sl: sg.sl, tp1, tp2: null, confidence: 50, ...sz, ts: now(),
        bt: BT[key].note, discussion: [
          { agent: 'אנליסט טכני', text: `${st.desc}. ATR ${I.atr[i].toPrecision(4)}, ADX ${I.adx[i].toFixed(0)}, RSI ${I.rsi14[i].toFixed(0)}` },
          { agent: 'אסטרטג', text: `${st.name}: ${sg.side} כניסה ${entry}, SL ${sg.sl.toPrecision(6)}, TP ${tp1.toPrecision(6)} (${sg.tpR}R)` },
          { agent: 'מנהל סיכונים', text: `סיכון 1.5% | גודל $${sz.notional.toFixed(2)} | מינוף ${sz.leverage}x | מרג'ין $${sz.margin.toFixed(2)}` }] });
    }
    return out;
  }

  /* ---------- signals: one exchange/pair per step (keeps each invocation cheap) ---------- */
  const ST = { byPair: {}, idx: 0, updated: null, errors: {} };
  const PAIRS = EXCHANGES.flatMap(e => SYMBOLS.map(s => [e, s]));
  async function signalsStep() {
    const [ex, sym] = PAIRS[ST.idx++ % PAIRS.length], key = ex + sym;
    try {
      const eq = (await account()).equity, [a, b] = await Promise.all([ohlcv(ex, sym, '15m', 300), ohlcv(ex, sym, '1h', 250)]);
      const list = [], comp = compositeSignal(ex, sym, a, b, eq); if (comp) list.push(comp);
      ruleSignals(ex, sym, a, eq).forEach(s => list.push(s));
      ST.byPair[key] = { ts: Date.now(), list }; delete ST.errors[ex]; ST.updated = now();
    } catch (e) { ST.errors[ex] = String(e.message || e).slice(0, 200); ST.byPair[key] = { ts: Date.now(), list: [] }; }
  }
  const allSignals = () => Object.values(ST.byPair).filter(x => Date.now() - x.ts < 15 * 60000).flatMap(x => x.list)
    .map(s => ({ ...s, id: `${s.strategy}:${s.exchange}:${s.symbol}:${Math.floor(s.ts)}` })).sort((x, y) => y.confidence - x.confidence);

  /* ---------- paper engine ---------- */
  async function account() {
    const open = S.positions.filter(p => p.status === 'open'), used = S.positions.filter(p => p.status === 'open' || p.status === 'pending').reduce((s, p) => s + p.margin, 0);
    let upnl = 0; for (const p of open) { let px = p.entry; try { px = await last(p.exchange, p.symbol); } catch (e) { } upnl += pnl(p, px); }
    const closed = S.positions.filter(p => p.status === 'closed'), wins = closed.filter(p => p.realized > 0).length;
    return { balance: S.balance, start: S.start, used_margin: used, available: S.balance - used, upnl, equity: S.balance + upnl, trades: closed.length, wins, win_rate: closed.length ? wins / closed.length * 100 : null, total_pnl: S.balance - S.start };
  }
  async function openTrade(t) {
    const side = String(t.side).toUpperCase();
    if (side !== 'LONG' && side !== 'SHORT') bad('כיוון לא חוקי');
    if (!(t.leverage >= 1 && t.leverage <= 100)) bad('מינוף חייב להיות בין 1 ל-100');
    if (!(t.margin > 0)) bad('סכום חייב להיות חיובי');
    if (!EXCHANGES.includes(t.exchange) || !SYMBOLS.includes(t.symbol)) bad('בורסה/צמד לא נתמך');
    const a = await account();
    if (t.margin + t.margin * t.leverage * FEE > a.available + 1e-9) bad(`אין מספיק יתרה פנויה ($${a.available.toFixed(2)})`);
    const d = dirn(side); let ref;
    if (t.order_type === 'limit') { if (!(t.limit_price > 0)) bad('חסר מחיר לימיט'); ref = t.limit_price; } else { const b = await book(t.exchange, t.symbol); ref = d === 1 ? b.ask : b.bid; }
    const sl = t.sl > 0 ? t.sl : null, tp1 = t.tp1 > 0 ? t.tp1 : null, tp2 = t.tp2 > 0 ? t.tp2 : null;
    if (sl && d * (ref - sl) <= 0) bad('Stop-Loss בצד הלא נכון של הכניסה');
    if ((tp1 && d * (tp1 - ref) <= 0) || (tp2 && d * (tp2 - ref) <= 0)) bad('Take-Profit בצד הלא נכון של הכניסה');
    const liq = ref * (1 - d * MAINT / t.leverage); if (sl && d * (sl - liq) <= 0) bad('ה-Stop-Loss מעבר למחיר הליקווידציה; הקטן מינוף');
    const qty = t.margin * t.leverage / ref, tnow = now();
    const p = { id: S.next++, exchange: t.exchange, symbol: t.symbol, side, status: t.order_type === 'limit' ? 'pending' : 'open', order_type: t.order_type, limit_price: t.limit_price || null, qty, init_qty: qty, entry: ref, leverage: t.leverage, margin: t.margin, sl, tp1, tp2, tp1_hit: 0, liq,
      realized: 0, fees: 0, opened: tnow, closed: null, close_price: null, close_reason: null, last_check: tnow, source: String(t.source || 'manual').slice(0, 30), risk0: sl ? Math.abs(ref - sl) * qty : null };
    S.positions.push(p); if (p.status === 'open') chargeOpen(p); await io.save(); return p.id;
  }
  function chargeOpen(p) { const f = p.entry * p.qty * FEE; S.balance -= f; p.fees += f; p.realized -= f; }
  function closePart(p, price, reason, qty) {
    qty = Math.min(qty || p.qty, p.qty); let gross = dirn(p.side) * (price - p.entry) * qty; const fee = price * qty * FEE;
    if (reason === 'LIQUIDATION') gross = -p.margin * (qty / p.init_qty);
    S.balance += gross - fee; p.realized += gross - fee; p.fees += fee; p.qty -= qty;
    if (p.qty <= p.init_qty * 1e-9) { p.qty = 0; p.status = 'closed'; p.closed = now(); p.close_price = price; p.close_reason = reason; }
  }
  function events(p, hi, lo) {
    const d = dirn(p.side), hit = l => d === 1 ? lo <= l : hi >= l, reach = l => d === 1 ? hi >= l : lo <= l, out = [];
    if (hit(p.liq)) return [['LIQUIDATION', p.liq, p.qty]];
    if (p.sl != null && hit(p.sl)) return [[p.tp1_hit ? 'SL-BE' : 'SL', p.sl, p.qty]];
    if (p.tp1 && !p.tp1_hit && reach(p.tp1)) { if (p.tp2) out.push(['TP1', p.tp1, p.init_qty / 2]); else return [['TP', p.tp1, p.qty]]; }
    if (p.tp2 && reach(p.tp2)) out.push(['TP2', p.tp2, null]); return out;
  }
  async function processPos(p) {
    const tn = now(), c = await ohlcv(p.exchange, p.symbol, '1m', Math.min(1000, Math.max(15, Math.floor((tn - p.last_check) / 60) + 3))), since = p.last_check * 1000, series = [];
    for (const [t, , h0, l0, cl] of c) { if (t + 60000 <= since) continue; let h = h0, l = l0; if (t <= p.opened * 1000 && p.opened * 1000 < t + 60000) h = l = cl; series.push([h, l]); }
    for (const [hi, lo] of series) {
      if (p.status === 'pending') { const d = dirn(p.side); if ((d === 1 && lo <= p.limit_price) || (d === -1 && hi >= p.limit_price)) { p.status = 'open'; p.opened = tn; chargeOpen(p); } continue; }
      if (p.status !== 'open') break;
      for (const [reason, price, qty] of events(p, hi, lo)) { if (p.status !== 'open') break; closePart(p, price, reason, qty); if (reason === 'TP1') { p.tp1_hit = 1; p.sl = p.entry; } }
      if (p.status !== 'open') break;
    }
    p.last_check = tn;
  }
  let ticking = false;
  async function tick() {
    if (ticking) return; ticking = true;
    try { let ch = false; for (const p of S.positions.filter(x => x.status === 'open' || x.status === 'pending').slice(0, 20)) { try { await processPos(p); ch = true; } catch (e) { } } if (ch) await io.save(); } finally { ticking = false; }
  }
  const hasActive = () => S.positions.some(p => p.status === 'open' || p.status === 'pending');

  /* ---------- stats per strategy (forward test on live paper trades) ---------- */
  function stats() {
    const by = {};
    for (const p of S.positions.filter(x => x.status === 'closed')) {
      const k = p.source || 'manual', b = by[k] || (by[k] = { strategy: k, n: 0, wins: 0, pnl: 0, sumR: 0, nR: 0 });
      b.n++; if (p.realized > 0) b.wins++; b.pnl += p.realized; if (p.risk0) { b.sumR += p.realized / p.risk0; b.nR++; }
    }
    return Object.values(by).map(b => ({ ...b, win_rate: b.n ? b.wins / b.n * 100 : null, avg_r: b.nR ? b.sumR / b.nR : null })).sort((a, b) => b.n - a.n);
  }

  /* ---------- HTTP-like routing ---------- */
  async function route(path, q, body) {
    const json = (o, status = 200) => ({ status, body: o });
    try {
      if (path === 'config') return json({ exchanges: EXCHANGES, symbols: SYMBOLS, mock: false, server: true });
      if (path === 'state') {
        const positions = S.positions.filter(p => p.status === 'open' || p.status === 'pending').map(p => ({ ...p })).reverse();
        for (const p of positions) { try { p.mark = await last(p.exchange, p.symbol); p.upnl = p.status === 'open' ? pnl(p, p.mark) : 0; } catch (e) { p.mark = null; p.upnl = 0; } }
        return json({ account: await account(), positions, mock: false, errors: ST.errors });
      }
      if (path === 'signals') return json({ signals: allSignals(), updated: ST.updated, errors: ST.errors, mock: false });
      if (path === 'ohlcv') { try { return json(await ohlcv(q.get('exchange'), q.get('symbol'), q.get('tf') || '15m', 200)); } catch (e) { return json({ detail: 'שגיאת בורסה: ' + String(e.message || e).slice(0, 150) }, 502); } }
      if (path === 'trade') return json({ id: await openTrade(body) });
      if (path.startsWith('close/')) {
        const p = S.positions.find(x => x.id === +path.split('/')[1]); if (!p) bad('לא נמצא');
        if (p.status === 'pending') { p.status = 'cancelled'; p.closed = now(); } else if (p.status === 'open') closePart(p, await last(p.exchange, p.symbol), 'MANUAL'); else bad('הפוזיציה כבר סגורה');
        await io.save(); return json({ ok: true });
      }
      if (path === 'history') {
        const trades = S.positions.filter(p => p.status === 'closed' || p.status === 'cancelled').sort((a, b) => b.closed - a.closed).slice(0, 200);
        let bal = S.start; const curve = []; trades.filter(t => t.status === 'closed').sort((a, b) => a.closed - b.closed).forEach(t => { bal += t.realized; curve.push([t.closed, bal]); });
        return json({ trades, curve });
      }
      if (path === 'stats') return json({ stats: stats() });
      if (path === 'reset') { for (const k of Object.keys(S)) delete S[k]; Object.assign(S, { balance: START, start: START, next: 1, positions: [] }); await io.save(); return json({ ok: true }); }
      return json({ detail: 'not found' }, 404);
    } catch (e) { return e.user ? json({ detail: e.message }, 400) : json({ detail: String(e.message || e) }, 502); }
  }
  return { route, tick, signalsStep, hasActive, openTrade, account, compositeSignal, ruleSignals, S, ST };
}
