"""Live trade signals for manual use (e.g. in a Bybit demo account) + optional Telegram alerts.

Every new signal from any bot is recorded with entry / stop-loss / take-profit, a suggested size
for a 500 USDT account risking 1% (5 USDT), and is then tracked against live prices until it hits
TP, hits SL or expires. Outcomes use the same rules as the backtest (SL first inside a minute).
Telegram: the bot token and chat are entered in the app (stored in the local DB, never in git)."""
import json, logging, time
import httpx, pandas as pd
from . import config as C, db

log = logging.getLogger("signals")
ACCOUNT, RISK_PCT, MAX_LEV = 500.0, 0.01, 5.0

db.x("""CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot_id TEXT,
  bot_name TEXT, verdict TEXT, symbol TEXT, side INTEGER, entry REAL, sl REAL, tp REAL, rr REAL,
  hold_min INTEGER, expires_ts REAL, qty REAL, notional REAL, leverage REAL, risk_usd REAL, regime TEXT,
  status TEXT DEFAULT 'active', close_ts REAL, close_px REAL, result_r REAL, checked_ts REAL)""")
db.x("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")


def kv_get(k, default=None):
    r = db.q("SELECT value FROM kv WHERE key=?", (k,))
    return json.loads(r[0]["value"]) if r else default


