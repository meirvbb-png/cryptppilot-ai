// node test_engine.js  – runs local.js with a stubbed browser + controlled prices
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let PX = 100, CANDLES = null;
const store = {};
const win = { addEventListener() {} };
const ctx = { window: win, localStorage: { getItem: k => store[k] || null, setItem: (k, v) => store[k] = v }, location: { href: 'http://x/' },
  Response, URL, Math, Date, JSON, console, setInterval: () => 0, setTimeout, Promise, Error, Array, Object, String, Number, isNaN, parseFloat };
ctx.fetch = async (url) => {
  const mk = o => ({ ok: true, json: async () => o });
  if (url.includes('klines')) return mk(CANDLES || [[Math.floor(Date.now() / 60000) * 60000, PX, PX, PX, PX, 1]]);
  if (url.includes('depth')) return mk({ bids: [[PX, 1]], asks: [[PX, 1]] });
  if (url.includes('ticker/price')) return mk({ price: String(PX) });
  throw new Error('unexpected ' + url);
};
win.fetch = ctx.fetch; ctx.window.fetch = ctx.fetch; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/local.js', 'utf8'), ctx);
const PT = win.__PT;
const FEE = 0.00055;
const reset = () => { PT.S = { balance: 450, start: 450, next: 1, positions: [] }; CANDLES = null; PX = 100; };
const candle = (hi, lo, c) => { const t = (Math.floor(Date.now() / 60000) + 1) * 60000; CANDLES = [[t, 100, hi, lo, c || hi, 1]]; PT.S.positions.forEach(p => p.last_check -= 120); };
const T = { exchange: 'mexc', symbol: 'BTC/USDT:USDT', order_type: 'market' };
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, a + ' != ' + b);
(async () => {
  reset(); await PT.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 104 });
  near(PT.S.balance, 450 - 500 * FEE); candle(105, 100); await PT.tick();
  let p = PT.S.positions[0]; assert.equal(p.status, 'closed'); assert.equal(p.close_reason, 'TP');
  near(PT.S.balance, 450 + 20 - 500 * FEE - 104 * 5 * FEE); console.log('ok long TP + fees');

  reset(); await PT.openTrade({ ...T, side: 'SHORT', margin: 50, leverage: 10, sl: 102, tp1: 95 }); candle(103, 99); await PT.tick();
  p = PT.S.positions[0]; assert.equal(p.close_reason, 'SL'); assert.ok(p.realized < 0); console.log('ok short SL');

  reset(); await PT.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 104 }); candle(110, 90); await PT.tick();
  assert.equal(PT.S.positions[0].close_reason, 'SL'); console.log('ok SL priority');

  reset(); await PT.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 102, tp2: 106 }); candle(103, 100); await PT.tick();
  p = PT.S.positions[0]; assert.ok(p.status === 'open' && p.tp1_hit && Math.abs(p.qty - 2.5) < 1e-9 && p.sl === 100);
  candle(101, 99); await PT.tick(); assert.equal(PT.S.positions[0].close_reason, 'SL-BE'); console.log('ok TP1 partial + breakeven');

  reset(); await PT.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 94, tp1: 110, order_type: 'limit', limit_price: 95 });
  assert.equal(PT.S.positions[0].status, 'pending'); candle(101, 94.5); await PT.tick(); assert.equal(PT.S.positions[0].status, 'open'); console.log('ok limit fill');

  reset(); await PT.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 20 }); candle(100, 90); await PT.tick();
  p = PT.S.positions[0]; assert.equal(p.close_reason, 'LIQUIDATION'); assert.ok(p.realized < -89); console.log('ok liquidation');

  reset();
  for (const bad of [{ side: 'LONG', margin: 100, leverage: 5, sl: 105, tp1: 110 }, { side: 'LONG', margin: 1000, leverage: 5, sl: 90 }, { side: 'SHORT', margin: 100, leverage: 5, sl: 110, tp1: 120 }])
    await assert.rejects(PT.openTrade({ ...T, ...bad })); console.log('ok validation');

  // agents on seeded random walks with drift: invariants must hold for every signal, and both sides must occur
  const rng = seed => () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
  const walk = (n, drift, tfms, seed) => { const r = rng(seed); let c = 500; return Array.from({ length: n }, (_, i) => { const o = c; c = o * (1 + drift + (r() - 0.5) * 0.01); return [i * tfms, o, Math.max(o, c) * 1.002, Math.min(o, c) * 0.998, c, 100 + r() * 50]; }); };
  const sides = new Set(); let count = 0;
  for (let seed = 1; seed <= 80; seed++) for (const dr of [0.002, -0.002, 0.001, -0.001]) {
    const g = PT.buildSignal('mexc', 'BTC/USDT:USDT', walk(300, dr, 9e5, seed), walk(250, dr * 3, 36e5, seed + 999), 450);
    if (!g) continue; count++; sides.add(g.side);
    const d = g.side === 'LONG' ? 1 : -1;
    assert.ok(d * (g.tp1 - g.entry) > 0 && d * (g.tp2 - g.tp1) > 0 && d * (g.entry - g.sl) > 0);
    assert.ok(g.risk_pct >= 0.015 && g.risk_pct <= 0.02 && g.risk_usd <= 9.0001 && g.margin <= 450 && g.discussion.length === 3);
  }
  assert.ok(count > 0 && sides.has('LONG') && sides.has('SHORT'), 'signals ' + count + ' ' + [...sides]);
  console.log('ok agents', count, 'signals', [...sides].join('/'));
  console.log('ALL PASSED');
})().catch(e => { console.error('FAIL', e); process.exit(1); });
