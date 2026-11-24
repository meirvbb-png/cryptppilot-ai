"""Rule-based candidates. Each returns arrays on the strategy-timeframe index:
sig (+1/-1/0), sl_d / tp_d (absolute price distance fixed at signal time),
hold (max minutes in trade), exit_l / exit_s (signal exits)."""
import numpy as np
from . import ind

MIN = {"5min": 5, "15min": 15, "1h": 60, "4h": 240}


def _pack(df, long, short, sl_d, tp_d, hold_bars, tf, exit_l=None, exit_s=None):
    sig = np.where(long, 1, np.where(short, -1, 0)).astype(np.int8)
    ok = np.isfinite(sl_d) & np.isfinite(tp_d) & (sl_d > 0)
    sig[~ok] = 0
    n = len(df)
    return dict(sig=sig, sl_d=np.nan_to_num(sl_d), tp_d=np.nan_to_num(tp_d),
                hold=np.full(n, hold_bars * MIN[tf], np.int64),
                exit_l=np.zeros(n, bool) if exit_l is None else np.asarray(exit_l, bool),
                exit_s=np.zeros(n, bool) if exit_s is None else np.asarray(exit_s, bool))


def hb_bollinger(df, tf, bb_len=100, bb_std=2.0, sl_atr=2.0, tp_atr=2.0, hold=48):
    """Hummingbot controllers/directional_trading/bollinger_v1.py (BBP<0 long, BBP>1 short)
    with triple-barrier exits (SL / TP / time limit) as in its PositionExecutor."""
    _, mid, _, bbp = ind.bbands(df.close, bb_len, bb_std)
    a = ind.atr(df, 14).to_numpy()
    return _pack(df, bbp < 0, bbp > 1, sl_atr * a, tp_atr * a, hold, tf,
                 exit_l=df.close > mid, exit_s=df.close < mid)


def hb_macd_bb(df, tf, bb_len=100, bb_std=2.0, sl_atr=2.0, tp_atr=2.0, hold=48):
    """Hummingbot macd_bb_v1.py: BBP<0 & MACDh>0 & MACD<0 -> long (mirror for short)."""
    _, _, _, bbp = ind.bbands(df.close, bb_len, bb_std)
    m, _, hst = ind.macd(df.close, 21, 42, 9)
    a = ind.atr(df, 14).to_numpy()
    return _pack(df, (bbp < 0) & (hst > 0) & (m < 0), (bbp > 1) & (hst < 0) & (m > 0),
                 sl_atr * a, tp_atr * a, hold, tf)


def hb_supertrend(df, tf, length=20, mult=4.0, dist=0.01, sl_atr=2.5, tp_atr=6.0, hold=200):
    """Hummingbot supertrend_v1.py: trend direction + entry only near the SuperTrend line.
    Exit on direction flip."""
    st, d = ind.supertrend(df, length, mult)
    pdist = (df.close - st).abs() / df.close
    a = ind.atr(df, 14).to_numpy()
    flip_dn = (d == -1) & (d.shift() == 1); flip_up = (d == 1) & (d.shift() == -1)
    return _pack(df, (d == 1) & (pdist < dist), (d == -1) & (pdist < dist),
                 sl_atr * a, tp_atr * a, hold, tf, exit_l=flip_dn, exit_s=flip_up)