def kv_set(k, v):
    db.x("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))


def _px(p, sym):
    t = C.TICK.get(sym, 0.01)
    return round(round(p / t) * t, 8)


SYM = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
STATUS_HE = {"active": "פעיל", "tp": "✅ הגיע ליעד", "sl": "❌ נגע בסטופ", "expired": "⌛ פג תוקף"}


# ------------------------------------------------------------------ telegram
def telegram_send(text):
    """Fire-and-forget (never blocks the price loop)."""
    cfg = kv_get("telegram") or {}
    if not (cfg.get("token") and cfg.get("chat_id")):
        return False
    import threading
    threading.Thread(target=_tg_post, args=(cfg, text), daemon=True).start()
    return True


def _tg_post(cfg, text):
    try:
        r = httpx.post(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", timeout=15,
                       json=dict(chat_id=cfg["chat_id"], text=text, disable_web_page_preview=True))
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            log.warning("telegram: %s", r.text[:200])
        return bool(ok)
    except Exception as e:
        log.warning("telegram: %s", e)
        return False


def telegram_connect(token):
    """Validate the bot token and find the chat of whoever last wrote to the bot (/start)."""
    token = token.strip()
    me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not me.get("ok"):
        raise ValueError("הטוקן של בוט הטלגרם לא תקין")
    ups = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json().get("result", [])
    chats = [u.get("message", {}).get("chat", {}).get("id") for u in ups if u.get("message")]
    chats = [c for c in chats if c]
    if not chats:
        raise ValueError(f"שלח קודם הודעה (למשל /start) לבוט @{me['result']['username']} בטלגרם, ואז נסה שוב")
    kv_set("telegram", dict(token=token, chat_id=chats[-1], username=me["result"]["username"]))
    telegram_send("✅ החיבור הצליח! מעכשיו תקבל כאן כל איתות חדש מהבוטים, עם כניסה, סטופ ויעד.")
    return me["result"]["username"]


def _fmt_new(s):
    d = "🟢 לונג" if s["side"] > 0 else "🔴 שורט"
    return (f"📣 איתות חדש · {s['bot_name']}\n{SYM.get(s['symbol'], s['symbol'])} {d}\n"
            f"כניסה: {s['entry']}\nסטופ-לוס: {s['sl']}\nטייק-פרופיט: {s['tp']}\n"
            f"יחס סיכוי/סיכון: 1:{s['rr']:.1f}\n"
            f"גודל מוצע לתיק $500 (סיכון $5): {s['qty']} ({s['leverage']:.1f}x)\n"
            f"בדיקות עבר: {s['verdict']} · בתוקף עד {time.strftime('%d/%m %H:%M', time.localtime(s['expires_ts']))}")


def _fmt_close(s):
    return (f"{STATUS_HE[s['status']]} · {SYM.get(s['symbol'], s['symbol'])} "
            f"{'לונג' if s['side'] > 0 else 'שורט'} ({s['bot_name']})\n"
            f"כניסה {s['entry']} → {s['close_px']} · תוצאה {s['result_r']:+.2f}R")


# ------------------------------------------------------------------ record / track
def record(bot, symbol, r, price, regime):
    side = int(r["sig"])
    if side == 0 or not r.get("sl_d"):
        return None
    if db.q("SELECT id FROM signals WHERE bot_id=? AND symbol=? AND side=? AND status='active'", (bot.id, symbol, side)):
        return None                                   # same idea still active -> no duplicate
    entry = _px(price, symbol)
    sl = _px(entry - side * r["sl_d"], symbol); tp = _px(entry + side * r["tp_d"], symbol)
    risk = ACCOUNT * RISK_PCT
    qty = min(risk / r["sl_d"], ACCOUNT * MAX_LEV / entry)
    now = time.time()
    s = dict(ts=now, bot_id=bot.id, bot_name=bot.spec["name"], verdict=bot.spec["bench"]["verdict"], symbol=symbol,
             side=side, entry=entry, sl=sl, tp=tp, rr=r["tp_d"] / r["sl_d"], hold_min=int(r["hold_min"]),
             expires_ts=now + r["hold_min"] * 60, qty=round(qty, 4), notional=round(qty * entry, 2),
             leverage=qty * entry / ACCOUNT, risk_usd=round(qty * r["sl_d"], 2), regime=regime, checked_ts=now)
    s["id"] = db.x(f"INSERT INTO signals({','.join(s)}) VALUES({','.join('?' * len(s))})", tuple(s.values()))
    db.event(bot.id, f"איתות {symbol} {'לונג' if side > 0 else 'שורט'}: כניסה {entry} | SL {sl} | TP {tp}")
    telegram_send(_fmt_new(s))
    return s


def _close(s, status, px, at):
    risk_px = abs(s["entry"] - s["sl"])
    res = s["side"] * (px - s["entry"]) / risk_px if risk_px else 0
    db.x("UPDATE signals SET status=?, close_ts=?, close_px=?, result_r=? WHERE id=?", (status, at, px, res, s["id"]))
    s.update(status=status, close_ts=at, close_px=px, result_r=res)
    telegram_send(_fmt_close(s))


_active_cache = {"ts": 0, "rows": []}


def _active():
    if time.time() - _active_cache["ts"] > 2:
        _active_cache.update(ts=time.time(), rows=db.q("SELECT * FROM signals WHERE status='active'"))
    return _active_cache["rows"]


def on_tick(symbol, ts_ms, price):
    tick = C.TICK.get(symbol, 0.01); now = time.time(); changed = False
    for s in _active():
        if s["symbol"] != symbol or s["status"] != "active":
            continue
        d = s["side"]
        if (d > 0 and price <= s["sl"]) or (d < 0 and price >= s["sl"]):
            _close(s, "sl", s["sl"], now); changed = True
        elif (d > 0 and price >= s["tp"] + tick) or (d < 0 and price <= s["tp"] - tick):
            _close(s, "tp", s["tp"], now); changed = True
        elif now > s["expires_ts"]:
            _close(s, "expired", price, now); changed = True
    if changed:
        _active_cache["ts"] = 0


def catch_up(store):
    """Settle active signals against 1m bars that arrived late (sleep / reconnect)."""
    for s in db.q("SELECT * FROM signals WHERE status='active'"):
        tick = C.TICK.get(s["symbol"], 0.01); d = s["side"]
        start = pd.Timestamp(int(max(s["ts"], s["checked_ts"] or 0) * 1000) // 60000 * 60000 + 60000, unit="ms")
        bars = store.frame(s["symbol"]).loc[start:]
        for ts, b in bars.iterrows():
            at = ts.timestamp() + 59
            if at > s["expires_ts"]:
                _close(s, "expired", b.close, s["expires_ts"]); break
            if (d > 0 and b.low <= s["sl"]) or (d < 0 and b.high >= s["sl"]):
                _close(s, "sl", s["sl"], at); break
            if (d > 0 and b.high >= s["tp"] + tick) or (d < 0 and b.low <= s["tp"] - tick):
                _close(s, "tp", s["tp"], at); break
        else:
            if len(bars):
                db.x("UPDATE signals SET checked_ts=? WHERE id=?", (bars.index[-1].timestamp() + 60, s["id"]))
    _active_cache["ts"] = 0


def summary():
    rows = db.q("SELECT status, result_r FROM signals WHERE status!='active'")
    done = [r for r in rows if r["status"] in ("tp", "sl", "expired")]
    wins = [r for r in done if (r["result_r"] or 0) > 0]
    return dict(total=len(done), wins=len(wins), win_pct=round(100 * len(wins) / len(done), 1) if done else None,
                avg_r=round(sum(r["result_r"] or 0 for r in done) / len(done), 2) if done else None,
                active=len(db.q("SELECT id FROM signals WHERE status='active'")))
