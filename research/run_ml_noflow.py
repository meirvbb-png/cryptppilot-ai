"""Validate the deployable FreqAI config (4h classifier, no order-flow features)."""
import sys, os
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from bt import data, engine, ml, ind
rows = []
btc = data.load_1m("BTCUSDT")
for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
    d = btc if sym == "BTCUSDT" else data.load_1m(sym)
    corr = None if sym == "BTCUSDT" else btc.reindex(d.index).ffill()
    base, X = ml.features(d, "4h", corr, flow=False)
    P = ml.walk_forward(base, X, "clf", sl_atr=2.0, tp_atr=4.0, hold=30, start="2025-04-01")
    a = ind.atr(base, 14).to_numpy(); cut = d.index.searchsorted(pd.Timestamp("2026-01-01")); t = d.t.to_numpy()
    for thr in [0.45, 0.5, 0.55, 0.6]:
        dp = P.do_predict == 1
        L = (P.p_long > thr) & (P.p_long > P.p_short) & dp; S = (P.p_short > thr) & (P.p_short > P.p_long) & dp
        sig = np.where(L, 1, np.where(S, -1, 0)).astype(np.int8)
        m = data.map_signals(d, base, "4h", sig=sig, sl_d=np.nan_to_num(2 * a), tp_d=np.nan_to_num(4 * a), hold=np.full(len(base), 30 * 240))
        row = dict(strategy="FreqAI_Clf_4h_noflow", symbol=sym, params=repr({"thr": thr}))
        for seg, sl in (("is", slice(0, cut)), ("oos", slice(cut, len(d)))):
            tr, eq = engine.run(d.iloc[sl], m["sig"][sl], m["sl_d"][sl], m["tp_d"][sl], m["hold"][sl], tick=data.TICK[sym])
            row.update({f"{seg}_{k}": v for k, v in engine.metrics(tr, eq, t[sl]).items()})
        rows.append(row)
    print(sym, flush=True)
df = pd.DataFrame(rows); df.to_csv("results/ml_noflow.csv", index=False)
print(df.groupby("params")[["is_net_pct", "is_pf", "oos_net_pct", "oos_pf", "oos_mdd_pct", "oos_trades"]].mean().round(2))
