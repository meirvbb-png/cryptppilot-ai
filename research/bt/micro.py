"""Tick-level simulator for Hummingbot-style microstructure strategies on Bybit L2 data.

Inputs: 100ms rebuilt order-book features (bid, ask, depth L1/L5/L20) and every trade.
Strategies (each trade has a fixed entry / stop-loss / take-profit):
  * OBI_Maker  : order-book-imbalance signal -> join best bid/ask with a limit (maker) order;
                 TP = limit (maker), SL = stop-market (taker), time stop. Cancel when stale.
  * OBI_Taker  : same signal, market entry (taker) at the touch.
  * PMM_Dyn    : Hummingbot pmm_dynamic-like: quote both sides at mid +/- k*sigma, skewed by
                 imbalance; each fill becomes a position with its own TP/SL (triple barrier).
Fill model: a resting limit fills only if a trade prints THROUGH its price (strict=True), or
AT its price (strict=False, optimistic upper bound: assumes front of queue).
"""
import numpy as np
import numba as nb
import polars as pl
import os, glob

D = os.path.join(os.path.dirname(__file__), "..", "data")


def load_day(sym, day):
    ob = pl.read_parquet(f"{D}/ob/{sym}_{day}.parquet")
    tk = pl.read_parquet(f"{D}/ticks/{sym}_{day}.parquet")
    return ob, tk


@nb.njit(cache=True)
def _sim(ob_ts, bid, ask, imb, vol, tk_ts, tk_px, tk_buy, mode, thr, tp_k, sl_k, max_hold_ms,
         stale_ms, cooldown_ms, strict, maker, taker, slip_ticks, tick, notional):
    """mode 0 = OBI maker, 1 = OBI taker, 2 = PMM dynamic (two-sided, k*vol spread)."""
    n = len(ob_ts); m = len(tk_ts)
    res = np.zeros((200000, 6))  # entry_ts, side, entry, exit, pnl_net, reason
    k = 0; ti = 0
    state = 0  # 0 flat, 1 working entry order(s), 2 in position
    side = 0; opx = 0.0; obid = 0.0; oask = 0.0; t_order = 0; t_entry = 0; entry = 0.0
    tp = 0.0; sl = 0.0; next_ok = 0; fee_in = maker
    for i in range(n - 1):
        t = ob_ts[i]; tnext = ob_ts[i + 1]
        b = bid[i]; a = ask[i]; mid = 0.5 * (a + b)
        # ---- decide on this book update ----
        if state == 0 and t >= next_ok:
            dist = max(tick, vol[i] * mid)
            if mode == 0:
                if imb[i] > thr:
                    side = 1; opx = b; state = 1; t_order = t
                elif imb[i] < -thr:
                    side = -1; opx = a; state = 1; t_order = t
            elif mode == 1:
                if imb[i] > thr or imb[i] < -thr:
                    side = 1 if imb[i] > 0 else -1
                    entry = (a + slip_ticks * tick) if side > 0 else (b - slip_ticks * tick)
                    fee_in = taker; t_entry = t; state = 2
                    tp = entry + side * tp_k * dist; sl = entry - side * sl_k * dist
            else:
                skew = imb[i] * dist * 0.5
                obid = min(b, np.floor((mid - thr * dist + skew) / tick) * tick)
                oask = max(a, np.ceil((mid + thr * dist + skew) / tick) * tick)
                state = 1; side = 0; t_order = t
        elif state == 1:
            stale = (t - t_order) > stale_ms
            if mode == 0 and (stale or (side > 0 and imb[i] < 0) or (side < 0 and imb[i] > 0)):
                state = 0
            elif mode == 2 and stale:
                state = 0
        elif state == 2 and (t - t_entry) > max_hold_ms:
            ex = (b - slip_ticks * tick) if side > 0 else (a + slip_ticks * tick)
            q = notional / entry
            pnl = side * q * (ex - entry) - q * (entry * fee_in + ex * taker)
            res[k, 0] = t_entry; res[k, 1] = side; res[k, 2] = entry; res[k, 3] = ex
            res[k, 4] = pnl; res[k, 5] = 3; k += 1; state = 0; next_ok = t + cooldown_ms
        # ---- process trades until next book update ----
        while ti < m and tk_ts[ti] < tnext:
            p = tk_px[ti]; seller = not tk_buy[ti]
            if state == 1:
                if mode == 0:
                    hit = (side > 0 and seller and (p < opx if strict else p <= opx)) or \
                          (side < 0 and (not seller) and (p > opx if strict else p >= opx))
                    if hit:
                        entry = opx; fee_in = maker; t_entry = tk_ts[ti]; state = 2
                        dist = max(tick, vol[i] * mid)
                        tp = entry + side * tp_k * dist; sl = entry - side * sl_k * dist
                else:
                    hb = seller and (p < obid if strict else p <= obid)
                    ha = (not seller) and (p > oask if strict else p >= oask)
                    if hb or ha:
                        side = 1 if hb else -1; entry = obid if hb else oask
                        fee_in = maker; t_entry = tk_ts[ti]; state = 2
                        dist = max(tick, vol[i] * mid)
                        tp = entry + side * tp_k * dist; sl = entry - side * sl_k * dist
            elif state == 2 and tk_ts[ti] > t_entry:
                reason = 0; ex = 0.0; fo = taker
                if side > 0:
                    if p <= sl: ex = p - slip_ticks * tick; reason = 1
                    elif p > tp: ex = tp; reason = 2; fo = maker
                else:
                    if p >= sl: ex = p + slip_ticks * tick; reason = 1
                    elif p < tp: ex = tp; reason = 2; fo = maker
                if reason:
                    q = notional / entry
                    pnl = side * q * (ex - entry) - q * (entry * fee_in + ex * fo)
                    res[k, 0] = t_entry; res[k, 1] = side; res[k, 2] = entry; res[k, 3] = ex
                    res[k, 4] = pnl; res[k, 5] = reason; k += 1
                    state = 0; next_ok = tk_ts[ti] + cooldown_ms
            ti += 1
    return res[:k]


