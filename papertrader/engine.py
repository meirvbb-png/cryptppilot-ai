"""Paper-trading engine. Pure simulation against real public prices - never touches a real account."""
import time
import db, exchange as ex

TAKER_FEE = 0.00055
MAINT = 0.9   # liquidation when 90% of margin is lost (simplified isolated margin)


def account():
    a = db.q("SELECT * FROM account")[0]
    opened = db.q("SELECT * FROM positions WHERE status='open'")
    used = sum(p["margin"] for p in opened) + sum(p["margin"] for p in db.q("SELECT margin FROM positions WHERE status='pending'"))
    upnl = 0.0
    for p in opened:
        px = p.get("mark")
        if px is None:
            try: px = ex.last_price(p["exchange"], p["symbol"])
            except Exception: px = p["entry"]
        upnl += _pnl(p, px)
    closed = db.q("SELECT realized FROM positions WHERE status='closed'")
    wins = sum(1 for c in closed if c["realized"] > 0)
    return dict(balance=a["balance"], start=a["start_balance"], used_margin=used, available=a["balance"] - used,
                upnl=upnl, equity=a["balance"] + upnl, trades=len(closed), wins=wins,
                win_rate=(wins / len(closed) * 100) if closed else None,
                total_pnl=a["balance"] - a["start_balance"])


def _d(side): return 1 if side == "LONG" else -1
def _pnl(p, px): return _d(p["side"]) * (px - p["entry"]) * p["qty"]


