"""Download Bybit public trade archives (public.bybit.com/trading) and aggregate
each day into 1-minute bars with taker buy/sell volume split.
Raw files are deleted after aggregation. Usage: fetch_trades.py SYMBOL START END"""
import sys, os, datetime as dt, subprocess, polars as pl
from concurrent.futures import ThreadPoolExecutor

BASE = "https://public.bybit.com/trading"
OUT = os.path.join(os.path.dirname(__file__), "data", "raw1m")

def one(sym, day):
    out = f"{OUT}/{sym}_{day}.parquet"
    if os.path.exists(out):
        return
    tmp = f"{OUT}/{sym}_{day}.csv.gz"
    r = subprocess.run(["curl", "-sS", "-f", "-m", "600", "--retry", "4", "-o", tmp,
                        f"{BASE}/{sym}/{sym}{day}.csv.gz"])
    if r.returncode != 0:
        print("FAIL", sym, day, flush=True); return
    df = pl.read_csv(tmp, columns=["timestamp", "side", "size", "price"])
    os.remove(tmp)
    df = df.with_columns(
        (pl.col("timestamp") * 1000).cast(pl.Int64).alias("ms"),
        (pl.col("size") * pl.col("price")).alias("notional"),
    ).with_columns((pl.col("ms") // 60000 * 60000).alias("t"))
    bars = (df.sort("ms").group_by("t", maintain_order=True).agg(
        pl.col("price").first().alias("open"), pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"), pl.col("price").last().alias("close"),
        pl.col("size").sum().alias("volume"), pl.col("notional").sum().alias("quote_vol"),
        pl.col("size").filter(pl.col("side") == "Buy").sum().alias("buy_vol"),
        pl.len().alias("trades")))
    bars.write_parquet(out)
    print("ok", sym, day, len(bars), flush=True)

if __name__ == "__main__":
    sym, start, end = sys.argv[1], dt.date.fromisoformat(sys.argv[2]), dt.date.fromisoformat(sys.argv[3])
    days = [(end - dt.timedelta(d)).isoformat() for d in range((end - start).days + 1)]
    with ThreadPoolExecutor(int(os.environ.get("JOBS", "3"))) as ex:
        list(ex.map(lambda d: one(sym, d), days))
