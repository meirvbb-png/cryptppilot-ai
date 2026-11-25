"""Bot engine: 4 independent bots, each with its own 500 USDT portfolio and its own mode.

Modes:  paper   - virtual trades in real time on live Bybit prices (default)
        signals - signals + theoretical results only (no orders, balance is theoretical)
        live    - real orders on Bybit (only if BYBIT_LIVE_ENABLED=true and keys present)
Fills (paper/signals) use the same cost model as the backtest: market entry/SL/time/signal exit
pay taker + slippage, TP is a resting limit (maker) filled only when price trades through it,
funding is charged per 8h held. Every trade has Entry / Stop-Loss / Take-Profit fixed at entry."""
import asyncio, json, logging, math, time
import pandas as pd
from . import config as C, db
from .strategies import BOTS, make_adapter, TF_MIN
from .regime import regime_of

log = logging.getLogger("bots")
LOT = {"BTCUSDT": 0.001, "ETHUSDT": 0.01, "SOLUSDT": 0.1}     # Bybit qtyStep = minOrderQty
MAX_LEV = 5.0
MODES = ("paper", "signals", "live")
MODE_HE = {"paper": "דמו", "signals": "איתותים", "live": "חי"}


def _round_step(x, step):
    return math.floor(x / step + 1e-9) * step


def _round_px(p, sym):
    t = C.TICK.get(sym, 0.01)
    return round(round(p / t) * t, 8)


