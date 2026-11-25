// Cloudflare Worker: serves the paper-trader UI + a read-only relay to Bybit / MEXC PUBLIC market data.
// (Browsers can't call those APIs directly - no CORS. The Worker runs at the edge near the user.)
const FILES = __FILES__;
const RELAY = {
  bybit: { base: 'https://api.bybit.com/', allow: /^v5\/market\/(kline|tickers|orderbook)$/ },
  mexc:  { base: 'https://api.mexc.com/',  allow: /^api\/v3\/(klines|ticker\/price|depth)$/ },
};
const TYPES = { html: 'text/html; charset=utf-8', js: 'application/javascript; charset=utf-8' };
export default {
  async fetch(req) {
    const u = new URL(req.url);
    if (u.pathname.startsWith('/x/')) {
      if (req.method !== 'GET') return new Response('method not allowed', { status: 405 });
      const [, , ex, ...rest] = u.pathname.split('/');
      const path = rest.join('/'), r = RELAY[ex];
      if (!r || !r.allow.test(path)) return new Response('forbidden', { status: 403 });
      const up = await fetch(r.base + path + u.search, { headers: { accept: 'application/json' }, cf: { cacheTtl: 1, cacheEverything: true } });
      return new Response(up.body, { status: up.status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'access-control-allow-origin': '*' } });
    }
    const name = u.pathname === '/' ? 'index.html' : u.pathname.slice(1);
    const body = FILES[name];
    if (body === undefined) return new Response('not found', { status: 404 });
    return new Response(body, { headers: { 'content-type': TYPES[name.split('.').pop()] || 'text/plain', 'cache-control': 'no-cache' } });
  },
};
