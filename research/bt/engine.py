"""Strict bar-by-bar backtest engine for single-position bots on Bybit USDT perpetuals.

Execution model (conservative by design):
  * Strategies emit signals on CLOSED bars of their own timeframe. Entry happens at the
    open of the next 1-minute bar (no look-ahead).
  * Every open trade is walked through 1-minute bars. If stop-loss and take-profit are
    both inside the same minute, the stop-loss is assumed to fill first.
  * Market entry / stop-loss / signal exit / time exit -> taker fee + slippage.
    Take-profit is a resting limit order -> maker fee, fills only if price trades
    THROUGH the level by one tick.
  * Optional limit (maker) entry: order rests at the signal price for `limit_wait`
    minutes; it fills only if price trades through it, otherwise it is cancelled.
  * Funding: a flat cost of `funding_8h` of notional per 8h held (both sides pay;
    pessimistic, Bybit funding is usually +0.01% paid by longs to shorts).
  * Sizing: risk `risk` of current equity on the stop distance, capped by `max_lev`.
"""
from dataclasses import dataclass
import numpy as np
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

TAKER = 0.00055   # Bybit USDT perp, non-VIP
MAKER = 0.00020
SLIP = 0.0002     # extra adverse slippage on market fills (2 bps)
FUND_8H = 0.0001


@dataclass
class Costs:
    taker: float = TAKER
    maker: float = MAKER
    slip: float = SLIP
    funding_8h: float = FUND_8H


@nb.njit(cache=False)
def _simulate(o, h, l, c, sig, sl_d, tp_d, max_hold, exit_l, exit_s,
              tick, equity0, risk, max_lev, taker, maker, slip, fund8h,
              limit_entry, limit_wait):
    n = len(o)
    eq = equity0
    # trade log columns: entry_i, exit_i, side, entry_px, exit_px, sl, tp, qty, pnl_net, fees, reason
    out = np.zeros((n // 2 + 1, 11))
    k = 0
    i = 0
    eq_curve = np.empty(n)
    while i < n:
        eq_curve[i] = eq
        s = sig[i]
        if s == 0 or i + 1 >= n:
            i += 1
            continue
        # ---- entry -------------------------------------------------------
        j = i + 1
        if limit_entry:
            px = c[i]
            filled = -1
            for w in range(j, min(n, j + limit_wait)):
                if (s > 0 and l[w] <= px - tick) or (s < 0 and h[w] >= px + tick):
                    filled = w
                    break
            if filled < 0:
                i += 1
                continue
            j = filled
            entry = px
            fee_in = maker
        else:
            entry = o[j] * (1 + slip * s)
            fee_in = taker
        sl = entry - s * sl_d[i]
        tp = entry + s * tp_d[i]
        qty = min(eq * risk / sl_d[i], eq * max_lev / entry)
        if qty <= 0:
            i += 1
            continue
        # ---- walk the trade ---------------------------------------------
        exit_px = 0.0
        reason = 0  # 1 sl, 2 tp, 3 time, 4 signal, 5 end
        fee_out = taker
        last = min(n - 1, j + max_hold[i])
        start = j
        if limit_entry:
            # on the fill bar only count the part after the fill: be conservative and
            # check the stop on the same bar, but not the take-profit
            if (s > 0 and l[j] <= sl) or (s < 0 and h[j] >= sl):
                exit_px = sl * (1 - slip * s)
                reason = 1
            start = j + 1
        m = start
        if reason == 0:
            while m <= last:
                if s > 0:
                    if o[m] <= sl:            # gap through stop
                        exit_px = o[m] * (1 - slip); reason = 1; break
                    if l[m] <= sl:
                        exit_px = sl * (1 - slip); reason = 1; break
                    if h[m] >= tp + tick:
                        exit_px = tp; reason = 2; fee_out = maker; break
                else:
                    if o[m] >= sl:
                        exit_px = o[m] * (1 + slip); reason = 1; break
                    if h[m] >= sl:
                        exit_px = sl * (1 + slip); reason = 1; break
                    if l[m] <= tp - tick:
                        exit_px = tp; reason = 2; fee_out = maker; break
                if (s > 0 and exit_l[m]) or (s < 0 and exit_s[m]):
                    if m + 1 < n:
                        m += 1
                        exit_px = o[m] * (1 - slip * s); reason = 4
                        break
                m += 1
            if reason == 0:
                m = last
                if m + 1 < n:
                    m += 1
                    exit_px = o[m] * (1 - slip * s)
                    reason = 3
                else:
                    exit_px = c[m]
                    reason = 5
        held_min = m - j + 1
        fees = qty * (entry * fee_in + exit_px * fee_out) + qty * entry * fund8h * (held_min / 480.0)
        pnl = s * qty * (exit_px - entry) - fees
        # mark-to-market equity while the trade is open (so drawdown includes open losses)
        for z in range(i, min(n, m)):
            eq_curve[z] = eq if z < j else eq + s * qty * (c[z] - entry) - qty * entry * fee_in
        eq += pnl
        out[k, 0] = j; out[k, 1] = m; out[k, 2] = s; out[k, 3] = entry; out[k, 4] = exit_px
        out[k, 5] = sl; out[k, 6] = tp; out[k, 7] = qty; out[k, 8] = pnl; out[k, 9] = fees
        out[k, 10] = reason
        k += 1
        eq_curve[m] = eq
        if eq <= equity0 * 0.05:
            for z in range(m, n):
                eq_curve[z] = eq
            break
        i = m + 1
    return out[:k], eq_curve


def run(df1m, sig, sl_d, tp_d, max_hold, exit_l=None, exit_s=None, tick=0.1,
        equity0=500.0, risk=0.01, max_lev=5.0, costs=Costs(), limit_entry=False, limit_wait=5):
    n = len(df1m)
    z = np.zeros(n, dtype=np.bool_)
    trades, eq = _simulate(df1m["open"].to_numpy(), df1m["high"].to_numpy(), df1m["low"].to_numpy(),
                           df1m["close"].to_numpy(), sig.astype(np.int8), sl_d.astype(np.float64),
                           tp_d.astype(np.float64), max_hold.astype(np.int64),
                           z if exit_l is None else exit_l.astype(np.bool_),
                           z if exit_s is None else exit_s.astype(np.bool_),
                           tick, equity0, risk, max_lev, costs.taker, costs.maker, costs.slip,
                           costs.funding_8h, limit_entry, limit_wait)
    return trades, eq


def metrics(trades, eq, t_ms, equity0=500.0):
    """Daily-equity Sharpe (365d), profit factor, max drawdown, etc."""
    day = (t_ms // 86_400_000)
    last_idx = np.r_[np.nonzero(np.diff(day))[0], len(day) - 1]
    deq = eq[last_idx]
    rets = np.diff(np.r_[equity0, deq]) / np.r_[equity0, deq[:-1]]
    sharpe = rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(np.r_[equity0, eq])
    mdd = ((np.r_[equity0, eq] - peak) / peak).min()
    pnl = trades[:, 8] if len(trades) else np.zeros(0)
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    days = max(1, len(deq))
    final = eq[-1]
    return dict(trades=int(len(pnl)), net_pct=100 * (final / equity0 - 1),
                cagr_pct=100 * ((final / equity0) ** (365 / days) - 1) if final > 0 else -100,
                pf=gp / gl if gl > 0 else np.inf, sharpe=sharpe, mdd_pct=100 * mdd,
                win_pct=100 * (pnl > 0).mean() if len(pnl) else 0,
                fees_usd=trades[:, 9].sum() if len(trades) else 0, final=final)
