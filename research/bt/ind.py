"""Indicators (pandas/numba), matching pandas-ta / TA-Lib definitions used by Hummingbot & FreqAI."""
import numpy as np
import pandas as pd
try:
    import numba as nb
except ImportError:                       # lean installs: same code, pure Python
    class _NB:
        @staticmethod
        def njit(*a, **k):
            if a and callable(a[0]):
                return a[0]
            return lambda f: f
    nb = _NB()


def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bbands(c, n=20, k=2.0):
    m = c.rolling(n).mean(); sd = c.rolling(n).std(ddof=0)
    up, lo = m + k * sd, m - k * sd
    return lo, m, up, (c - lo) / (up - lo)          # BBP like pandas-ta


def macd(c, f=12, s=26, sig=9):
    m = ema(c, f) - ema(c, s); sg = ema(m, sig)
    return m, sg, m - sg


def adx(df, n=14):
    up = df.high.diff(); dn = -df.low.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    ndi = 100 * pd.Series(ndm, df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def mfi(df, n=14):
    tp = (df.high + df.low + df.close) / 3; mf = tp * df.volume
    pos = mf.where(tp > tp.shift(), 0).rolling(n).sum(); neg = mf.where(tp < tp.shift(), 0).rolling(n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))


@nb.njit(cache=False)
def _supertrend(h, l, c, a, mult):
    n = len(c); d = np.ones(n, np.int8); st = np.full(n, np.nan)
    ub = (h + l) / 2 + mult * a; lb = (h + l) / 2 - mult * a
    fu = ub.copy(); fl = lb.copy()
    for i in range(1, n):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if c[i] > fu[i - 1]: d[i] = 1
        elif c[i] < fl[i - 1]: d[i] = -1
        else: d[i] = d[i - 1]
        st[i] = fl[i] if d[i] == 1 else fu[i]
    return st, d


def supertrend(df, n=20, mult=4.0):
    a = atr(df, n).to_numpy()
    st, d = _supertrend(df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy(), a, mult)
    return pd.Series(st, df.index), pd.Series(d, df.index)
