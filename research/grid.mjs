// Robustness: full parameter grid for one strategy, train vs test. node grid.mjs <dir> <tf> <strat>
import fs from 'fs'; import { createRequire } from 'module';
const S = createRequire(import.meta.url)('../papertrader-web/strategies.js');
const [dir, tf, key] = process.argv.slice(2), SYMS = ['BTC','ETH','SOL','XRP','DOGE','BNB'].map(s => s + 'USDT');
const FEE = +(process.env.FEE || 0.00055), SLIP = +(process.env.SLIP || 0.0002), MAXBARS = 36;
function sim(C, I, strat, p, from, to) { const t = []; let i = from; while (i < to - 2) { const sg = strat.signal(I, i, p); if (!sg) { i++; continue; } const d = sg.side === 'LONG' ? 1 : -1, e = C[i + 1][1] * (1 + d * SLIP), r = Math.abs(e - sg.sl); if (!(r > 0) || r / e > 0.05 || d * (e - sg.sl) <= 0) { i++; continue; } const tp = e + d * sg.tpR * r; let x = null, j = i + 1; for (; j < Math.min(to, i + 1 + MAXBARS); j++) { if (d === 1 ? C[j][3] <= sg.sl : C[j][2] >= sg.sl) { x = sg.sl; break; } if (d === 1 ? C[j][2] >= tp : C[j][3] <= tp) { x = tp; break; } } if (x === null) { j = Math.min(to, i + 1 + MAXBARS) - 1; x = C[j][4]; } x *= 1 - d * SLIP; t.push((d * (x - e) - (e + x) * FEE) / r); i = j + 1; } return t; }
const D = Object.fromEntries(SYMS.map(s => { const C = JSON.parse(fs.readFileSync(`${dir}/${s}_${tf}.json`)); return [s, { C, I: S.prep(C) }]; }));
const f = a => a.length ? `${(a.reduce((s, x) => s + x, 0) / a.length).toFixed(2)}R/${a.length}` : '-';
console.log(`${key} ${tf} FEE=${FEE} SLIP=${SLIP}`); console.log('slAtr tpR   TRAIN        TEST');
for (const slAtr of [0.8, 1.2, 1.8, 2.5]) for (const tpR of [1, 1.5, 2.5, 3.5]) {
  let a = [], b = []; for (const s of SYMS) { const { C, I } = D[s], cut = Math.floor(C.length * 0.6); a = a.concat(sim(C, I, S.STRATS[key], { slAtr, tpR }, 210, cut)); b = b.concat(sim(C, I, S.STRATS[key], { slAtr, tpR }, cut, C.length)); }
  console.log(String(slAtr).padEnd(5), String(tpR).padEnd(5), f(a).padEnd(12), f(b));
}
