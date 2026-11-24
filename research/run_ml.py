"""FreqAI-style walk-forward ML candidates. Predictions are always out-of-sample (sliding window
retrain every 7 days); 2025 is used only to choose the entry threshold, 2026 is reported.
Usage: python run_ml.py SYMBOL"""
import sys, os, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from bt import data, engine, ml, ind

IS_END = "2026-01-01"
CONFIGS = [
    ("FreqAI_LGBM_Clf", dict(mode="clf", tf="1h", sl=2.0, tp=3.0, hold=24), [0.45, 0.5, 0.55, 0.6]),
    ("FreqAI_LGBM_Clf", dict(mode="clf", tf="4h", sl=2.0, tp=4.0, hold=30), [0.45, 0.5, 0.55, 0.6]),
    ("FreqAI_LGBM_Reg", dict(mode="reg", tf="1h", sl=2.0, tp=3.0, hold=24), [0.5, 1.0, 1.5]),
    ("FreqAI_LGBM_Reg", dict(mode="reg", tf="4h", sl=2.0, tp=4.0, hold=12), [0.5, 1.0, 1.5]),
]

if __name__ == "__main__":
    sym = sys.argv[1]
    d = data.load_1m(sym)
    corr = data.load_1m("BTCUSDT") if sym != "BTCUSDT" else None
    if corr is not None:
        corr = corr.reindex(d.index).ffill()
    cut = d.index.searchsorted(pd.Timestamp(IS_END)); t = d.t.to_numpy()
    rows = []
    for name, c, thrs in CONFIGS:
        t0 = time.time()
        base, X = ml.features(d, c["tf"], corr)
        P = ml.walk_forward(base, X, c["mode"], label_period=c["hold"], sl_atr=c["sl"], tp_atr=c["tp"],
                            hold=c["hold"], start="2025-04-01")
        P.to_parquet(f"results/pred_{name}_{c['tf']}_{sym}.parquet")
        a = ind.atr(base, 14).to_numpy()
        tfm = 60 if c["tf"] == "1h" else 240
        for thr in thrs:
            dp = P.do_predict == 1
            if c["mode"] == "clf":
                L = (P.p_long > thr) & (P.p_long > P.p_short) & dp
                S = (P.p_short > thr) & (P.p_short > P.p_long) & dp
            else:
                L = (P.pred > thr) & dp; S = (P.pred < -thr) & dp
            sig = np.where(L, 1, np.where(S, -1, 0)).astype(np.int8)
            m = data.map_signals(d, base, c["tf"], sig=sig, sl_d=np.nan_to_num(c["sl"] * a),
                                 tp_d=np.nan_to_num(c["tp"] * a), hold=np.full(len(base), c["hold"] * tfm))
            row = dict(strategy=name, symbol=sym, params=repr({**c, "thr": thr}))
            for seg, sl in (("is", slice(0, cut)), ("oos", slice(cut, len(d)))):
                tr, eq = engine.run(d.iloc[sl], m["sig"][sl], m["sl_d"][sl], m["tp_d"][sl], m["hold"][sl],
                                    tick=data.TICK[sym])
                row.update({f"{seg}_{k}": v for k, v in engine.metrics(tr, eq, t[sl]).items()})
            rows.append(row)
        print(name, c["tf"], sym, round(time.time() - t0), "s", flush=True)
        pd.DataFrame(rows).to_csv(f"results/ml_{sym}.csv", index=False)
