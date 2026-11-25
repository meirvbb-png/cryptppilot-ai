// Cloudflare Worker: serves the paper-trader UI + a read-only relay to Bybit / MEXC PUBLIC market data.
// (Browsers can't call those APIs directly - no CORS. The Worker runs at the edge near the user.)
const FILES = __FILES__;
const RELAY = {
  bybit: { bases: ['https://api.bybit.com/', 'https://api.bytick.com/', 'https://api.bybit.nl/', 'https://api.byhkbit.com/'], allow: /^v5\/market\/(kline|tickers|orderbook)$/ },
  mexc:  { bases: ['https://api.mexc.com/'], allow: /^api\/v3\/(klines|ticker\/price|depth)$/ },
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
      let up, last = [];
      for (const b of r.bases) {
        try { up = await fetch(b + path + u.search, { headers: { accept: 'application/json', 'user-agent': 'Mozilla/5.0 (compatible; paper-trader)' } }); } catch (e) { last.push(b + ' ' + e.message); continue; }
        if (up.status !== 403 && up.status < 500) break;
        last.push(b + ' ' + up.status);
      }
      if (!up || up.status === 403 || up.status >= 500) return new Response(JSON.stringify({ error: 'upstream', tried: last, colo: req.cf && req.cf.colo, country: req.cf && req.cf.country }), { status: 502, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
      return new Response(up.body, { status: up.status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-upstream': up.url.split('/')[2] } });
    }
    const name = u.pathname === '/' ? 'index.html' : u.pathname.slice(1);
    const body = FILES[name];
    if (body === undefined) return new Response('not found', { status: 404 });
    return new Response(body, { headers: { 'content-type': TYPES[name.split('.').pop()] || 'text/plain', 'cache-control': 'no-cache' } });
  },
};
