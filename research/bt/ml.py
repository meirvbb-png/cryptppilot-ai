"""FreqAI-style walk-forward ML (re-implemented from freqtrade/freqai docs & FreqaiExampleStrategy).

* Features: FreqaiExampleStrategy feature set (RSI, MFI, ADX, SMA/EMA ratios, BB width,
  close/BB-lower, ROC, relative volume) for indicator_periods_candles x include_timeframes,
  include_shifted_candles, correlated pair (BTC for alts), %-pct-change, hour/day-of-week.
  Optional Bybit order-flow features (taker buy imbalance), not in FreqAI by default.
* Sliding window: train_period_days, retrain every backtest_period_days. Training labels
  that would peek into the prediction window are purged.
* Outliers: Dissimilarity Index (DI_threshold) -> do_predict = 0 for unfamiliar points.
* Targets:
    reg -> FreqAI default "&-s_close" (mean future close over label_period / close - 1)
    clf -> probabilistic triple-barrier label: TP (tp_atr*ATR) reached before SL (sl_atr*ATR)
           within `hold` bars, separately for long and short (LightGBM classifier).
"""
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.neighbors import NearestNeighbors
from sklearn.metrics import pairwise_distances
from . import ind
from .data import resample

LGB = dict(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=100,
           subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=1.0, verbose=-1, n_jobs=2)


def _feat_block(df, periods, prefix):
    f = {}
    for p in periods:
        f[f"{prefix}rsi{p}"] = ind.rsi(df.close, p)
        f[f"{prefix}mfi{p}"] = ind.mfi(df, p)
        f[f"{prefix}adx{p}"] = ind.adx(df, p)
        f[f"{prefix}sma{p}"] = df.close / ind.sma(df.close, p) - 1
        f[f"{prefix}ema{p}"] = df.close / ind.ema(df.close, p) - 1
        lo, mid, up, _ = ind.bbands((df.high + df.low + df.close) / 3, p, 2.2)
        f[f"{prefix}bbw{p}"] = (up - lo) / mid
        f[f"{prefix}cbl{p}"] = df.close / lo
        f[f"{prefix}roc{p}"] = df.close.pct_change(p)
        f[f"{prefix}rvol{p}"] = df.volume / df.volume.rolling(p).mean()
    f[f"{prefix}pct"] = df.close.pct_change()
    f[f"{prefix}natr"] = ind.atr(df, 14) / df.close
    return pd.DataFrame(f, index=df.index)


def features(df1m, tf, corr1m=None, periods=(10, 20, 50), htf="4h", shifts=2, flow=True):
    base = resample(df1m, tf)
    X = _feat_block(base, periods, "")
    if flow:
        imb = (2 * base.buy_vol - base.volume) / base.volume.replace(0, np.nan)
        for p in (1, 6, 24):
            X[f"flow{p}"] = imb.rolling(p).mean()
        X["tradesz"] = base.quote_vol / base.trades.replace(0, np.nan)
        X["tradesz"] = X["tradesz"] / X["tradesz"].rolling(100).mean()
    hi = resample(df1m, htf)
    H = _feat_block(hi, periods[:2], "h_")
    # a higher-timeframe bar is only known after it closes -> shift by one htf bar
    H.index = H.index + pd.Timedelta(htf) - pd.Timedelta(tf)
    X = X.join(H.reindex(X.index, method="ffill"))
    if corr1m is not None:
        cb = resample(corr1m, tf).reindex(base.index)
        C = _feat_block(cb, periods[:2], "c_")
        X = X.join(C)
    cols = list(X.columns)
    for s in range(1, shifts + 1):
        for c in [c for c in cols if not c.startswith("h_")][:12]:
            X[f"{c}_sh{s}"] = X[c].shift(s)
    X["hour"] = X.index.hour
    X["dow"] = X.index.dayofweek
    return base, X.replace([np.inf, -np.inf], np.nan)