class Bot:
    def __init__(self, spec, store, broker_live=None):
        self.spec = spec; self.id = spec["id"]; self.store = store
        self.adapter = make_adapter(spec); self.broker_live = broker_live
        row = db.q("SELECT * FROM bots WHERE id=?", (self.id,))
        if not row:
            db.x("INSERT INTO bots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (self.id, spec["name"], spec["strategy"], "paper", 500.0, 500.0, 500.0, 0.01, 0,
                  db.jdump({"blocked_regimes": [], "params": spec["params"]}), time.time()))
            db.x("INSERT INTO equity VALUES(?,?,?)", (self.id, time.time(), 500.0))
        self.last_eval = {}
        self._checked = {}
        saved = self.settings.get("params")
        if saved and hasattr(self.adapter, "p"):
            self.adapter.p = dict(self.adapter.p, **saved)    # parameters approved earlier via the AI
        self._reload()

    def _reload(self):
        self._open = db.q("SELECT * FROM trades WHERE bot_id=? AND status='open'", (self.id,))

    # ---------- state ----------
    @property
    def row(self):
        return db.q("SELECT * FROM bots WHERE id=?", (self.id,))[0]

    @property
    def settings(self):
        return json.loads(self.row["settings"] or "{}")

    def set_settings(self, **kw):
        s = self.settings; s.update(kw)
        db.x("UPDATE bots SET settings=? WHERE id=?", (db.jdump(s), self.id))

    def open_trades(self, symbol=None):
        return [t for t in self._open if symbol is None or t["symbol"] == symbol]

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("mode")
        if mode == "live" and not (C.LIVE_ENABLED and C.BYBIT_KEY and C.BYBIT_SECRET and self.broker_live):
            raise PermissionError("מצב חי כבוי: נדרש BYBIT_LIVE_ENABLED=true ומפתחות API בקובץ .env")
        if self.open_trades():
            raise RuntimeError("יש עסקאות פתוחות - סגור אותן לפני החלפת מצב")
        db.x("UPDATE bots SET mode=? WHERE id=?", (mode, self.id))
        db.event(self.id, f"מצב הוחלף ל-{MODE_HE[mode]}")

    # ---------- strategy evaluation on bar close ----------
    async def on_bar(self, symbol, minute_ts):
        if self.open_trades(symbol):
            self.catch_up()
        tfm = TF_MIN[self.spec["tf"]]
        closed_end = minute_ts + pd.Timedelta("1min")
        if (closed_end.value // 60_000_000_000) % tfm != 0:
            return
        bar_start = closed_end - pd.Timedelta(minutes=tfm)
        if not self._bar_complete(symbol, bar_start, minute_ts, tfm):
            await asyncio.to_thread(self.store.repair, 6)          # try to fill the gap from Bybit REST now
            if not self._bar_complete(symbol, bar_start, minute_ts, tfm):
                db.event(self.id, f"{symbol}: נר {bar_start:%H:%M} חסר נתונים (ניתוק/שינה) - לא נבדק, למניעת איתות שגוי", "warn")
                self.last_eval[symbol] = dict(status="missing_data", ts=time.time())
                return
        df = self.store.frame(symbol).loc[:minute_ts]
        corr = None
        if symbol != "BTCUSDT" and self.spec["strategy"] == "FreqAI_LGBM_Clf":
            corr = self.store.frame("BTCUSDT").loc[:minute_ts].reindex(df.index).ffill()
        try:
            if self.spec["strategy"] == "FreqAI_LGBM_Clf":
                r = await asyncio.to_thread(self.adapter.evaluate, df, corr, symbol)
            else:
                r = await asyncio.to_thread(self.adapter.evaluate, df)
        except Exception as e:
            log.exception("eval %s %s", self.id, symbol); db.event(self.id, f"שגיאת חישוב {symbol}: {e}", "error")
            return
        if r is None:
            self.last_eval[symbol] = dict(status="warmup", ts=time.time())
            return
        self.last_eval[symbol] = dict(r, ts=time.time())
        self.act(symbol, r)

    def _bar_complete(self, symbol, start, end, tfm):
        n = len(self.store.frame(symbol).loc[start:end])
        return n >= 0.9 * tfm

    def act(self, symbol, r):
        price = self.store.last.get(symbol, (0, r["price"]))[1]
        for t in self.open_trades(symbol):
            if (t["side"] > 0 and r["exit_l"]) or (t["side"] < 0 and r["exit_s"]):
                self.close(t, price, "signal")
        if r["sig"] == 0 or self.open_trades(symbol):
            return
        row = self.row
        if row["paused"]:
            db.event(self.id, f"איתות {symbol} {'לונג' if r['sig']>0 else 'שורט'} דולג (הבוט מושהה)")
            return
        reg = regime_of(self.store, symbol)
        if reg in self.settings.get("blocked_regimes", []):
            db.event(self.id, f"איתות {symbol} דולג - מצב שוק '{reg}' חסום ע\"י ה-AI (באישורך)")
            return
        self.open(symbol, r, price, reg)

    # ---------- execution ----------
    def open(self, symbol, r, price, regime):
        row = self.row; side = r["sig"]; eq = row["equity"]
        entry = _round_px(price * (1 + C.SLIP * side), symbol)
        sl = _round_px(entry - side * r["sl_d"], symbol); tp = _round_px(entry + side * r["tp_d"], symbol)
        n_sym = len(C.SYMBOLS)
        risk_usd = eq * row["risk"] / n_sym                  # risk split across coins (= tested per-coin risk)
        qty = min(risk_usd / r["sl_d"], eq * MAX_LEV / n_sym / entry)
        qty = _round_step(qty, LOT[symbol])
        if qty <= 0:
            mq = LOT[symbol]
            if mq * r["sl_d"] <= eq * 0.01:                  # min lot still within 1% of equity
                qty = mq
            else:
                db.event(self.id, f"איתות {symbol} דולג: גודל מינימלי ({mq}) מסכן יותר מ-1% מהתיק", "warn")
                return
        notional = qty * entry
        order_id = None
        if row["mode"] == "live":
            try:
                order_id = self.broker_live.open(symbol, side, qty, sl, tp)
            except Exception as e:
                db.event(self.id, f"פקודה חיה נכשלה {symbol}: {e}", "error"); return
        fee_in = notional * C.TAKER
        db.x("""INSERT INTO trades(bot_id,symbol,side,mode,status,entry_ts,entry_px,sl,tp,qty,notional,leverage,
                max_hold_min,fees,funding,regime,hour,equity_before,order_id,info)
                VALUES(?,?,?,?, 'open',?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
             (self.id, symbol, side, row["mode"], time.time(), entry, sl, tp, qty, notional, notional / eq,
              r["hold_min"], fee_in, regime, time.gmtime().tm_hour, eq, order_id, db.jdump(r.get("info", {}))))
        self._reload()
        db.event(self.id, f"נפתחה {'לונג' if side>0 else 'שורט'} {symbol} @ {entry} | SL {sl} | TP {tp} | כמות {qty}")

    def close(self, t, price, reason, at_ts=None, exit_override=None):
        side = t["side"]
        at_ts = at_ts or time.time()
        if exit_override is not None:
            exit_px, fee_rate = _round_px(exit_override, t["symbol"]), C.TAKER
        elif reason == "tp":
            exit_px, fee_rate = t["tp"], C.MAKER
        elif reason == "sl":
            exit_px, fee_rate = _round_px(t["sl"] * (1 - C.SLIP * side), t["symbol"]), C.TAKER
        else:
            exit_px, fee_rate = _round_px(price * (1 - C.SLIP * side), t["symbol"]), C.TAKER
        if t["mode"] == "live" and self.broker_live and reason in ("signal", "time", "manual"):
            try:
                self.broker_live.close(t["symbol"], side, t["qty"])
            except Exception as e:
                db.event(self.id, f"סגירה חיה נכשלה: {e}", "error"); return
        hours = max(0.0, at_ts - t["entry_ts"]) / 3600
        funding = t["notional"] * C.FUNDING_8H * hours / 8
        fees = t["fees"] + t["qty"] * exit_px * fee_rate
        pnl = side * t["qty"] * (exit_px - t["entry_px"]) - fees - funding
        eq = self.row["equity"] + pnl
        db.x("""UPDATE trades SET status='closed', exit_ts=?, exit_px=?, reason=?, fees=?, funding=?, pnl_net=?,
                pnl_pct=? WHERE id=?""", (at_ts, exit_px, reason, fees, funding, pnl,
                                         100 * pnl / t["equity_before"], t["id"]))
        db.x("UPDATE bots SET equity=? WHERE id=?", (eq, self.id))
        db.x("INSERT INTO equity VALUES(?,?,?)", (self.id, at_ts, eq))
        self._reload()
        he = {"tp": "טייק-פרופיט", "sl": "סטופ-לוס", "signal": "איתות יציאה", "time": "זמן מקסימלי", "manual": "ידני"}
        db.event(self.id, f"נסגרה {t['symbol']} ({he.get(reason, reason)}) @ {exit_px} | נטו {pnl:+.2f}$")

    def on_tick(self, symbol, ts_ms, price):
        tick = C.TICK.get(symbol, 0.01)
        for t in self.open_trades(symbol):
            if t["mode"] == "live":
                continue                      # exchange holds SL/TP; LiveBroker.sync() records the close
            s = t["side"]
            if (s > 0 and price <= t["sl"]) or (s < 0 and price >= t["sl"]):
                self.close(t, price, "sl")
            elif (s > 0 and price >= t["tp"] + tick) or (s < 0 and price <= t["tp"] - tick):
                self.close(t, price, "tp")
            elif time.time() - t["entry_ts"] > t["max_hold_min"] * 60:
                self.close(t, price, "time")

    def catch_up(self):
        """Settle open paper trades against 1-minute bars that arrived late (computer sleep,
        disconnects, server restart): same rules as the backtest - SL checked before TP in a
        minute, a gap through the stop fills at that minute's open, TP needs a trade-through."""
        for t in list(self.open_trades()):
            if t["mode"] == "live":
                continue
            sym = t["symbol"]; s = t["side"]; tick = C.TICK.get(sym, 0.01)
            start = pd.Timestamp(int(t["entry_ts"] * 1000) // 60000 * 60000 + 60000, unit="ms")
            start = max(start, self._checked.get(t["id"], start))
            bars = self.store.frame(sym).loc[start:]
            if not len(bars):
                continue
            limit = pd.Timestamp(int((t["entry_ts"] + t["max_hold_min"] * 60) * 1000), unit="ms")
            for ts, b in bars.iterrows():
                at = ts.timestamp() + 59
                if ts > limit:
                    self.close(t, b.open, "time", at_ts=ts.timestamp()); break
                if s > 0:
                    if b.open <= t["sl"]:
                        self.close(t, b.open, "sl", at_ts=ts.timestamp(), exit_override=b.open * (1 - C.SLIP)); break
                    if b.low <= t["sl"]:
                        self.close(t, b.low, "sl", at_ts=at); break
                    if b.high >= t["tp"] + tick:
                        self.close(t, b.high, "tp", at_ts=at); break
                else:
                    if b.open >= t["sl"]:
                        self.close(t, b.open, "sl", at_ts=ts.timestamp(), exit_override=b.open * (1 + C.SLIP)); break
                    if b.high >= t["sl"]:
                        self.close(t, b.high, "sl", at_ts=at); break
                    if b.low <= t["tp"] - tick:
                        self.close(t, b.low, "tp", at_ts=at); break
            else:
                self._checked[t["id"]] = bars.index[-1] + pd.Timedelta("1min")
                continue
            db.event(self.id, f"{sym}: העסקה הוסדרה בדיעבד לפי נרות דקה (המערכת לא הייתה מחוברת בזמן הפגיעה)")

    def snapshot(self):
        """Balance incl. unrealized P&L for the UI."""
        row = self.row; unreal = 0.0; opens = []
        for t in self.open_trades():
            px = self.store.last.get(t["symbol"], (0, t["entry_px"]))[1]
            hours = (time.time() - t["entry_ts"]) / 3600
            exit_fee = t["qty"] * px * C.TAKER
            u = t["side"] * t["qty"] * (px - t["entry_px"]) - t["fees"] - exit_fee - t["notional"] * C.FUNDING_8H * hours / 8
            unreal += u
            opens.append(dict(t, price=px, upnl=u, upnl_pct=100 * u / t["equity_before"]))
        return row, opens, unreal


class Engine:
    def __init__(self, store, broker_live=None):
        self.store = store
        self.bots = {b["id"]: Bot(b, store, broker_live) for b in BOTS}
        store.listeners_bar.append(self.on_bar)
        store.listeners_tick.append(self.on_tick)
        store.listeners_repair.append(lambda: [b.catch_up() for b in self.bots.values()])

    async def on_bar(self, symbol, minute_ts):
        for b in self.bots.values():
            await b.on_bar(symbol, minute_ts)

    def on_tick(self, symbol, ts, price):
        for b in self.bots.values():
            b.on_tick(symbol, ts, price)

    async def warm_start(self):
        """Evaluate every bot on the last closed bar once at start (no trades are opened from it:
        signals at start-up may be hours old; we only record the state)."""
        for b in self.bots.values():
            for s in C.SYMBOLS:
                df = self.store.frame(s)
                if not len(df):
                    continue
                tfm = TF_MIN[b.spec["tf"]]
                last = df.index[-1]
                boundary = last.floor(f"{tfm}min") - pd.Timedelta("1min")
                d = df.loc[:boundary]
                try:
                    if b.spec["strategy"] == "FreqAI_LGBM_Clf":
                        corr = None if s == "BTCUSDT" else self.store.frame("BTCUSDT").loc[:boundary].reindex(d.index).ffill()
                        r = await asyncio.to_thread(b.adapter.evaluate, d, corr, s)
                    else:
                        r = await asyncio.to_thread(b.adapter.evaluate, d)
                    b.last_eval[s] = dict(r, ts=time.time()) if r else dict(status="warmup", ts=time.time())
                except Exception as e:
                    log.exception("warm %s %s", b.id, s)
                    b.last_eval[s] = dict(status=f"error: {e}", ts=time.time())
