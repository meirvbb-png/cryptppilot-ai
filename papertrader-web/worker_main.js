// ---- Cloudflare Worker entry + Durable Object (the always-on paper-trading engine) ----
const BASES = {
  bybit: ['https://api.bybit.com/', 'https://api.bytick.com/', 'https://api.bybit.nl/', 'https://api.byhkbit.com/'],
  mexc: ['https://api.mexc.com/'],
};
const ALLOW = { bybit: /^v5\/market\/(kline|tickers|orderbook)\?/, mexc: /^api\/v3\/(klines|ticker\/price|depth)\?/ };
async function upstream(ex, path) {
  if (!ALLOW[ex] || !ALLOW[ex].test(path)) throw new Error('blocked path');   // read-only public market data only
  const tried = [];
  for (const b of BASES[ex]) {
    try {
      const r = await fetch(b + path, { headers: { accept: 'application/json', 'user-agent': 'Mozilla/5.0 (compatible; paper-trader)' } });
      if (r.ok) return await r.json();
      tried.push(new URL(b).host + ' ' + r.status);
      if (r.status !== 403 && r.status < 500) break;
    } catch (e) { tried.push(new URL(b).host + ' ' + e.message); }
  }
  throw new Error(ex + ' HTTP ' + tried.join(', '));
}
const jres = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });

export class Trader {
  constructor(state, env) { this.state = state; this.env = env; this.core = null; }
  async init() {
    if (this.core) return;
    const S = (await this.state.storage.get('S')) || {};
    this.core = createCore({ state: S, get: upstream, save: () => this.state.storage.put('S', S) });
  }
  async fetch(req) {
    await this.init();
    const u = new URL(req.url), path = u.pathname.replace(/^\/api\//, '');
    if (!(await this.state.storage.getAlarm())) await this.state.storage.setAlarm(Date.now() + 2000);
    if (path === '_poke') return jres({ ok: true });
    let body = {}; if (req.method === 'POST') { try { body = await req.json(); } catch (e) { } }
    const r = await this.core.route(path, u.searchParams, body);
    return jres(r.body, r.status);
  }
  async alarm() {
    await this.init();
    try { await this.core.tick(); } catch (e) { }
    try { await this.core.signalsStep(); } catch (e) { }
    await this.state.storage.setAlarm(Date.now() + 10000);
  }
}
const TYPES = { html: 'text/html; charset=utf-8', js: 'application/javascript; charset=utf-8' };
export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    if (u.pathname.startsWith('/api/')) return env.TRADER.get(env.TRADER.idFromName('main')).fetch(req);
    const name = u.pathname === '/' ? 'index.html' : u.pathname.slice(1), body = FILES[name];
    if (body === undefined) return new Response('not found', { status: 404 });
    return new Response(body, { headers: { 'content-type': TYPES[name.split('.').pop()] || 'text/plain', 'cache-control': 'no-cache' } });
  },
  async scheduled(event, env, ctx) {   // cron watchdog: makes sure the engine alarm loop is alive
    ctx.waitUntil(env.TRADER.get(env.TRADER.idFromName('main')).fetch('https://do/api/_poke'));
  },
};
