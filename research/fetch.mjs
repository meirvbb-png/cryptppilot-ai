// Downloads historical klines from MEXC spot public API (paged). node fetch.mjs <outdir>
import fs from 'fs';
const out = process.argv[2], SYMS = ['BTC','ETH','SOL','XRP','DOGE','BNB'].map(s => s + 'USDT');
const plan = { '5m': 300000, '15m': 900000, '60m': 3600000 }, DAYS = { '5m': 45, '15m': 90, '60m': 365 };
const ONLY = process.argv[3]; for (const [tf, ms] of Object.entries(plan).filter(([k]) => !ONLY || k === ONLY)) for (const s of SYMS) {
  let start = Date.now() - DAYS[tf] * 86400000, all = [], seen = new Set();
  while (start < Date.now()) {
    const r = await fetch(`https://api.mexc.com/api/v3/klines?symbol=${s}&interval=${tf}&limit=500&startTime=${start}&endTime=${start + 480 * ms}`);
    const d = await r.json(); if (!Array.isArray(d)) break;
    let added = 0; for (const c of d) if (!seen.has(c[0])) { seen.add(c[0]); all.push([+c[0], +c[1], +c[2], +c[3], +c[4], +c[5]]); added++; }
    start = Math.max(start + 480 * ms, d.length ? d[d.length - 1][0] + 1 : 0);
  }
  all.sort((a, b) => a[0] - b[0]);
  fs.writeFileSync(`${out}/${s}_${tf}.json`, JSON.stringify(all)); console.log(s, tf, all.length);
}