def tb_labels(base, sl_atr, tp_atr, hold):
    """Triple-barrier outcome on tf bars (ties -> stop first, i.e. pessimistic)."""
    a = ind.atr(base, 14).to_numpy()
    c, h, l = base.close.to_numpy(), base.high.to_numpy(), base.low.to_numpy()
    n = len(c); yl = np.zeros(n); ys = np.zeros(n)
    for i in range(n - 1):
        if not np.isfinite(a[i]): continue
        e = c[i]; up_tp, up_sl = e + tp_atr * a[i], e - sl_atr * a[i]
        dn_tp, dn_sl = e - tp_atr * a[i], e + sl_atr * a[i]
        rl = rs = 0
        for j in range(i + 1, min(n, i + 1 + hold)):
            if rl == 0:
                if l[j] <= up_sl: rl = -1
                elif h[j] >= up_tp: rl = 1
            if rs == 0:
                if h[j] >= dn_sl: rs = -1
                elif l[j] <= dn_tp: rs = 1
            if rl and rs: break
        yl[i] = rl == 1; ys[i] = rs == 1
    return yl, ys, a


def walk_forward(base, X, mode="clf", train_days=90, step_days=7, label_period=24,
                 sl_atr=2.0, tp_atr=3.0, hold=24, di_threshold=1.0, start=None):
    """Returns DataFrame of out-of-sample predictions on tf bars."""
    tf_min = int((base.index[1] - base.index[0]).total_seconds() // 60)
    bars_day = 1440 // tf_min
    if mode == "reg":
        y = base.close[::-1].rolling(label_period).mean()[::-1].shift(-1) / base.close - 1
        ys = None
        purge = label_period + 1
    else:
        yl, ys_, _ = tb_labels(base, sl_atr, tp_atr, hold)
        y = pd.Series(yl, base.index); ys = pd.Series(ys_, base.index)
        purge = hold + 1
    Xv = X.to_numpy(np.float32)
    good = np.isfinite(Xv).all(1)
    n = len(base)
    first = train_days * bars_day + 300 if start is None else base.index.get_indexer([pd.Timestamp(start)], method="bfill")[0]
    out = pd.DataFrame(index=base.index, data={"p_long": np.nan, "p_short": np.nan, "pred": np.nan, "do_predict": 0})
    step = step_days * bars_day
    for t0 in range(first, n, step):
        tr = np.arange(max(0, t0 - train_days * bars_day), t0 - purge)
        tr = tr[good[tr]]
        te = np.arange(t0, min(n, t0 + step)); te_ok = te[good[te]]
        if len(tr) < 500 or len(te_ok) == 0: continue
        mu, sd = Xv[tr].mean(0), Xv[tr].std(0) + 1e-9
        Ztr = (Xv[tr] - mu) / sd; Zte = (Xv[te_ok] - mu) / sd
        # Dissimilarity Index (FreqAI): distance to nearest train point / mean pairwise train distance
        sub = Ztr[np.random.default_rng(0).choice(len(Ztr), min(1500, len(Ztr)), replace=False)]
        avg_mean_dist = pairwise_distances(sub).mean()
        di = NearestNeighbors(n_neighbors=1).fit(Ztr).kneighbors(Zte)[0][:, 0] / avg_mean_dist
        dp = (di < di_threshold).astype(int)
        w = np.exp(-np.arange(len(tr))[::-1] / (len(tr) * 2.0))  # weight_factor-like recency weight
        if mode == "reg":
            m = lgb.LGBMRegressor(**LGB).fit(Xv[tr], y.to_numpy()[tr], sample_weight=w)
            p = m.predict(Xv[te_ok])
            pr_tr = y.to_numpy()[tr]
            out.iloc[te_ok, out.columns.get_loc("pred")] = (p - pr_tr.mean()) / pr_tr.std()
        else:
            for col, yy in (("p_long", y), ("p_short", ys)):
                m = lgb.LGBMClassifier(**LGB).fit(Xv[tr], yy.to_numpy()[tr].astype(int), sample_weight=w)
                out.iloc[te_ok, out.columns.get_loc(col)] = m.predict_proba(Xv[te_ok])[:, 1]
        out.iloc[te_ok, out.columns.get_loc("do_predict")] = dp
    return out
