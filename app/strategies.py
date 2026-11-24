"""Live adapters around the exact research functions (research/bt) so live = backtested logic.
Each adapter returns, for the LAST CLOSED bar: sig (+1/-1/0), sl_d, tp_d, hold_min, exit_long, exit_short."""
import time
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.neighbors import NearestNeighbors
from sklearn.metrics import pairwise_distances
from research.bt import strategies as RS, ml as RML, ind
from research.bt.data import resample

# Benchmarks from Stage 1 (2026 out-of-sample, average per coin) -> used by the Meta-AI
BOTS = [
    dict(id="bot1", name="בוט 1 · פריצת מגמה", strategy="Donchian_TSMOM", tf="4h",
         params=dict(n=55, sl_atr=2.0, tp_atr=10.0, hold=300, trend=200),
         bench=dict(pf=1.41, win=28.6, net=9.2, sharpe=0.86, mdd=-13.9, trades_year=34, verdict="עברה"),
         desc="פריצת ערוץ דונצ'יאן 55 נרות 4h בכיוון EMA200. סטופ 2×ATR, יעד 10×ATR."),
    dict(id="bot2", name="בוט 2 · חציית ממוצעים", strategy="EMA_Cross_TSMOM", tf="4h",
         params=dict(fast=10, slow=100, sl_atr=2.0, tp_atr=6.0, hold=300),
         bench=dict(pf=1.31, win=30.1, net=3.0, sharpe=0.42, mdd=-13.1, trades_year=26, verdict="גבולית"),
         desc="חציית EMA10/EMA100 על 4h, יציאה בחצייה נגדית. סטופ 2×ATR, יעד 6×ATR."),
    dict(id="bot3", name="בוט 3 · זרימת פקודות", strategy="OrderFlow_Mom", tf="1h",
         params=dict(n=12, z=2.0, trend=100, sl_atr=2.0, tp_atr=3.0, hold=48),
         bench=dict(pf=1.12, win=46.7, net=6.3, sharpe=0.62, mdd=-14.3, trades_year=113, verdict="נכשלה ב-2025"),
         desc="חוסר איזון בין קונים למוכרים אגרסיביים (טייקר) ב-Bybit, בכיוון EMA100. סטופ 2×ATR, יעד 3×ATR."),
    dict(id="bot4", name="בוט 4 · FreqAI למידת מכונה", strategy="FreqAI_LGBM_Clf", tf="4h",
         params=dict(thr=0.5, sl_atr=2.0, tp_atr=4.0, hold=30, train_days=90, retrain_days=7),
         bench=dict(pf=0.81, win=35.5, net=-6.4, sharpe=-0.72, mdd=-14.5, trades_year=61, verdict="נכשלה (ניסיוני)"),
         desc="LightGBM מסווג הסתברותי (האם היעד יושג לפני הסטופ), אימון מחדש שבועי, סינון חריגות DI."),
]
TF_MIN = {"1h": 60, "4h": 240}


def _last(a, k):
    v = a[k]
    return v[-1] if len(v) else 0


class RuleAdapter:
    FUN = {"Donchian_TSMOM": RS.donchian, "EMA_Cross_TSMOM": RS.ema_cross, "OrderFlow_Mom": RS.flow_momentum}

    def __init__(self, strategy, tf, params):
        self.f = self.FUN[strategy]; self.tf = tf; self.p = params

    def evaluate(self, df1m, corr1m=None):
        dft = resample(df1m, self.tf)
        if len(dft) < 250:
            return None
        a = self.f(dft, self.tf, **self.p)
        return dict(sig=int(_last(a, "sig")), sl_d=float(_last(a, "sl_d")), tp_d=float(_last(a, "tp_d")),
                    hold_min=int(_last(a, "hold")), exit_l=bool(_last(a, "exit_l")), exit_s=bool(_last(a, "exit_s")),
                    bar_time=str(dft.index[-1]), price=float(dft.close.iloc[-1]))


class FreqAIAdapter:
    """Same features / labels / DI filter as research/bt/ml.py, trained on a sliding window."""

    def __init__(self, tf, params):
        self.tf = tf; self.p = params; self.models = {}   # symbol -> (trained_ts, mL, mS, mu, sd, Ztr, avg_dist)

    def _train(self, sym, base, X):
        p = self.p
        yl, ys, _ = RML.tb_labels(base, p["sl_atr"], p["tp_atr"], p["hold"])
        bars_day = 1440 // TF_MIN[self.tf]
        Xv = X.to_numpy(np.float32); good = np.isfinite(Xv).all(1)
        n = len(base); end = n - p["hold"] - 1           # purge labels that are not yet known
        tr = np.arange(max(0, end - p["train_days"] * bars_day), end); tr = tr[good[tr]]
        if len(tr) < 300:
            return None
        mu, sd = Xv[tr].mean(0), Xv[tr].std(0) + 1e-9
        Ztr = (Xv[tr] - mu) / sd
        sub = Ztr[np.random.default_rng(0).choice(len(Ztr), min(1500, len(Ztr)), replace=False)]
        avg = pairwise_distances(sub).mean()
        w = np.exp(-np.arange(len(tr))[::-1] / (len(tr) * 2.0))
        mL = lgb.LGBMClassifier(**RML.LGB).fit(Xv[tr], yl[tr].astype(int), sample_weight=w)
        mS = lgb.LGBMClassifier(**RML.LGB).fit(Xv[tr], ys[tr].astype(int), sample_weight=w)
        nn = NearestNeighbors(n_neighbors=1).fit(Ztr)
        self.models[sym] = (time.time(), mL, mS, mu, sd, nn, avg)
        return self.models[sym]

    def evaluate(self, df1m, corr1m=None, sym=""):
        base, X = RML.features(df1m, self.tf, corr1m, flow=False)
        if len(base) < 400:
            return None
        m = self.models.get(sym)
        if m is None or time.time() - m[0] > self.p["retrain_days"] * 86400:
            m = self._train(sym, base, X)
            if m is None:
                return None
        _, mL, mS, mu, sd, nn, avg = m
        x = X.to_numpy(np.float32)[-1:]
        if not np.isfinite(x).all():
            return None
        di = nn.kneighbors((x - mu) / sd)[0][0, 0] / avg
        pl_, ps_ = mL.predict_proba(x)[0, 1], mS.predict_proba(x)[0, 1]
        thr = self.p["thr"]; ok = di < 1.0
        sig = 1 if (ok and pl_ > thr and pl_ > ps_) else -1 if (ok and ps_ > thr and ps_ > pl_) else 0
        a = float(ind.atr(base, 14).iloc[-1])
        return dict(sig=sig, sl_d=self.p["sl_atr"] * a, tp_d=self.p["tp_atr"] * a,
                    hold_min=self.p["hold"] * TF_MIN[self.tf], exit_l=False, exit_s=False,
                    bar_time=str(base.index[-1]), price=float(base.close.iloc[-1]),
                    info=dict(p_long=round(float(pl_), 3), p_short=round(float(ps_), 3), di=round(float(di), 3)))


def make_adapter(b):
    if b["strategy"] == "FreqAI_LGBM_Clf":
        return FreqAIAdapter(b["tf"], b["params"])
    return RuleAdapter(b["strategy"], b["tf"], b["params"])