def donchian(df, tf, n=55, sl_atr=2.5, tp_atr=8.0, hold=300, trend=200):
    """Classic time-series momentum / channel breakout with EMA trend filter."""
    hi = df.high.rolling(n).max().shift(); lo = df.low.rolling(n).min().shift()
    e = ind.ema(df.close, trend)
    a = ind.atr(df, 20).to_numpy()
    xl = df.close < df.low.rolling(n // 2).min().shift()
    xs = df.close > df.high.rolling(n // 2).max().shift()
    return _pack(df, (df.close > hi) & (df.close > e), (df.close < lo) & (df.close < e),
                 sl_atr * a, tp_atr * a, hold, tf, exit_l=xl, exit_s=xs)


def rsi2_meanrev(df, tf, lo=10, hi=90, trend=200, sl_atr=2.0, tp_atr=1.5, hold=24):
    """Connors RSI(2) pullback inside the long-term trend."""
    r = ind.rsi(df.close, 2); e = ind.ema(df.close, trend)
    a = ind.atr(df, 14).to_numpy(); m5 = ind.sma(df.close, 5)
    return _pack(df, (r < lo) & (df.close > e), (r > hi) & (df.close < e),
                 sl_atr * a, tp_atr * a, hold, tf, exit_l=df.close > m5, exit_s=df.close < m5)


def flow_momentum(df, tf, n=24, z=2.0, trend=100, sl_atr=2.0, tp_atr=4.0, hold=48):
    """Order-flow (taker buy/sell imbalance from Bybit ticks) breakout in trend direction.
    Candle-level proxy of Hummingbot's microstructure ideas."""
    imb = (2 * df.buy_vol - df.volume) / df.volume.replace(0, np.nan)
    cvd = imb.rolling(n).mean()
    zz = (cvd - cvd.rolling(n * 10).mean()) / cvd.rolling(n * 10).std()
    e = ind.ema(df.close, trend)
    a = ind.atr(df, 14).to_numpy()
    return _pack(df, (zz > z) & (df.close > e), (zz < -z) & (df.close < e),
                 sl_atr * a, tp_atr * a, hold, tf)


def ema_cross(df, tf, fast=20, slow=100, sl_atr=3.0, tp_atr=9.0, hold=300):
    """Time-series momentum: fast/slow EMA cross, exit on the opposite cross."""
    f, s = ind.ema(df.close, fast), ind.ema(df.close, slow)
    up = (f > s) & (f.shift() <= s.shift()); dn = (f < s) & (f.shift() >= s.shift())
    a = ind.atr(df, 20).to_numpy()
    return _pack(df, up, dn, sl_atr * a, tp_atr * a, hold, tf, exit_l=dn, exit_s=up)


def trend_pullback(df, tf, htf_fast=50, htf_slow=200, rsi_n=14, lo=35, hi=65, sl_atr=2.0, tp_atr=4.0, hold=48):
    """Higher-timeframe trend (EMA50 vs EMA200 on 4x the bar) + RSI pullback entry."""
    k = 4
    f, s = ind.ema(df.close, htf_fast * k), ind.ema(df.close, htf_slow * k)
    r = ind.rsi(df.close, rsi_n)
    a = ind.atr(df, 14).to_numpy()
    long = (f > s) & (r < lo) & (r.shift() >= lo)
    short = (f < s) & (r > hi) & (r.shift() <= hi)
    return _pack(df, long, short, sl_atr * a, tp_atr * a, hold, tf)


def hb_stat_arb(df, tf, lookback=100, z_in=2.0, sl_z=1.5, tp_z=2.0, hold=96):
    """Hummingbot controllers/generic/stat_arb.py idea on the ETH/BTC ratio: z-score of the
    spread vs its rolling mean; fade extremes. SL/TP are fixed ratio prices at entry."""
    lr = np.log(df.close)
    mu = lr.rolling(lookback).mean(); sd = lr.rolling(lookback).std()
    z = (lr - mu) / sd
    px_sd = (sd * df.close).to_numpy()          # 1 sd in ratio-price units
    back = (z.abs() < 0.2)
    return _pack(df, z < -z_in, z > z_in, sl_z * px_sd, tp_z * px_sd, hold, tf,
                 exit_l=back, exit_s=back)


REGISTRY = {
    "HB_Bollinger": (hb_bollinger, dict(tf=["15min", "1h"], bb_len=[50, 100], bb_std=[2.0, 2.5],
                                        sl_atr=[2.0, 3.0], tp_atr=[1.5, 3.0], hold=[24, 72])),
    "HB_MACD_BB": (hb_macd_bb, dict(tf=["15min", "1h"], bb_len=[50, 100], bb_std=[2.0],
                                    sl_atr=[2.0, 3.0], tp_atr=[1.5, 3.0], hold=[24, 72])),
    "HB_SuperTrend": (hb_supertrend, dict(tf=["15min", "1h", "4h"], length=[20], mult=[3.0, 4.0],
                                          dist=[0.005, 0.01, 0.02], sl_atr=[2.0, 3.0], tp_atr=[6.0, 10.0],
                                          hold=[200])),
    "Donchian_TSMOM": (donchian, dict(tf=["1h", "4h"], n=[20, 55], sl_atr=[2.0, 3.0], tp_atr=[6.0, 10.0],
                                      hold=[300], trend=[100, 200])),
    "RSI2_MeanRev": (rsi2_meanrev, dict(tf=["15min", "1h"], lo=[5, 10], hi=[90, 95], trend=[200],
                                        sl_atr=[2.0, 3.0], tp_atr=[1.0, 2.0], hold=[24])),
    "OrderFlow_Mom": (flow_momentum, dict(tf=["15min", "1h"], n=[12, 24], z=[1.5, 2.0], trend=[100],
                                          sl_atr=[2.0], tp_atr=[3.0, 5.0], hold=[48])),
    "EMA_Cross_TSMOM": (ema_cross, dict(tf=["1h", "4h"], fast=[10, 20, 50], slow=[100, 200], sl_atr=[2.0, 3.0],
                                        tp_atr=[6.0, 10.0], hold=[300])),
    "Trend_Pullback": (trend_pullback, dict(tf=["15min", "1h"], lo=[30, 40], hi=[60, 70], sl_atr=[1.5, 2.5],
                                            tp_atr=[3.0, 5.0], hold=[48])),
    "HB_Bollinger_Maker": (hb_bollinger, dict(tf=["15min", "1h"], bb_len=[50, 100], bb_std=[2.0, 2.5],
                                              sl_atr=[2.0, 3.0], tp_atr=[1.5, 3.0], hold=[24, 72], limit=[True])),
    "HB_StatArb_ETHBTC": (hb_stat_arb, dict(tf=["15min", "1h"], lookback=[100, 300], z_in=[2.0, 2.5],
                                            sl_z=[1.5, 2.5], tp_z=[1.5, 2.5], hold=[96])),
    "RSI2_Maker": (rsi2_meanrev, dict(tf=["15min", "1h"], lo=[5, 10], hi=[90, 95], trend=[200],
                                      sl_atr=[2.0, 3.0], tp_atr=[1.0, 2.0], hold=[24], limit=[True])),
}