def open_trade(exchange_, symbol, side, margin, leverage, sl, tp1=None, tp2=None, order_type="market", limit_price=None, source="manual"):
    side = side.upper()
    if side not in ("LONG", "SHORT"): raise ValueError("כיוון לא חוקי")
    if not (1 <= leverage <= 100): raise ValueError("מינוף חייב להיות בין 1 ל-100")
    if margin <= 0: raise ValueError("סכום חייב להיות חיובי")
    acc = account()
    fee_reserve = margin * leverage * TAKER_FEE
    if margin + fee_reserve > acc["available"] + 1e-9: raise ValueError(f"אין מספיק יתרה פנויה (${acc['available']:.2f})")
    d = _d(side)
    if order_type == "limit":
        if not limit_price or limit_price <= 0: raise ValueError("חסר מחיר לימיט")
        ref = limit_price
    else:
        ob = ex.orderbook(exchange_, symbol, 5)
        ref = float(ob["asks"][0][0] if d == 1 else ob["bids"][0][0])
    if sl is not None and sl > 0:
        if d * (ref - sl) <= 0: raise ValueError("Stop-Loss בצד הלא נכון של הכניסה")
    else:
        sl = None
    for t in (tp1, tp2):
        if t and d * (t - ref) <= 0: raise ValueError("Take-Profit בצד הלא נכון של הכניסה")
    tp1 = tp1 or None; tp2 = tp2 or None
    qty = margin * leverage / ref
    now = time.time()
    liq = ref * (1 - d * MAINT / leverage)
    if sl is not None and d * (sl - liq) <= 0: raise ValueError("ה-Stop-Loss מעבר למחיר הלִיקווידציה; הקטן מינוף")
    pid = db.x("""INSERT INTO positions(exchange,symbol,side,status,order_type,limit_price,qty,init_qty,entry,leverage,margin,sl,tp1,tp2,liq,opened,last_check,source)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (exchange_, symbol, side, "pending" if order_type == "limit" else "open", order_type, limit_price, qty, qty, ref, leverage,
                margin, sl, tp1, tp2, liq, now, now, source))
    if order_type != "limit":
        _charge_open_fee(pid)
    return pid


def _charge_open_fee(pid):
    p = db.q("SELECT * FROM positions WHERE id=?", (pid,))[0]
    fee = p["entry"] * p["qty"] * TAKER_FEE
    db.x("UPDATE account SET balance=balance-?", (fee,))
    db.x("UPDATE positions SET fees=fees+?, realized=realized-? WHERE id=?", (fee, fee, pid))


def _close(p, price, reason, qty=None):
    qty = min(qty or p["qty"], p["qty"])
    gross = _d(p["side"]) * (price - p["entry"]) * qty
    fee = price * qty * TAKER_FEE
    if reason == "LIQUIDATION":
        gross = -p["margin"] * (qty / p["init_qty"]); 
    db.x("UPDATE account SET balance=balance+?", (gross - fee,))
    left = p["qty"] - qty
    done = left <= p["init_qty"] * 1e-9
    db.x("""UPDATE positions SET qty=?, realized=realized+?, fees=fees+?, status=?, closed=?, close_price=?, close_reason=? WHERE id=?""",
         (0 if done else left, gross - fee, fee, "closed" if done else "open", time.time() if done else None,
          price if done else None, reason if done else None, p["id"]))


def close_manual(pid):
    p = db.q("SELECT * FROM positions WHERE id=?", (pid,))
    if not p: raise ValueError("לא נמצא")
    p = p[0]
    if p["status"] == "pending":
        db.x("UPDATE positions SET status='cancelled', closed=? WHERE id=?", (time.time(), pid)); return
    if p["status"] != "open": raise ValueError("הפוזיציה כבר סגורה")
    _close(p, ex.last_price(p["exchange"], p["symbol"]), "MANUAL")


def _events(p, hi, lo):
    """Return list of (reason, price, qty) triggered in a candle. Conservative: SL/liq before TP."""
    d = _d(p["side"]); out = []
    hit = lambda lvl: (lo <= lvl) if d == 1 else (hi >= lvl)
    reach = lambda lvl: (hi >= lvl) if d == 1 else (lo <= lvl)
    if hit(p["liq"]): return [("LIQUIDATION", p["liq"], p["qty"])]
    sl = p["sl"]
    if sl is not None and hit(sl):
        return [("SL" if not p["tp1_hit"] else "SL-BE", sl, p["qty"])]
    if p["tp1"] and not p["tp1_hit"] and reach(p["tp1"]):
        if p["tp2"]: out.append(("TP1", p["tp1"], p["init_qty"] / 2))
        else: return [("TP", p["tp1"], p["qty"])]
    tp_final = p["tp2"] if p["tp2"] else None
    if tp_final and reach(tp_final):
        out.append(("TP2", tp_final, None))
    return out


def tick():
    """One engine pass: fill limit orders, check SL/TP/liquidation using real 1m candles + live price."""
    for p in db.q("SELECT * FROM positions WHERE status IN ('pending','open')"):
        try:
            _process(p)
        except Exception as e:
            print("engine error", p["id"], e)


def _process(p):
    now = time.time()
    candles = ex.ohlcv(p["exchange"], p["symbol"], "1m", 15)
    px = candles[-1][4]
    since = p["last_check"] * 1000
    series = []
    for t, o, h, l, c, v in candles:
        if t + 60_000 <= since: continue
        if t <= p["opened"] * 1000 < t + 60_000:
            h = l = c   # candle that started before entry: only trust current price
        series.append((h, l))
    for hi, lo in series:
        p = db.q("SELECT * FROM positions WHERE id=?", (p["id"],))[0]
        if p["status"] == "pending":
            d = _d(p["side"])
            if (d == 1 and lo <= p["limit_price"]) or (d == -1 and hi >= p["limit_price"]):
                db.x("UPDATE positions SET status='open', opened=? WHERE id=?", (now, p["id"]))
                _charge_open_fee(p["id"]); p = db.q("SELECT * FROM positions WHERE id=?", (p["id"],))[0]
                break   # evaluate exits from the next pass (no look-ahead inside the fill candle)
            continue
        if p["status"] != "open": break
        for reason, price, qty in _events(p, hi, lo):
            p = db.q("SELECT * FROM positions WHERE id=?", (p["id"],))[0]
            if p["status"] != "open": break
            _close(p, price, reason, qty)
            if reason == "TP1":
                db.x("UPDATE positions SET tp1_hit=1, sl=entry WHERE id=?", (p["id"],))
        p = db.q("SELECT * FROM positions WHERE id=?", (p["id"],))[0]
        if p["status"] != "open": break
    db.x("UPDATE positions SET last_check=? WHERE id=?", (now, p["id"]))
