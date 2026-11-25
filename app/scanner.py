"""Market scanner (watch-list only - it never trades).

Sources (all free, no keys):
  * Bybit public WebSocket tickers for every USDT perpetual: 24h change, turnover, open interest,
    funding. Turnover / OI are compared with the scanner's own rolling history to find surges.
  * Bybit symbol list from public.bybit.com (works everywhere) -> newly listed symbols.
  * Bybit REST instruments (launchTime) and announcements (upcoming listings) when reachable
    (blocked from US servers, works from Israel).
  * CoinGecko trending searches (proxy for social buzz) and market data.
  * Reddit ticker mentions (r/CryptoCurrency, r/altcoin) when reachable.
Every row gets a 0-100 score and explicit risk flags.
"""
import asyncio, json, logging, re, time
from collections import Counter, defaultdict, deque
import httpx, websockets
from . import config as C, db

log = logging.getLogger("scanner")
UA = {"User-Agent": "Mozilla/5.0 (cryptopilot-scanner)"}
STATE = {"updated": None, "rows": [], "trending": [], "new_listings": [], "announcements": [], "sources": {}}
STABLE = {"USDCUSDT", "USDEUSDT", "FDUSDUSDT", "DAIUSDT", "TUSDUSDT"}

db.x("""CREATE TABLE IF NOT EXISTS scan_symbols(symbol TEXT PRIMARY KEY, first_seen REAL, launch_ms REAL)""")


