/* Scalp strategies. Pure functions, shared by the browser/Worker engine and the research backtester.
   Candle = [ts,o,h,l,c,v]. prep(candles) -> indicator arrays; each strategy.signal(ind,i) looks ONLY at bars <= i. */
(function (root) {
  const sma = (a, n) => { const o = Array(a.length).fill(NaN); let s = 0; for (let i = 0; i < a.length; i++) { s += a[i]; if (i >= n) s -= a[i - n]; if (i >= n - 1) o[i] = s / n; } return o; };
  const ema = (a, n) => { const o = Array(a.length).fill(NaN); if (a.length < n) return o; let e = a.slice(0, n).reduce((x, y) => x + y, 0) / n; o[n - 1] = e; const k = 2 / (n + 1); for (let i = n; i < a.length; i++) { e = a[i] * k + e * (1 - k); o[i] = e; } return o; };
  const rma = (a, n) => { const o = Array(a.length).fill(NaN); if (a.length <= n) return o; let e = a.slice(1, n + 1).reduce((x, y) => x + y, 0) / n; o[n] = e; for (let i = n + 1; i < a.length; i++) { e = (e * (n - 1) + a[i]) / n; o[i] = e; } return o; };
  const rsi = (c, n) => { const g = [0], l = [0]; for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g.push(Math.max(d, 0)); l.push(Math.max(-d, 0)); } const ag = rma(g, n), al = rma(l, n); return ag.map((x, i) => al[i] === 0 ? 100 : 100 - 100 / (1 + x / al[i])); };
  const atr = (h, l, c, n) => rma(h.map((x, i) => i === 0 ? x - l[i] : Math.max(x - l[i], Math.abs(x - c[i - 1]), Math.abs(l[i] - c[i - 1]))), n);
  const hh = (a, n) => a.map((_, i) => i < n ? NaN : Math.max(...a.slice(i - n, i)));   // highest of PREVIOUS n bars
  const ll = (a, n) => a.map((_, i) => i < n ? NaN : Math.min(...a.slice(i - n, i)));
  function bb(c, n, k) { const m = sma(c, n), sd = c.map((_, i) => { if (i < n - 1) return NaN; let s = 0; for (let j = i - n + 1; j <= i; j++) s += (c[j] - m[i]) ** 2; return Math.sqrt(s / n); }); return { m, up: m.map((x, i) => x + k * sd[i]), lo: m.map((x, i) => x - k * sd[i]), w: m.map((x, i) => 2 * k * sd[i] / x) }; }
  function adx(h, l, c, n) {
    const pdm = [0], mdm = [0], tr = [h[0] - l[0]];
    for (let i = 1; i < h.length; i++) { const up = h[i] - h[i - 1], dn = l[i - 1] - l[i]; pdm.push(up > dn && up > 0 ? up : 0); mdm.push(dn > up && dn > 0 ? dn : 0); tr.push(Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1]))); }
    const a = rma(tr, n), p = rma(pdm, n), m = rma(mdm, n), dx = a.map((x, i) => { const pi = 100 * p[i] / x, mi = 100 * m[i] / x; return 100 * Math.abs(pi - mi) / (pi + mi); });
    const first = dx.findIndex(x => !isNaN(x)), out = Array(dx.length).fill(NaN); if (first < 0) return out;
    const r = rma(dx.slice(first).map(x => isNaN(x) ? 0 : x), n); r.forEach((x, i) => out[first + i] = x); return out;
  }
  function prep(C) {
    const o = C.map(x => x[1]), h = C.map(x => x[2]), l = C.map(x => x[3]), c = C.map(x => x[4]), v = C.map(x => x[5]);
    return { o, h, l, c, v, ema9: ema(c, 9), ema20: ema(c, 20), ema50: ema(c, 50), ema200: ema(c, 200), rsi14: rsi(c, 14), rsi2: rsi(c, 2), atr: atr(h, l, c, 14),
      vsma: sma(v, 20), hh20: hh(h, 20), ll20: ll(l, 20), bb: bb(c, 20, 2), adx: adx(h, l, c, 14) };
  }
  const ok = (...x) => x.every(v => !isNaN(v) && v != null);
  // each strategy returns {side, sl, tpR} (SL price; TP = tpR * risk) using bar i close as the reference entry
  const STRATS = {
    pullback: { name: 'Trend Pullback', desc: 'חזרה ל-EMA20 בתוך מגמה (EMA50/200) עם נר אישור',
      defaults: { slAtr: 1.0, tpR: 1.5 },
      signal(I, i, p) {
        if (!ok(I.ema200[i], I.ema50[i], I.rsi14[i], I.atr[i], I.adx[i]) || I.adx[i] < 18) return null;
        const up = I.c[i] > I.ema200[i] && I.ema50[i] > I.ema200[i], dn = I.c[i] < I.ema200[i] && I.ema50[i] < I.ema200[i];
        if (up && I.l[i] <= I.ema20[i] && I.c[i] > I.ema20[i] && I.c[i] > I.o[i] && I.rsi14[i] > 40 && I.rsi14[i] < 65)
          return { side: 'LONG', sl: Math.min(I.l[i], I.c[i] - p.slAtr * I.atr[i]) - 0.1 * I.atr[i], tpR: p.tpR };
        if (dn && I.h[i] >= I.ema20[i] && I.c[i] < I.ema20[i] && I.c[i] < I.o[i] && I.rsi14[i] < 60 && I.rsi14[i] > 35)
          return { side: 'SHORT', sl: Math.max(I.h[i], I.c[i] + p.slAtr * I.atr[i]) + 0.1 * I.atr[i], tpR: p.tpR };
        return null;
      } },
    rsi2: { name: 'RSI2 Reversion', desc: 'קיצון RSI(2) נגד המגמה הקצרה, בכיוון המגמה הגדולה',
      defaults: { slAtr: 1.5, tpR: 1.0 },
      signal(I, i, p) {
        if (!ok(I.ema200[i], I.rsi2[i], I.atr[i])) return null;
        if (I.c[i] > I.ema200[i] && I.rsi2[i] < 8 && I.c[i] > I.ema50[i] * 0.99) return { side: 'LONG', sl: I.c[i] - p.slAtr * I.atr[i], tpR: p.tpR };
        if (I.c[i] < I.ema200[i] && I.rsi2[i] > 92 && I.c[i] < I.ema50[i] * 1.01) return { side: 'SHORT', sl: I.c[i] + p.slAtr * I.atr[i], tpR: p.tpR };
        return null;
      } },
    breakout: { name: 'Volume Breakout', desc: 'פריצת שיא/שפל 20 נרות עם נפח חריג וסינון מגמה',
      defaults: { slAtr: 1.2, tpR: 2.0 },
      signal(I, i, p) {
        if (!ok(I.hh20[i], I.vsma[i], I.atr[i], I.ema50[i], I.adx[i]) || I.adx[i] < 20 || I.v[i] < 1.5 * I.vsma[i]) return null;
        if (I.c[i] > I.hh20[i] && I.c[i] > I.ema50[i]) return { side: 'LONG', sl: I.c[i] - p.slAtr * I.atr[i], tpR: p.tpR };
        if (I.c[i] < I.ll20[i] && I.c[i] < I.ema50[i]) return { side: 'SHORT', sl: I.c[i] + p.slAtr * I.atr[i], tpR: p.tpR };
        return null;
      } },
    squeeze: { name: 'BB Squeeze', desc: 'פריצה מרצועות בולינגר דחוסות (תנודתיות נמוכה → גבוהה)',
      defaults: { slAtr: 1.2, tpR: 2.0 },
      signal(I, i, p) {
        if (i < 120 || !ok(I.bb.w[i], I.atr[i], I.ema50[i])) return null;
        const w = I.bb.w.slice(i - 100, i).filter(x => !isNaN(x)).sort((a, b) => a - b), thr = w[Math.floor(w.length * 0.2)];
        if (!(I.bb.w[i - 1] <= thr)) return null;
        if (I.c[i] > I.bb.up[i] && I.c[i] > I.ema50[i]) return { side: 'LONG', sl: I.c[i] - p.slAtr * I.atr[i], tpR: p.tpR };
        if (I.c[i] < I.bb.lo[i] && I.c[i] < I.ema50[i]) return { side: 'SHORT', sl: I.c[i] + p.slAtr * I.atr[i], tpR: p.tpR };
        return null;
      } },
  };
  const api = { STRATS, prep, ema, sma, rsi, atr };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.PTStrat = api;
})(typeof window !== 'undefined' ? window : globalThis);
