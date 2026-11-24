"""Walk-forward grid evaluation of rule-based candidates.
IS (in-sample, parameter selection): 2025-01-01 .. 2025-12-31
OOS (out-of-sample, reporting):      2026-01-01 .. end of data
Usage: python run_rules.py SYMBOL [STRATEGY ...]"""
import sys, itertools, os, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from bt import data, engine, strategies as S

IS_END = "2026-01-01"
OUT = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(OUT, exist_ok=True)


def evaluate(d, f, params, tick, costs=engine.Costs()):
    params = dict(params); limit = params.pop("limit", False)
    tf = params["tf"]
    dft = data.resample(d, tf)
    a = f(dft, **params)
    m = data.map_signals(d, dft, tf, **a)
    cut = d.index.searchsorted(pd.Timestamp(IS_END))
    t = d.t.to_numpy()
    rows = {}
    for seg, sl in (("is", slice(0, cut)), ("oos", slice(cut, len(d)))):
        tr, eq = engine.run(d.iloc[sl], m["sig"][sl], m["sl_d"][sl], m["tp_d"][sl], m["hold"][sl],
                            m["exit_l"][sl], m["exit_s"][sl], tick=tick, costs=costs,
                            limit_entry=limit, limit_wait=int(pd.Timedelta(tf).total_seconds() // 60))
        rows[seg] = engine.metrics(tr, eq, t[sl])
    return rows


if __name__ == "__main__":
    sym = sys.argv[1]
    names = sys.argv[2:] or list(S.REGISTRY)
    pair = "/" in sym
    d = data.load_pair(*sym.split("/")) if pair else data.load_1m(sym)
    tick = 1e-7 if pair else data.TICK[sym]
    costs = engine.Costs(taker=2 * engine.TAKER, maker=2 * engine.MAKER, slip=2 * engine.SLIP,
                         funding_8h=0.0) if pair else engine.Costs()
    print(sym, d.index[0], d.index[-1], flush=True)
    for name in names:
        f, grid = S.REGISTRY[name]
        keys = list(grid)
        res = []
        t0 = time.time()
        for combo in itertools.product(*grid.values()):
            p = dict(zip(keys, combo))
            r = evaluate(d, f, p, tick, costs)
            row = {"strategy": name, "symbol": sym, "params": repr(p)}
            for seg in r:
                row.update({f"{seg}_{k}": v for k, v in r[seg].items()})
            res.append(row)
        pd.DataFrame(res).to_csv(f"{OUT}/rules_{name}_{sym.replace('/', '-')}.csv", index=False)
        print(name, len(res), "combos", round(time.time() - t0), "s", flush=True)