class Scanner:
    def __init__(self):
        self.tick = {}                                   # symbol -> latest ticker dict
        self.hist = defaultdict(lambda: deque(maxlen=200))  # symbol -> (ts, turnover24h, oi_value)
        self.symbols = []

    # ------------------------------------------------------------------ sources
    def _src(self, name, ok, note=""):
        STATE["sources"][name] = dict(ok=ok, note=note, ts=time.time())

    def bybit_symbols(self):
        try:
            r = httpx.get("https://public.bybit.com/trading/", timeout=30, headers=UA)
            syms = sorted(set(re.findall(r'href="([A-Z0-9]+USDT)/"', r.text)) - STABLE)
            self._src("bybit_list", True, f"{len(syms)} חוזים")
        except Exception as e:
            self._src("bybit_list", False, "לא זמין כרגע"); return self.symbols
        now = time.time()
        known = {r["symbol"] for r in db.q("SELECT symbol FROM scan_symbols")}
        first_run = not known
        for s in syms:
            if s not in known:
                # first run = baseline (age unknown); later appearances are real new listings
                db.x("INSERT OR IGNORE INTO scan_symbols VALUES(?,?,NULL)", (s, 0 if first_run else now))
        self.symbols = syms
        return syms

    def bybit_rest(self):
        """launchTime of every linear instrument + upcoming-listing announcements (blocked in the US)."""
        try:
            cur, n = "", 0
            while True:
                j = httpx.get(f"{C.REST_BASE['mainnet']}/v5/market/instruments-info",
                              params=dict(category="linear", limit=1000, cursor=cur), timeout=20).json()
                for it in j["result"]["list"]:
                    db.x("UPDATE scan_symbols SET launch_ms=? WHERE symbol=?", (float(it.get("launchTime") or 0), it["symbol"]))
                    n += 1
                cur = j["result"].get("nextPageCursor") or ""
                if not cur:
                    break
            a = httpx.get(f"{C.REST_BASE['mainnet']}/v5/announcements/index",
                          params=dict(locale="en-US", type="new_crypto", limit=15), timeout=20).json()
            STATE["announcements"] = [dict(title=x["title"], url=x["url"], ts=x["dateTimestamp"] / 1000)
                                      for x in a["result"]["list"]]
            self._src("bybit_rest", True, f"{n} מכשירים, {len(STATE['announcements'])} הודעות")
        except Exception as e:
            self._src("bybit_rest", False, "חסום מהשרת הזה (עובד מישראל)")

    def coingecko(self):
        out = {}
        try:
            t = httpx.get("https://api.coingecko.com/api/v3/search/trending", timeout=20, headers=UA).json()
            STATE["trending"] = []
            for i, c in enumerate(t.get("coins", [])[:15]):
                it = c["item"]; d = it.get("data", {})
                sym = it["symbol"].upper()
                row = dict(rank=i + 1, symbol=sym, name=it["name"], mcap_rank=it.get("market_cap_rank"),
                           price=d.get("price"), chg24=(d.get("price_change_percentage_24h") or {}).get("usd"),
                           on_bybit=f"{sym}USDT" in self.symbols)
                STATE["trending"].append(row); out[sym] = i + 1
            self._src("coingecko", True, f"{len(STATE['trending'])} טרנדים")
        except Exception as e:
            self._src("coingecko", False, "לא זמין כרגע")
        return out

    def reddit(self):
        cnt = Counter()
        bases = {s[:-4] for s in self.symbols}
        try:
            for sub in ("CryptoCurrency", "altcoin", "CryptoMoonShots"):
                r = httpx.get(f"https://www.reddit.com/r/{sub}/new.json?limit=100", timeout=20, headers=UA)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                for p in r.json()["data"]["children"]:
                    text = (p["data"].get("title", "") + " " + p["data"].get("selftext", "")[:500])
                    for w in set(re.findall(r"\$?\b([A-Z]{2,10})\b", text)):
                        if w in bases and w not in {"USD", "ETF", "CEO", "ATH", "THE", "AND", "FOR"}:
                            cnt[w] += 1
            self._src("reddit", True, f"{sum(cnt.values())} אזכורים")
        except Exception as e:
            self._src("reddit", False, "חוסם את השרת הזה" if "403" in str(e) else "לא זמין כרגע")
        return cnt

    async def run_ws(self):
        """Ticker stream for every USDT perpetual (several connections, 200 symbols each)."""
        while not self.symbols:
            await asyncio.sleep(5)
        chunks = [self.symbols[i:i + 200] for i in range(0, len(self.symbols), 200)]
        await asyncio.gather(*(self._ws(ch) for ch in chunks))

    async def _ws(self, syms):
        backoff = 1
        while True:
            try:
                async with websockets.connect(C.WS_PUBLIC, ping_interval=20, open_timeout=20, max_size=2**23) as ws:
                    for s in syms:      # one per request: a delisted symbol must not sink its neighbours
                        await ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{s}"]}))
                    self._src("bybit_ws", True, f"{len(self.tick)} מטבעות בזמן אמת"); backoff = 1
                    async for msg in ws:
                        m = json.loads(msg); d = m.get("data")
                        if not d or not m.get("topic", "").startswith("tickers."):
                            continue
                        cur = self.tick.setdefault(d["symbol"], {})
                        cur.update({k: v for k, v in d.items() if v not in (None, "")})
            except Exception as e:
                self._src("bybit_ws", False, type(e).__name__)
                await asyncio.sleep(backoff); backoff = min(60, backoff * 2)

    # ------------------------------------------------------------------ scoring
    def scan(self):
        self.bybit_symbols()
        self.bybit_rest()
        trend = self.coingecko()
        red = self.reddit()
        now = time.time()
        meta = {r["symbol"]: r for r in db.q("SELECT * FROM scan_symbols")}
        rows = []
        for s, t in list(self.tick.items()):
            try:
                px = float(t["lastPrice"]); chg = 100 * float(t.get("price24hPcnt", 0))
                turn = float(t.get("turnover24h", 0)); oi = float(t.get("openInterestValue", 0))
                fund = 100 * float(t.get("fundingRate", 0))
            except (KeyError, ValueError):
                continue
            h = self.hist[s]; h.append((now, turn, oi))
            # compare with the value ~24h ago if we have it, else the oldest we have
            old = next((x for x in h if now - x[0] <= 26 * 3600), h[0])
            age_h = (now - old[0]) / 3600
            turn_x = turn / old[1] if old[1] > 0 and age_h >= 1 else 1.0
            oi_chg = 100 * (oi / old[2] - 1) if old[2] > 0 and age_h >= 1 else 0.0
            base = s[:-4]
            m = meta.get(s, {})
            launch = (m.get("launch_ms") or 0) / 1000 or (m.get("first_seen") or 0)
            days_listed = (now - launch) / 86400 if launch else None
            flags, score = [], 0.0
            score += min(25, max(0, chg) * 0.8)                       # momentum
            score += min(20, max(0, (turn_x - 1) * 10))               # volume surge vs our history
            score += min(15, max(0, oi_chg) * 0.5)                    # new money entering (OI)
            if base in trend:
                score += 20 - trend[base]                             # social search buzz
            score += min(10, red.get(base, 0) * 2)                    # Reddit mentions
            if days_listed is not None and days_listed < 30:
                score += 10; flags.append(f"חדש ב-Bybit ({days_listed:.0f} ימים)")
            if turn < 2_000_000:
                flags.append("נזילות נמוכה"); score -= 10
            if chg > 40:
                flags.append("זינוק חריג - סכנת פאמפ")
            if abs(fund) >= 0.1:
                flags.append(f"מימון קיצוני {fund:+.3f}%")
            rows.append(dict(symbol=s, base=base, price=px, chg24=chg, turnover=turn, turn_x=turn_x, oi=oi,
                             oi_chg=oi_chg, funding=fund, trending=trend.get(base), reddit=red.get(base, 0),
                             days_listed=days_listed, score=round(max(0, min(100, score))), flags=flags))
        rows.sort(key=lambda r: r["score"], reverse=True)
        if STATE["sources"].get("bybit_ws", {}).get("ok"):
            self._src("bybit_ws", True, f"{len(self.tick)} מטבעות בזמן אמת")
        STATE["rows"] = rows
        STATE["new_listings"] = sorted([r for r in rows if r["days_listed"] is not None and r["days_listed"] < 30],
                                       key=lambda r: r["days_listed"])
        STATE["updated"] = now
        log.info("scan: %d symbols, top %s", len(rows), [r["symbol"] for r in rows[:5]])

    async def loop(self):
        await asyncio.to_thread(self.bybit_symbols)
        asyncio.get_running_loop().create_task(self.run_ws())
        await asyncio.sleep(60)                     # let tickers arrive
        while True:
            try:
                await asyncio.to_thread(self.scan)
            except Exception as e:
                log.exception("scan failed: %s", e)
            await asyncio.sleep(600)
