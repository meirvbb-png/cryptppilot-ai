"""Market regime on 4h bars: volatile (ATR% in top 20% of the last 90 days), trend (ADX>25),
otherwise range. Cached per symbol per 4h bar."""
import numpy as np
from research.bt import ind
from research.bt.data import resample

_cache = {}
REGIMES = ("טרנד", "דשדוש", "תנודתי")


def regime_detail(store, symbol):
    df = store.frame(symbol)
    if len(df) < 60 * 24 * 20:
        return dict(regime="לא ידוע", adx=None, atr_pct=None, atr_rank=None, trend_dir=0)
    bar = df.index[-1].floor("4h")
    if symbol in _cache and _cache[symbol][0] == bar:
        return _cache[symbol][1]
    b = resample(df.iloc[-60 * 24 * 100:], "4h")
    adx = ind.adx(b, 14); atrp = ind.atr(b, 14) / b.close
    rank = float((atrp.iloc[-540:] <= atrp.iloc[-1]).mean())
    e50, e200 = ind.ema(b.close, 50).iloc[-1], ind.ema(b.close, 200).iloc[-1]
    a = float(adx.iloc[-1])
    reg = "תנודתי" if rank > 0.8 else "טרנד" if a > 25 else "דשדוש"
    out = dict(regime=reg, adx=round(a, 1), atr_pct=round(100 * float(atrp.iloc[-1]), 2),
               atr_rank=round(100 * rank), trend_dir=int(np.sign(e50 - e200)))
    _cache[symbol] = (bar, out)
    return out


def regime_of(store, symbol):
    return regime_detail(store, symbol)["regime"]
