"""Market data: live Bybit trades -> 1-minute bars (with taker buy volume), plus history bootstrap.

History sources, in order: local parquet cache -> research archive (if present) ->
public.bybit.com tick archives (last ARCHIVE_DAYS days, gives buy/sell split) ->
Bybit REST klines (older days and today's gap; no buy/sell split)."""
import asyncio, datetime as dt, glob, io, json, logging, time
import httpx, numpy as np, pandas as pd, polars as pl, websockets
from . import config as C

log = logging.getLogger("market")
COLS = ["open", "high", "low", "close", "volume", "quote_vol", "buy_vol", "trades"]


class BarStore:
    def __init__(self, symbols):
        self.symbols = symbols
        self.bars = {s: pd.DataFrame(columns=COLS, dtype=float) for s in symbols}
        self.cur = {}          # symbol -> dict of the minute bar being built
        self.last = {}         # symbol -> (ts_ms, price)
        self.listeners_bar = []    # async fn(symbol, minute_ts)
        self.listeners_tick = []   # fn(symbol, ts_ms, price)
        self.status = {"ws": "disconnected", "history": "loading", "msg": ""}

    # ---------------- history ----------------
    def _cache(self, s):
        return C.DATA_DIR / f"bars_{s}.parquet"

    def bootstrap(self):
        for s in self.symbols:
            try:
                self.bars[s] = self._bootstrap_symbol(s)
                log.info("history %s: %d bars, %s -> %s", s, len(self.bars[s]),
                         self.bars[s].index.min(), self.bars[s].index.max())
            except Exception as e:
                log.exception("bootstrap %s failed: %s", s, e)
        self.status["history"] = "ready"

    def _bootstrap_symbol(self, s):
        now = pd.Timestamp.utcnow().tz_localize(None).floor("min")
        start = (now - pd.Timedelta(days=C.HISTORY_DAYS)).floor("D")
        parts = []
        if self._cache(s).exists():
            parts.append(pd.read_parquet(self._cache(s)))
        files = sorted(glob.glob(str(C.HIST_DIR / f"{s}_*.parquet")))
        files = [f for f in files if f[-18:-8] >= start.strftime("%Y-%m-%d")]
        if files:
            parts.append(_from_research(files))
        df = _merge(parts)
        # tick archives for recent full days (buy/sell split)
        have = set(df.index.normalize().unique()) if len(df) else set()
        for k in range(C.ARCHIVE_DAYS, 0, -1):
            day = (now - pd.Timedelta(days=k)).normalize()
            if day in have and df.loc[day:day + pd.Timedelta("1439min")].shape[0] >= 1430:
                continue
            self.status["msg"] = f"מוריד ארכיון עסקאות {s} {day.date()}"
            b = _archive_day(s, day.strftime("%Y-%m-%d"))
            if b is not None:
                df = _merge([df, b])
        # REST klines for everything still missing (older days + today's gap)
        self.status["msg"] = f"משלים נרות {s}"
        df = _merge([df, _rest_fill(s, df, start, now)])
        df = df.loc[start:]
        df.to_parquet(self._cache(s))
        self.status["msg"] = ""
        return df

    def save(self):
        for s in self.symbols:
            try:
                self.bars[s].loc[pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=C.HISTORY_DAYS):].to_parquet(self._cache(s))
            except Exception as e:
                log.warning("save %s: %s", s, e)

    def frame(self, s, include_current=False):
        df = self.bars[s]
        if include_current and s in self.cur:
            c = self.cur[s]
            df = pd.concat([df, pd.DataFrame([{k: c[k] for k in COLS}], index=[c["t"]])])
        return df

    # ---------------- live ----------------
    def on_trade(self, s, ts_ms, price, size, is_buy):
        minute = pd.Timestamp(ts_ms // 60000 * 60000, unit="ms")
        c = self.cur.get(s)
        if c is not None and minute > c["t"]:
            self._close_bar(s)
            c = None
        if c is None:
            c = self.cur[s] = dict(t=minute, open=price, high=price, low=price, close=price, volume=0.0,
                                   quote_vol=0.0, buy_vol=0.0, trades=0)
        c["high"] = max(c["high"], price); c["low"] = min(c["low"], price); c["close"] = price
        c["volume"] += size; c["quote_vol"] += size * price; c["trades"] += 1
        if is_buy:
            c["buy_vol"] += size
        self.last[s] = (ts_ms, price)
        for f in self.listeners_tick:
            f(s, ts_ms, price)

    def _close_bar(self, s):
        c = self.cur.pop(s)
        row = pd.DataFrame([{k: c[k] for k in COLS}], index=[c["t"]])
        df = self.bars[s]
        if len(df) and c["t"] <= df.index[-1]:
            return
        # fill silent minutes (no trades) with flat bars so time is continuous
        if len(df):
            gap = pd.date_range(df.index[-1] + pd.Timedelta("1min"), c["t"] - pd.Timedelta("1min"), freq="1min")
            if 0 < len(gap) <= 30:
                p = df.close.iloc[-1]
                filler = pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": 0.0,
                                       "quote_vol": 0.0, "buy_vol": 0.0, "trades": 0}, index=gap)
                row = pd.concat([filler, row])
        self.bars[s] = pd.concat([df, row])
        for f in self.listeners_bar:
            asyncio.get_event_loop().create_task(f(s, c["t"]))

    async def flush_minutes(self):
        """Close a minute bar even if no new trade arrived after the minute ended."""
        while True:
            await asyncio.sleep(1)
            now_min = pd.Timestamp(int(time.time() * 1000) // 60000 * 60000, unit="ms")
            for s in list(self.cur):
                if self.cur[s]["t"] < now_min - pd.Timedelta(seconds=0) and time.time() % 60 > 2:
                    self._close_bar(s)

    async def run_ws(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(C.WS_PUBLIC, ping_interval=20, open_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"publicTrade.{s}" for s in self.symbols]}))
                    self.status["ws"] = "connected"; backoff = 1
                    async for msg in ws:
                        m = json.loads(msg)
                        if not m.get("topic", "").startswith("publicTrade"):
                            continue
                        for t in m["data"]:
                            self.on_trade(t["s"], int(t["T"]), float(t["p"]), float(t["v"]), t["S"] == "Buy")
            except Exception as e:
                self.status["ws"] = f"reconnecting ({type(e).__name__})"
                log.warning("ws error %s; reconnect in %ss", e, backoff)
                await asyncio.sleep(backoff); backoff = min(60, backoff * 2)


