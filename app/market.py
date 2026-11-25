"""Market data: live Bybit trades -> 1-minute bars (with taker buy volume), plus history bootstrap.

History sources, in order: local npz cache -> shipped snapshot (deploy/history) -> research archive (if present) ->
public.bybit.com tick archives (last ARCHIVE_DAYS days, gives buy/sell split) ->
Bybit REST klines (older days and today's gap; no buy/sell split)."""
import asyncio, datetime as dt, glob, io, json, logging, time
import httpx, numpy as np, pandas as pd, websockets
from . import config as C

log = logging.getLogger("market")
REST_STATE = {"ok": None, "err": ""}
COLS = ["open", "high", "low", "close", "volume", "quote_vol", "buy_vol", "trades"]


class BarStore:
    def __init__(self, symbols):
        self.symbols = symbols
        self.bars = {s: pd.DataFrame(columns=COLS, dtype=float) for s in symbols}
        self.cur = {}          # symbol -> dict of the minute bar being built
        self.last = {}         # symbol -> (ts_ms, price)
        self.listeners_bar = []    # async fn(symbol, minute_ts)
        self.listeners_tick = []   # fn(symbol, ts_ms, price)
        self.listeners_repair = [] # fn() after gaps were filled
        self.status = {"ws": "disconnected", "history": "loading", "msg": ""}
        self._arch_tried = set()
        self.connected_min = None   # minute in which the websocket (re)connected -> that bar is partial

    # ---------------- history ----------------
    def _cache(self, s):
        return C.DATA_DIR / f"bars_{s}.npz"

    def bootstrap(self):
        for s in self.symbols:
            try:
                hist = self._bootstrap_symbol(s)
                self.bars[s] = _merge([self.bars[s], hist])     # keep live bars closed during bootstrap
                log.info("history %s: %d bars, %s -> %s", s, len(self.bars[s]),
                         self.bars[s].index.min(), self.bars[s].index.max())
            except Exception as e:
                log.exception("bootstrap %s failed: %s", s, e)
        self.status["history"] = "ready"
        self.update_warn()

    def update_warn(self):
        need = pd.Timedelta(days=60)
        short = [s for s in self.symbols if not len(self.bars[s]) or self.bars[s].index[-1] - self.bars[s].index[0] < need]
        if REST_STATE["ok"] is False and short:
            self.status["warn"] = ("לא ניתן להוריד היסטוריית נרות מ-Bybit (api.bybit.com חסום מהרשת הזו). "
                                   "חלק מהבוטים יישארו במצב 'מתחמם' עד שיצטברו נתונים. "
                                   "בדוק חיבור אינטרנט / VPN / חומת אש.")
        else:
            self.status.pop("warn", None)

    def _bootstrap_symbol(self, s):
        now = pd.Timestamp.utcnow().tz_localize(None).floor("min")
        start = (now - pd.Timedelta(days=C.HISTORY_DAYS)).floor("D")
        parts = []
        if self._cache(s).exists():
            parts.append(load_bars(self._cache(s)))
        elif (C.SEED_DIR / f"bars_{s}.npz").exists():
            parts.append(load_bars(C.SEED_DIR / f"bars_{s}.npz"))
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
        save_bars(df, self._cache(s))
        self.status["msg"] = ""
        return df

    def save(self):
        for s in self.symbols:
            try:
                save_bars(self.bars[s].loc[pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=C.HISTORY_DAYS):],
                          self._cache(s))
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
        if self.connected_min is not None and c["t"] <= self.connected_min:
            return              # bar started before we were connected -> incomplete; repair() refills it
        row = pd.DataFrame([{k: c[k] for k in COLS}], index=[c["t"]])
        df = self.bars[s]
        if len(df) and c["t"] <= df.index[-1]:
            return
        self.bars[s] = pd.concat([df, row])
        for f in self.listeners_bar:
            asyncio.get_running_loop().create_task(f(s, c["t"]))

    async def flush_minutes(self):
        """Close a minute bar even if no new trade arrived after the minute ended."""
        while True:
            await asyncio.sleep(1)
            now_min = pd.Timestamp(int(time.time() * 1000) // 60000 * 60000, unit="ms")
            for s in list(self.cur):
                if self.cur[s]["t"] < now_min - pd.Timedelta(seconds=0) and time.time() % 60 > 2:
                    self._close_bar(s)

    def repair(self, hours=48):
        """Fill missing closed minutes of the last `hours` from Bybit REST klines."""
        now = pd.Timestamp(int(time.time() * 1000) // 60000 * 60000, unit="ms")
        for s in self.symbols:
            df = self.bars[s]
            if not len(df):
                continue
            start = max(df.index[0], now - pd.Timedelta(hours=hours))
            fill = _rest_fill(s, df.loc[start:], start, now - pd.Timedelta("1min"))
            if fill is not None and len(fill):
                self.bars[s] = _merge([self.bars[s], fill])
                log.info("repair %s: filled %d missing minutes from REST", s, len(fill))
            # REST blocked (e.g. US servers): fill gaps of past days from the daily tick archive
            df = self.bars[s]
            for k in range(1, C.ARCHIVE_DAYS + 1):
                day = (now - pd.Timedelta(days=k)).normalize()
                if day < df.index[0].normalize() or (s, day) in self._arch_tried:
                    continue
                have = len(df.loc[day:day + pd.Timedelta("1439min")])
                if have >= 1438:
                    continue
                self._arch_tried.add((s, day))
                b = _archive_day(s, day.strftime("%Y-%m-%d"))
                if b is not None and len(b):
                    self.bars[s] = df = _merge([df, b])
                    log.info("repair %s: filled %s from tick archive (%d -> %d bars)", s, day.date(), have, len(b))

    async def run_repair(self):
        while True:
            await asyncio.sleep(300)
            if self.status["history"] == "ready":
                await asyncio.to_thread(self.repair)
                self.update_warn()
                for f in self.listeners_repair:
                    f()

    async def run_ws(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(C.WS_PUBLIC, ping_interval=20, open_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"publicTrade.{s}" for s in self.symbols]}))
                    self.status["ws"] = "connected"; backoff = 1
                    self.connected_min = pd.Timestamp(int(time.time() * 1000) // 60000 * 60000, unit="ms")
                    self.cur.clear()
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


def save_bars(df, path, dtype=np.float64):
    """Compressed numpy archive (no pyarrow needed)."""
    np.savez_compressed(path, t=df.index.as_unit("ms").asi8, **{c: df[c].to_numpy(dtype) for c in COLS})


def load_bars(path):
    z = np.load(path)
    return pd.DataFrame({c: z[c].astype(np.float64) for c in COLS},
                        index=pd.to_datetime(z["t"], unit="ms"))


def _from_research(files):
    import polars as pl                       # research archive only (dev machine)
    df = pl.concat([pl.read_parquet(f) for f in files]).to_pandas()
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    return df


def _archive_day(s, day):
    """One day of Bybit tick archive -> 1m bars with taker buy volume.
    Streams to disk and aggregates in chunks so memory stays ~100MB even for BTC (~3M trades/day)."""
    import tempfile, os
    url = f"https://public.bybit.com/trading/{s}/{s}{day}.csv.gz"
    fd, path = tempfile.mkstemp(suffix=".csv.gz", dir=C.DATA_DIR); os.close(fd)
    try:
        with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        parts = []
        for ch in pd.read_csv(path, usecols=["timestamp", "side", "size", "price"], chunksize=500_000):
            ch["t"] = pd.to_datetime((ch.timestamp * 1000).astype("int64") // 60000 * 60000, unit="ms")
            ch["notional"] = ch["size"] * ch["price"]
            ch["buy"] = ch["size"].where(ch.side == "Buy", 0.0)
            g = ch.groupby("t", sort=True)
            parts.append(pd.DataFrame({"open": g.price.first(), "high": g.price.max(), "low": g.price.min(),
                                       "close": g.price.last(), "volume": g["size"].sum(), "quote_vol": g.notional.sum(),
                                       "buy_vol": g.buy.sum(), "trades": g.size()}))
            del ch, g
    except Exception as e:
        log.warning("archive %s %s: %s", s, day, e)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not parts:
        return None
    a = pd.concat(parts)                      # a minute can straddle two chunks -> combine in file order
    g = a.groupby(level=0, sort=True)
    return pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(),
                         "volume": g.volume.sum(), "quote_vol": g.quote_vol.sum(), "buy_vol": g.buy_vol.sum(),
                         "trades": g.trades.sum()}).astype(float)


def _rest_fill(s, df, start, now, client=None):
    """Fetch 1m klines from Bybit REST for missing ranges (buy_vol unknown -> NaN)."""
    idx = pd.date_range(start, now - pd.Timedelta("1min"), freq="1min")
    missing = idx.difference(df.index) if len(df) else idx
    if len(missing) == 0:
        return None
    out = []
    # walk missing minutes in 1000-bar windows
    blocks = pd.Series(missing).groupby((pd.Series(missing).diff() != pd.Timedelta("1min")).cumsum())
    own = client is None
    client = client or httpx.Client(base_url=C.REST_BASE["mainnet"], timeout=20)
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
        REST_STATE.update(ok=False, err=str(e)[:80])
        log.warning("REST kline %s unavailable (%s) - live data will fill forward", s, str(e)[:80])
        return None
    finally:
        if own:
            client.close()
    REST_STATE.update(ok=True, err="")
    if not out:
        return None
    k = pd.DataFrame(out, columns=["t", "open", "high", "low", "close", "volume", "quote_vol"]).set_index("t").sort_index()
    k = k[~k.index.duplicated()]
    k["buy_vol"] = np.nan; k["trades"] = np.nan
    return k
