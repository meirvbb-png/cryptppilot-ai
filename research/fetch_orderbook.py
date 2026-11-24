"""Rebuild Bybit L2 order book (quote-saver.bycsi.com ob200 archives, 100ms updates)
and keep per-update top-of-book + depth features. Also stores raw tick trades for the
same day (needed for realistic maker-fill simulation). Usage: SYMBOL DAY [DAY...]"""
import sys, os, subprocess, zipfile, orjson, polars as pl
from sortedcontainers import SortedDict

D = os.path.join(os.path.dirname(__file__), "data")

def process(sym, day):
    out = f"{D}/ob/{sym}_{day}.parquet"
    if not os.path.exists(out):
        z = f"{D}/ob/{sym}_{day}.zip"
        subprocess.run(["curl", "-sS", "-f", "--retry", "4", "-o", z,
            f"https://quote-saver.bycsi.com/orderbook/linear/{sym}/{day}_{sym}_ob200.data.zip"], check=True)
        bids, asks = SortedDict(), SortedDict()
        cols = {k: [] for k in ["ts", "bid", "ask", "bq1", "aq1", "bq5", "aq5", "bq20", "aq20"]}
        with zipfile.ZipFile(z) as zf, zf.open(zf.namelist()[0]) as fh:
            for line in fh:
                m = orjson.loads(line); d = m["data"]
                if m["type"] == "snapshot":
                    bids.clear(); asks.clear()
                for p, q in d["b"]:
                    p = float(p); q = float(q)
                    if q == 0: bids.pop(p, None)
                    else: bids[p] = q
                for p, q in d["a"]:
                    p = float(p); q = float(q)
                    if q == 0: asks.pop(p, None)
                    else: asks[p] = q
                if not bids or not asks: continue
                bk = bids.keys(); ak = asks.keys(); bv = bids.values(); av = asks.values()
                nb, na = len(bids), len(asks)
                cols["ts"].append(m["ts"]); cols["bid"].append(bk[-1]); cols["ask"].append(ak[0])
                cols["bq1"].append(bv[-1]); cols["aq1"].append(av[0])
                cols["bq5"].append(sum(bv[max(0, nb-5):])); cols["aq5"].append(sum(av[:5]))
                cols["bq20"].append(sum(bv[max(0, nb-20):])); cols["aq20"].append(sum(av[:20]))
        os.remove(z)
        pl.DataFrame(cols).write_parquet(out)
    tk = f"{D}/ticks/{sym}_{day}.parquet"
    if not os.path.exists(tk):
        g = f"{D}/ticks/{sym}_{day}.csv.gz"
        subprocess.run(["curl", "-sS", "-f", "--retry", "4", "-o", g,
            f"https://public.bybit.com/trading/{sym}/{sym}{day}.csv.gz"], check=True)
        t = pl.read_csv(g, columns=["timestamp", "side", "size", "price"])
        os.remove(g)
        t.with_columns((pl.col("timestamp") * 1000).cast(pl.Int64).alias("ts")).select(
            "ts", (pl.col("side") == "Buy").alias("buy"), "size", "price").sort("ts").write_parquet(tk)
    print("ok", sym, day, flush=True)

if __name__ == "__main__":
    for day in sys.argv[2:]:
        process(sys.argv[1], day)
