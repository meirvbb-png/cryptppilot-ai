// node backtest.mjs <datadir> <tf>   – walk-forward evaluation, realistic costs, no look-ahead.
import fs from 'fs'; import { createRequire } from 'module';
const S = createRequire(import.meta.url)('../papertrader-web/strategies.js');
const dir = process.argv[2], tf = process.argv[3] || '5m';
const SYMS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB'].map(s => s + 'USDT');
const FEE = +(process.env.FEE || 0.00055), SLIP = +(process.env.SLIP || 0.0002), MAXBARS = +(process.env.MAXBARS || 36);   // per side taker fee, slippage per side, time-stop
function sim(C, I, strat, p, from, to) {
  const trades = []; let i = from;
  while (i < to - 2) {
    const sg = strat.signal(I, i, p); if (!sg) { i++; continue; }
    const d = sg.side === 'LONG' ? 1 : -1, entry = C[i + 1][1] * (1 + d * SLIP);   // next bar open (+slippage against us)
    const risk = Math.abs(entry - sg.sl); if (!(risk > 0) || risk / entry > 0.05 || d * (entry - sg.sl) <= 0) { i++; continue; }
    const tp = entry + d * sg.tpR * risk; let exit = null, j = i + 1;
    for (; j < Math.min(to, i + 1 + MAXBARS); j++) {
      const hi = C[j][2], lo = C[j][3];
      if (d === 1 ? lo <= sg.sl : hi >= sg.sl) { exit = sg.sl; break; }
      if (d === 1 ? hi >= tp : lo <= tp) { exit = tp; break; }
    }
    if (exit === null) { j = Math.min(to, i + 1 + MAXBARS) - 1; exit = C[j][4]; }
    exit *= (1 - d * SLIP);
    const gross = d * (exit - entry), cost = (entry + exit) * FEE, R = (gross - cost) / risk;
    trades.push({ R, win: R > 0, t: C[i][0] }); i = j + 1;
  }
  return trades;
}
const stats = t => { const n = t.length; if (!n) return { n: 0, exp: 0, wr: 0, pf: 0, dd: 0, tot: 0 }; const w = t.filter(x => x.R > 0), l = t.filter(x => x.R <= 0); let c = 0, pk = 0, dd = 0; for (const x of t) { c += x.R; pk = Math.max(pk, c); dd = Math.max(dd, pk - c); }
  return { n, exp: t.reduce((s, x) => s + x.R, 0) / n, wr: w.length / n * 100, pf: l.length ? w.reduce((s, x) => s + x.R, 0) / -l.reduce((s, x) => s + x.R, 0) : 99, dd, tot: c }; };
const data = Object.fromEntries(SYMS.map(s => { const C = JSON.parse(fs.readFileSync(`${dir}/${s}_${tf}.json`)); return [s, { C, I: S.prep(C) }]; }));
const grid = { slAtr: [0.8, 1.2, 1.8], tpR: [1, 1.5, 2.5] };
const fmt = s => `n=${String(s.n).padStart(4)} exp=${s.exp.toFixed(3)}R wr=${s.wr.toFixed(0)}% pf=${s.pf.toFixed(2)} dd=${s.dd.toFixed(1)}R tot=${s.tot.toFixed(1)}R`;
const results = {};
for (const [key, strat] of Object.entries(S.STRATS)) {
  let best = null;
  for (const slAtr of grid.slAtr) for (const tpR of grid.tpR) {           // choose params on TRAIN (first 60%) only
    const p = { slAtr, tpR }; let all = [];
    for (const s of SYMS) { const { C, I } = data[s]; all = all.concat(sim(C, I, strat, p, 210, Math.floor(C.length * 0.6))); }
    const st = stats(all); if (st.n >= 40 && (!best || st.exp > best.st.exp)) best = { p, st };
  }
  if (!best) { console.log(key, 'no params with enough trades'); continue; }
  let test = [], per = {};
  for (const s of SYMS) { const { C, I } = data[s]; const t = sim(C, I, strat, best.p, Math.floor(C.length * 0.6), C.length); per[s] = stats(t); test = test.concat(t); }
  results[key] = { params: best.p, train: best.st, test: stats(test), per };
  console.log(`\n${strat.name} [${tf}] params=${JSON.stringify(best.p)}\n  TRAIN ${fmt(best.st)}\n  TEST  ${fmt(results[key].test)}`);
  console.log('  per-symbol TEST: ' + SYMS.map(s => `${s.slice(0, -4)} ${per[s].exp.toFixed(2)}R(${per[s].n})`).join('  '));
}
fs.writeFileSync(`${dir}/results_${tf}.json`, JSON.stringify(results, null, 1));
