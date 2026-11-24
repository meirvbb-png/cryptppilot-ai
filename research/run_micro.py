"""Grid sweep of order-book strategies on Bybit L2 + tick data.
First 4 days = selection (IS), remaining days = out-of-sample. Notional 2500 USDT (5x of 500).
Usage: python run_micro.py SYMBOL"""
import sys, os, itertools
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from bt import micro

GRID = dict(mode=[0, 1, 2], thr=[0.3, 0.5, 0.7], tp_k=[3, 8, 20], sl_k=[3, 8], hold_s=[300, 1800],
            strict=[True, False])
NAMES = {0: "OBI_Maker", 1: "OBI_Taker", 2: "PMM_Dynamic"}

if __name__ == "__main__":
    sym = sys.argv[1]
    days = micro.days_available(sym)
    rows = []
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(GRID, combo))
        thr = p["thr"] * 3 if p["mode"] == 2 else p["thr"]   # PMM: thr = spread in vol units (0.9..2.1)
        per_day = []
        for d in days:
            r = micro.run_day(sym, d, p["mode"], thr, p["tp_k"], p["sl_k"], hold_s=p["hold_s"],
                              strict=p["strict"])
            per_day.append((len(r), r[:, 4].sum(), (r[:, 1] * (r[:, 3] - r[:, 2]) * 2500 / np.maximum(r[:, 2], 1e-9)).sum()))
        a = np.array(per_day)
        rows.append(dict(strategy=NAMES[p["mode"]], symbol=sym, params=repr(p),
                         is_trades=a[:4, 0].sum(), is_net=a[:4, 1].sum(), is_gross=a[:4, 2].sum(),
                         oos_trades=a[4:, 0].sum(), oos_net=a[4:, 1].sum(), oos_gross=a[4:, 2].sum()))
    os.makedirs("results", exist_ok=True)
    pd.DataFrame(rows).to_csv(f"results/micro_{sym}.csv", index=False)
    print(sym, len(rows), "combos; days", days)