# ---------------- helpers ----------------
def _merge(parts):
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return pd.DataFrame(columns=COLS, dtype=float)
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df[COLS].astype(float)


def _from_research(files):
    df = pl.concat([pl.read_parquet(f) for f in files]).to_pandas()
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    return df


def _archive_day(s, day):
    url = f"https://public.bybit.com/trading/{s}/{s}{day}.csv.gz"
    try:
        r = httpx.get(url, timeout=600, follow_redirects=True)
        if r.status_code != 200:
            return None
        t = pl.read_csv(io.BytesIO(r.content), columns=["timestamp", "side", "size", "price"])
    except Exception as e:
        log.warning("archive %s %s: %s", s, day, e)
        return None
    t = t.with_columns((pl.col("timestamp") * 1000).cast(pl.Int64).alias("ms"),
                       (pl.col("size") * pl.col("price")).alias("notional")
                       ).with_columns((pl.col("ms") // 60000 * 60000).alias("t"))
    b = (t.sort("ms").group_by("t", maintain_order=True).agg(
        pl.col("price").first().alias("open"), pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"), pl.col("price").last().alias("close"),
        pl.col("size").sum().alias("volume"), pl.col("notional").sum().alias("quote_vol"),
        pl.col("size").filter(pl.col("side") == "Buy").sum().alias("buy_vol"), pl.len().alias("trades"))).to_pandas()
    b.index = pd.to_datetime(b.pop("t"), unit="ms")
    return b


def _rest_fill(s, df, start, now):
    """Fetch 1m klines from Bybit REST for missing ranges (buy_vol unknown -> NaN)."""
    idx = pd.date_range(start, now - pd.Timedelta("1min"), freq="1min")
    missing = idx.difference(df.index) if len(df) else idx
    if len(missing) == 0:
        return None
    out = []
    # walk missing minutes in 1000-bar windows
    blocks = pd.Series(missing).groupby((pd.Series(missing).diff() != pd.Timedelta("1min")).cumsum())
    client = httpx.Client(base_url=C.REST_BASE["mainnet"], timeout=20)
    try:
        for _, blk in blocks:
            a, b = blk.iloc[0], blk.iloc[-1]
            cur = a
            while cur <= b:
                end = min(b, cur + pd.Timedelta(minutes=999))
                r = client.get("/v5/market/kline", params=dict(category="linear", symbol=s, interval="1",
                               start=int(cur.timestamp() * 1000), end=int(end.timestamp() * 1000), limit=1000))
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                j = r.json()
                rows = j.get("result", {}).get("list", [])
                for k in rows:
                    out.append((pd.Timestamp(int(k[0]), unit="ms"), *map(float, k[1:7])))
                cur = end + pd.Timedelta("1min")
    except Exception as e:
        log.warning("REST kline %s unavailable (%s) - live data will fill forward", s, str(e)[:80])
        return None
    finally:
        client.close()
    if not out:
        return None
    k = pd.DataFrame(out, columns=["t", "open", "high", "low", "close", "volume", "quote_vol"]).set_index("t")
    k["buy_vol"] = np.nan; k["trades"] = np.nan
    return k