def prep(ob, depth="5", ema_n=50, vol_win=600):
    b = ob["bid"].to_numpy(); a = ob["ask"].to_numpy()
    bq = ob[f"bq{depth}"].to_numpy(); aq = ob[f"aq{depth}"].to_numpy()
    raw = (bq - aq) / (bq + aq)
    alpha = 2 / (ema_n + 1)
    imb = np.empty_like(raw); s = 0.0
    for i in range(len(raw)):          # EMA smoothing of imbalance
        s = alpha * raw[i] + (1 - alpha) * s; imb[i] = s
    mid = 0.5 * (a + b)
    r = np.r_[0, np.diff(np.log(mid))]
    # rolling std of 100ms log-returns over vol_win updates, scaled to ~1 minute
    c1 = np.cumsum(r); c2 = np.cumsum(r * r)
    w = vol_win
    var = (c2[w:] - c2[:-w]) / w - ((c1[w:] - c1[:-w]) / w) ** 2
    vol = np.r_[np.full(w, np.nan), np.sqrt(np.maximum(var, 0))] * np.sqrt(600)
    vol = np.nan_to_num(vol, nan=np.nanmedian(vol))
    return ob["ts"].to_numpy(), b, a, imb, vol


def run_day(sym, day, mode, thr, tp_k, sl_k, hold_s=300, stale_s=10, cool_s=5, strict=True,
            maker=0.0002, taker=0.00055, slip_ticks=1, notional=2500.0, depth="5"):
    from .data import TICK
    ob, tk = load_day(sym, day)
    ts, b, a, imb, vol = prep(ob, depth)
    return _sim(ts, b, a, imb, vol, tk["ts"].to_numpy(), tk["price"].to_numpy(), tk["buy"].to_numpy(),
                mode, thr, tp_k, sl_k, hold_s * 1000, stale_s * 1000, cool_s * 1000, strict,
                maker, taker, slip_ticks, TICK[sym], notional)


def days_available(sym):
    return sorted(os.path.basename(f)[len(sym) + 1:-8] for f in glob.glob(f"{D}/ob/{sym}_*.parquet"))
