// node test_core.js – engine logic against controlled prices (core.js with a stubbed exchange)
const fs = require('fs'), assert = require('assert');
globalThis.PTStrat = require('./strategies.js');
const createCore = new Function(fs.readFileSync(__dirname + '/core.js', 'utf8') + '\nreturn createCore;')();
let PX = 100, CANDLES = null; const FEE = 0.00055;
const io = { state: {}, saves: 0, save: async () => { io.saves++; },
  get: async (ex, path) => {
    if (path.includes('klines')) return CANDLES || [[Math.floor(Date.now() / 60000) * 60000, PX, PX, PX, PX, 1]];
    if (path.includes('depth')) return { bids: [[PX, 1]], asks: [[PX, 1]] };
    if (path.includes('ticker/price')) return { price: String(PX) };
    throw new Error('unexpected ' + path); } };
const core = createCore(io), S = io.state;
const reset = () => { for (const k of Object.keys(S)) delete S[k]; Object.assign(S, { balance: 450, start: 450, next: 1, positions: [] }); CANDLES = null; PX = 100; };
const candle = (hi, lo, c) => { const t = (Math.floor(Date.now() / 60000) + 1) * 60000; CANDLES = [[t, 100, hi, lo, c || hi, 1]]; S.positions.forEach(p => p.last_check -= 120); };
const T = { exchange: 'mexc', symbol: 'BTC/USDT:USDT', order_type: 'market' }, near = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, a + ' != ' + b);
(async () => {
  reset(); await core.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 104, source: 'pullback' }); near(S.balance, 450 - 500 * FEE);
  candle(105, 100); await core.tick(); let p = S.positions[0]; assert.equal(p.close_reason, 'TP'); near(S.balance, 450 + 20 - 500 * FEE - 104 * 5 * FEE);
  const st = (await core.route('stats', null, {})).body.stats[0]; assert.equal(st.strategy, 'pullback'); assert.equal(st.n, 1); assert.ok(st.avg_r > 0); console.log('ok TP + fees + per-strategy stats');
  reset(); await core.openTrade({ ...T, side: 'SHORT', margin: 50, leverage: 10, sl: 102, tp1: 95 }); candle(103, 99); await core.tick(); assert.equal(S.positions[0].close_reason, 'SL'); console.log('ok SL');
  reset(); await core.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 104 }); candle(110, 90); await core.tick(); assert.equal(S.positions[0].close_reason, 'SL'); console.log('ok SL priority');
  reset(); await core.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 98, tp1: 102, tp2: 106 }); candle(103, 100); await core.tick(); p = S.positions[0];
  assert.ok(p.status === 'open' && p.tp1_hit && Math.abs(p.qty - 2.5) < 1e-9 && p.sl === 100); candle(101, 99); await core.tick(); assert.equal(S.positions[0].close_reason, 'SL-BE'); console.log('ok TP1 partial + BE');
  reset(); await core.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 5, sl: 94, tp1: 110, order_type: 'limit', limit_price: 95 }); assert.equal(S.positions[0].status, 'pending'); candle(101, 94.5); await core.tick(); assert.equal(S.positions[0].status, 'open'); console.log('ok limit');
  reset(); await core.openTrade({ ...T, side: 'LONG', margin: 100, leverage: 20 }); candle(100, 90); await core.tick(); assert.equal(S.positions[0].close_reason, 'LIQUIDATION'); console.log('ok liquidation');
  reset(); for (const b of [{ side: 'LONG', margin: 100, leverage: 5, sl: 105, tp1: 110 }, { side: 'LONG', margin: 1000, leverage: 5, sl: 90 }, { side: 'SHORT', margin: 100, leverage: 5, sl: 110, tp1: 120 }, { side: 'LONG', margin: 10, leverage: 5, exchange: 'evil' }]) await assert.rejects(core.openTrade({ ...T, ...b })); console.log('ok validation');
  const r = await core.route('reset', null, {}); assert.equal(S.balance, 450); assert.equal(S.positions.length, 0); console.log('ok reset');
  // signals: synthetic uptrend with pullbacks produces valid structured signals
  const rng = seed => () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
  const walk = (n, dr, ms, seed) => { const r = rng(seed); let c = 500; return Array.from({ length: n }, (_, i) => { const o = c; c = o * (1 + dr + (r() - 0.5) * 0.01); return [i * ms, o, Math.max(o, c) * 1.002, Math.min(o, c) * 0.998, c, 100 + r() * 50]; }); };
  let cnt = 0; const sides = new Set(), kinds = new Set();
  for (let seed = 1; seed <= 80; seed++) for (const dr of [0.002, -0.002, 0.001, -0.001]) {
    const a = walk(300, dr, 9e5, seed), b = walk(250, dr * 3, 36e5, seed + 999), list = [];
    const c = core.compositeSignal('mexc', 'BTC/USDT:USDT', a, b, 450); if (c) list.push(c); core.ruleSignals('mexc', 'BTC/USDT:USDT', a, 450).forEach(s => list.push(s));
    for (const g of list) { cnt++; sides.add(g.side); kinds.add(g.strategy); const d = g.side === 'LONG' ? 1 : -1;
      assert.ok(d * (g.tp1 - g.entry) > 0 && d * (g.entry - g.sl) > 0 && g.margin <= 450 && g.risk_usd <= 450 * 0.0201 + 1e-6 && g.discussion.length === 3 && g.bt); }
  }
  assert.ok(cnt > 0 && sides.has('LONG') && sides.has('SHORT') && kinds.has('composite')); console.log('ok signals', cnt, [...kinds].join('/'));
  console.log('ALL PASSED');
})().catch(e => { console.error('FAIL', e); process.exit(1); });
