"""Pick one parameter set per strategy using IN-SAMPLE data only, report its OUT-OF-SAMPLE result.
Score = median IS Sharpe across symbols (same params on every coin; >= 20 IS trades per coin)."""
import glob, os, sys
import numpy as np, pandas as pd

R = os.path.join(os.path.dirname(__file__), "results")
df = pd.concat([pd.read_csv(f) for f in glob.glob(f"{R}/rules_*.csv")] +
               [pd.read_csv(f) for f in glob.glob(f"{R}/ml_*.csv")], ignore_index=True)

rows = []
for strat, g in df.groupby("strategy"):
    nsym = g.symbol.nunique()
    piv = g.groupby("params")
    ok = piv.filter(lambda x: (len(x) == nsym) and (x.is_trades >= 20).all())
    if ok.empty:
        continue
    sc = ok.groupby("params").is_sharpe.median().sort_values(ascending=False)
    best = sc.index[0]
    b = ok[ok.params == best]
    share_pos = (g.groupby("params").oos_net_pct.median() > 0).mean()
    rows.append(dict(strategy=strat, params=best, combos=g.params.nunique(),
                     IS_sharpe=b.is_sharpe.median(), IS_pf=b.is_pf.median(), IS_net=b.is_net_pct.mean(),
                     OOS_net_pct=b.oos_net_pct.mean(), OOS_sharpe=b.oos_sharpe.median(),
                     OOS_pf=b.oos_pf.median(), OOS_mdd=b.oos_mdd_pct.min(),
                     OOS_trades=b.oos_trades.sum(), OOS_win=b.oos_win_pct.mean(),
                     per_symbol=" | ".join(f"{s[:3]} {v:+.1f}%" for s, v in zip(b.symbol, b.oos_net_pct)),
                     pct_combos_oos_positive=100 * share_pos))
out = pd.DataFrame(rows).sort_values("OOS_sharpe", ascending=False)
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 120)
print(out.round(2).to_string(index=False))
out.to_csv(f"{R}/selection.csv", index=False)
