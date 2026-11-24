"""Bybit v5 REST broker for LIVE mode (also works with Bybit's own demo account: BYBIT_ENV=demo).
Safety: refuses to run if the API key has withdrawal permission; one-way mode, market entry with
exchange-side Stop-Loss (market) and Take-Profit (limit) attached to the order."""
import hashlib, hmac, json, logging, time
import httpx
from . import config as C, db

log = logging.getLogger("live")


class LiveBroker:
    def __init__(self):
        self.base = C.REST_BASE[C.BYBIT_ENV]
        self.c = httpx.Client(base_url=self.base, timeout=15)
        self.ok = False; self.reason = ""

    def _sign(self, payload):
        ts = str(int(time.time() * 1000)); rw = "5000"
        sig = hmac.new(C.BYBIT_SECRET.encode(), (ts + C.BYBIT_KEY + rw + payload).encode(), hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": C.BYBIT_KEY, "X-BAPI-SIGN": sig, "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": rw, "Content-Type": "application/json"}

    def get(self, path, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        r = self.c.get(path + ("?" + qs if qs else ""), headers=self._sign(qs)).json()
        if r.get("retCode") != 0:
            raise RuntimeError(f"{path}: {r.get('retMsg')}")
        return r["result"]

    def post(self, path, body, ok_codes=(0,)):
        b = json.dumps(body)
        r = self.c.post(path, content=b, headers=self._sign(b)).json()
        if r.get("retCode") not in ok_codes:
            raise RuntimeError(f"{path}: {r.get('retCode')} {r.get('retMsg')}")
        return r.get("result", {})

    def check(self):
        """Verify keys and that the key CANNOT withdraw."""
        if not (C.LIVE_ENABLED and C.BYBIT_KEY and C.BYBIT_SECRET):
            self.reason = "כבוי (BYBIT_LIVE_ENABLED=false או חסרים מפתחות)"; return False
        try:
            info = self.get("/v5/user/query-api")
            perms = json.dumps(info.get("permissions", {}))
            if "Withdraw" in perms:
                self.reason = "המפתח כולל הרשאת משיכה - נחסם. צור מפתח בלי Withdraw."; return False
            if info.get("readOnly") == 1:
                self.reason = "המפתח לקריאה בלבד"; return False
            self.ok = True; self.reason = f"מחובר ({C.BYBIT_ENV})"
        except Exception as e:
            self.reason = f"שגיאת חיבור: {e}"
        return self.ok

    def open(self, symbol, side, qty, sl, tp, lev=5):
        if not self.ok:
            raise RuntimeError(self.reason)
        self.post("/v5/position/set-leverage", dict(category="linear", symbol=symbol, buyLeverage=str(lev),
                                                    sellLeverage=str(lev)), ok_codes=(0, 110043))
        r = self.post("/v5/order/create", dict(
            category="linear", symbol=symbol, side="Buy" if side > 0 else "Sell", orderType="Market",
            qty=f"{qty}", positionIdx=0, tpslMode="Partial", takeProfit=f"{tp}", stopLoss=f"{sl}",
            tpOrderType="Limit", tpLimitPrice=f"{tp}", slOrderType="Market", tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"))
        return r.get("orderId")

    def close(self, symbol, side, qty):
        self.post("/v5/order/create", dict(category="linear", symbol=symbol, side="Sell" if side > 0 else "Buy",
                                           orderType="Market", qty=f"{qty}", reduceOnly=True, positionIdx=0))

    def sync(self, engine):
        """Record live trades that the exchange closed (SL/TP hit)."""
        for bot in engine.bots.values():
            for t in bot.open_trades():
                if t["mode"] != "live":
                    continue
                try:
                    pos = self.get("/v5/position/list", category="linear", symbol=t["symbol"])["list"]
                    if pos and float(pos[0].get("size") or 0) > 0:
                        continue
                    cl = self.get("/v5/position/closed-pnl", category="linear", symbol=t["symbol"], limit=5)["list"]
                    cl = [c for c in cl if int(c["updatedTime"]) / 1000 >= t["entry_ts"] - 5]
                    if not cl:
                        continue
                    c = cl[0]
                    exit_px = float(c["avgExitPrice"]); pnl = float(c["closedPnl"])
                    reason = "tp" if (t["side"] > 0) == (exit_px >= t["entry_px"]) else "sl"
                    db.x("""UPDATE trades SET status='closed', exit_ts=?, exit_px=?, reason=?, pnl_net=?, pnl_pct=?
                            WHERE id=?""", (time.time(), exit_px, reason, pnl, 100 * pnl / t["equity_before"], t["id"]))
                    db.x("UPDATE bots SET equity=equity+? WHERE id=?", (pnl, bot.id))
                    db.x("INSERT INTO equity VALUES(?,?,?)", (bot.id, time.time(), bot.row["equity"]))
                    bot._reload()
                    db.event(bot.id, f"עסקה חיה נסגרה בבורסה {t['symbol']} @ {exit_px} | נטו {pnl:+.2f}$")
                except Exception as e:
                    log.warning("sync %s: %s", t["symbol"], e)
