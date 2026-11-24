"""Load 1-minute Bybit bars (built from public.bybit.com tick archives) and resample."""
import glob, os
import numpy as np
import pandas as pd
import polars as pl

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw1m")
TICK = {"BTCUSDT": 0.1, "ETHUSDT": 0.01, "SOLUSDT": 0.01}


def load_1m(sym: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    files = sorted(glob.glob(f"{RAW}/{sym}_*.parquet"))
    if start: files = [f for f in files if f[-18:-8] >= start]
    if end: files = [f for f in files if f[-18:-8] <= end]
    df = pl.concat([pl.read_parquet(f) for f in files]).sort("t").unique("t", keep="first").to_pandas()
    idx = pd.date_range(pd.to_datetime(df.t.min(), unit="ms"), pd.to_datetime(df.t.max(), unit="ms"),
                        freq="1min")
    df.index = pd.to_datetime(df.t, unit="ms")
    df = df.reindex(idx)
    df["close"] = df["close"].ffill()
    for k in ("open", "high", "low"):
        df[k] = df[k].fillna(df["close"])
    for k in ("volume", "quote_vol", "buy_vol", "trades"):
        df[k] = df[k].fillna(0)
    df["t"] = df.index.as_unit("ms").asi8
    return df


def resample(df1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    r = df1m.resample(tf, label="left", closed="left")
    out = pd.DataFrame({
        "open": r["open"].first(), "high": r["high"].max(), "low": r["low"].min(),
        "close": r["close"].last(), "volume": r["volume"].sum(), "buy_vol": r["buy_vol"].sum(),
        "quote_vol": r["quote_vol"].sum(), "trades": r["trades"].sum()}).dropna()
    return out


def map_signals(df1m, dft, tf, **arrays):
    """Map tf-bar arrays to the 1m index (last minute of each tf bar)."""
    pos = df1m.index.get_indexer(dft.index + pd.Timedelta(tf) - pd.Timedelta("1min"))
    ok = pos >= 0
    res = {}
    for k, a in arrays.items():
        o = np.zeros(len(df1m), dtype=a.dtype)
        o[pos[ok]] = a[ok]
        res[k] = o
    return res


def load_pair(a: str, b: str) -> pd.DataFrame:
    """Synthetic ratio instrument a/b (long a & short b with equal notional).
    1m bars: o = previous close, h/l = max/min(o, c) -- coarse but unbiased at 1m."""
    x, y = load_1m(a), load_1m(b)
    idx = x.index.intersection(y.index)
    c = (x.close.reindex(idx) / y.close.reindex(idx))
    df = pd.DataFrame({"close": c})
    df["open"] = c.shift().fillna(c)
    df["high"] = df[["open", "close"]].max(axis=1)
    df["low"] = df[["open", "close"]].min(axis=1)
    df["volume"] = x.quote_vol.reindex(idx); df["buy_vol"] = 0.0; df["quote_vol"] = df["volume"]
    df["trades"] = 0
    df["t"] = df.index.as_unit("ms").asi8
    return df
