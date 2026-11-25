"""Three role-based agents run as a sequential crew (CrewAI-style pipeline, no LLM / API key needed):
   TechnicalAnalyst -> Strategist -> RiskManager. Each agent appends its reasoning to a shared discussion log."""
import math
import pandas as pd
import pandas_ta as ta

RISK_MIN, RISK_MAX = 0.015, 0.02
MAX_LEVERAGE = 20
TAKER_FEE = 0.00055


def to_df(raw):
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def _pivots(df, w=5):
    sup, res = [], []
    for i in range(w, len(df) - w):
        win = df.iloc[i - w:i + w + 1]
        if df.low.iloc[i] == win.low.min(): sup.append(float(df.low.iloc[i]))
        if df.high.iloc[i] == win.high.max(): res.append(float(df.high.iloc[i]))
    return sup, res


class TechnicalAnalyst:
    name = "אנליסט טכני"

    def run(self, ctx):
        df = to_df(ctx["raw"])
        htf = to_df(ctx["raw_htf"])
        df["ema20"] = ta.ema(df.close, 20); df["ema50"] = ta.ema(df.close, 50)
        df["rsi"] = ta.rsi(df.close, 14); df["atr"] = ta.atr(df.high, df.low, df.close, 14)
        macd = ta.macd(df.close)
        df["macdh"] = macd.iloc[:, 1] if macd is not None else 0
        df["vol_sma"] = ta.sma(df.volume, 20)
        htf["ema50"] = ta.ema(htf.close, 50); htf["ema200"] = ta.ema(htf.close, 200)
        last, h = df.iloc[-1], htf.iloc[-1]
        price = float(last.close)
        if any(pd.isna(v) for v in (last.ema50, last.rsi, last.atr, last["macdh"])):
            ctx["analysis"] = None
            ctx["log"].append((self.name, "אין מספיק נתונים לאינדיקטורים"))
            return ctx
        htf_trend = 1 if h.close > h.ema50 else -1
        if not pd.isna(h.ema200):
            htf_trend = 1 if (h.close > h.ema50 > h.ema200) else -1 if (h.close < h.ema50 < h.ema200) else htf_trend * 0.5
        ltf_trend = 1 if last.ema20 > last.ema50 else -1
        sup, res = _pivots(df.tail(150).reset_index(drop=True))
        sup_below = [s for s in sup if s < price]; res_above = [r for r in res if r > price]
        a = dict(price=price, rsi=float(last.rsi), atr=float(last.atr), macd_hist=float(last["macdh"]),
                 ema20=float(last.ema20), ema50=float(last.ema50), htf_trend=htf_trend, ltf_trend=ltf_trend,
                 vol_ratio=float(last.volume / last.vol_sma) if last.vol_sma else 1.0,
                 support=max(sup_below) if sup_below else None, resistance=min(res_above) if res_above else None)
        ctx["analysis"] = a
        tr = "עולה" if htf_trend > 0 else "יורדת" if htf_trend < 0 else "צדדית"
        ctx["log"].append((self.name, f"מגמה בטווח גבוה: {tr}; EMA20/50 {'חיובי' if ltf_trend>0 else 'שלילי'}; RSI {a['rsi']:.0f}; MACD {'+' if a['macd_hist']>0 else '-'}; נפח x{a['vol_ratio']:.1f}; תמיכה {a['support']}, התנגדות {a['resistance']}"))
        return ctx


class Strategist:
    name = "אסטרטג"

    def run(self, ctx):
        a = ctx.get("analysis")
        if not a:
            ctx["signal"] = None; return ctx
        score = 0
        score += 2 * (1 if a["htf_trend"] > 0 else -1 if a["htf_trend"] < 0 else 0)
        score += 1 * a["ltf_trend"]
        score += 1 if a["macd_hist"] > 0 else -1
        score += 1 if 50 < a["rsi"] < 68 else -1 if 32 < a["rsi"] < 50 else 0
        if a["vol_ratio"] > 1.2: score += 1 if a["ltf_trend"] > 0 else -1
        side = "LONG" if score >= 3 else "SHORT" if score <= -3 else None
        if side is None:
            ctx["signal"] = None
            ctx["log"].append((self.name, f"ציון {score} – אין יתרון ברור, לא מוציא איתות"))
            return ctx
        price, atr = a["price"], a["atr"]
        d = 1 if side == "LONG" else -1
        entry = price
        sl = entry - d * 1.5 * atr
        # tighten/loosen SL behind nearest structure when sensible
        lvl = a["support"] if side == "LONG" else a["resistance"]
        if lvl and 0.8 * atr < abs(entry - lvl) < 2.5 * atr:
            sl = lvl - d * 0.25 * atr
        risk = abs(entry - sl)
        tp1, tp2 = entry + d * 1.5 * risk, entry + d * 3 * risk
        ctx["signal"] = dict(side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2, score=score, confidence=min(95, 40 + abs(score) * 9))
        ctx["log"].append((self.name, f"{side} (ציון {score}) כניסה {entry:.6g}, SL {sl:.6g}, TP1 {tp1:.6g}, TP2 {tp2:.6g}"))
        return ctx


class RiskManager:
    name = "מנהל סיכונים"

    def __init__(self, equity):
        self.equity = equity

    def run(self, ctx):
        s = ctx.get("signal")
        if not s:
            return ctx
        eq = self.equity
        risk_pct = RISK_MAX if s["confidence"] >= 70 else (RISK_MIN + RISK_MAX) / 2 if s["confidence"] >= 55 else RISK_MIN
        risk_usd = eq * risk_pct
        dist = abs(s["entry"] - s["sl"]) / s["entry"]
        fee_round = 2 * TAKER_FEE
        notional = risk_usd / (dist + fee_round)       # fees included in the risked amount
        lev = max(1, math.ceil(notional / (eq * 0.5)))  # use at most ~50% of equity as margin
        while lev < MAX_LEVERAGE and dist > 0.6 / lev:  # SL must sit well inside liquidation distance
            lev += 1
        notional = min(notional, eq * lev * 0.9)
        margin = notional / lev
        s.update(risk_pct=risk_pct, risk_usd=notional * dist + notional * fee_round, notional=notional, leverage=lev,
                 margin=margin, qty=notional / s["entry"], rr1=1.5, rr2=3.0)
        ctx["log"].append((self.name, f"סיכון {risk_pct*100:.2f}% = ${risk_usd:.2f} | גודל ${notional:.2f} | מינוף {lev}x | מרג'ין ${margin:.2f}"))
        return ctx


def build_signal(exchange, symbol, raw, raw_htf, equity):
    ctx = {"raw": raw, "raw_htf": raw_htf, "log": []}
    for agent in (TechnicalAnalyst(), Strategist(), RiskManager(equity)):
        ctx = agent.run(ctx)
    if not ctx.get("signal"):
        return None
    return {"exchange": exchange, "symbol": symbol, **ctx["signal"], "analysis": ctx["analysis"],
            "discussion": [{"agent": n, "text": t} for n, t in ctx["log"]]}
