import asyncio, os, time, traceback
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import db, engine, exchange as ex, agents

HERE = os.path.dirname(__file__)
pool = ThreadPoolExecutor(8)
STATE = {"signals": [], "updated": None, "errors": {}, "marks": {}}


def refresh_signals():
    eq = engine.account()["equity"]
    sigs, errs = [], {}
    for e in ex.EXCHANGES:
        for s in ex.SYMBOLS:
            try:
                raw = ex.ohlcv(e, s, "15m", 300); htf = ex.ohlcv(e, s, "1h", 250)
                sig = agents.build_signal(e, s, raw, htf, eq)
                if sig: sig["ts"] = time.time(); sigs.append(sig)
            except Exception as err:
                errs[e] = str(err)[:200]
                break   # exchange unreachable -> skip its other symbols this round
    sigs.sort(key=lambda s: -s["confidence"])
    for i, s in enumerate(sigs): s["id"] = f"{s['exchange']}:{s['symbol']}:{int(s['ts'])}"
    STATE.update(signals=sigs, updated=time.time(), errors=errs)


async def loop(fn, every):
    lp = asyncio.get_running_loop()
    while True:
        try: await lp.run_in_executor(pool, fn)
        except Exception: traceback.print_exc()
        await asyncio.sleep(every)


@asynccontextmanager
async def lifespan(app):
    db.conn()
    tasks = [asyncio.create_task(loop(engine.tick, 3)), asyncio.create_task(loop(refresh_signals, 120))]
    yield
    for t in tasks: t.cancel()

app = FastAPI(lifespan=lifespan)


class Trade(BaseModel):
    exchange: str = "bybit"; symbol: str; side: str; margin: float; leverage: float
    sl: float | None = None; tp1: float | None = None; tp2: float | None = None
    order_type: str = "market"; limit_price: float | None = None; source: str = "manual"


@app.get("/api/config")
def config(): return dict(exchanges=ex.EXCHANGES, symbols=ex.SYMBOLS, mock=ex.MOCK)


@app.get("/api/state")
def state():
    positions = db.q("SELECT * FROM positions WHERE status IN ('open','pending') ORDER BY id DESC")
    for p in positions:
        try: p["mark"] = ex.last_price(p["exchange"], p["symbol"]); p["upnl"] = engine._pnl(p, p["mark"]) if p["status"] == "open" else 0
        except Exception: p["mark"] = None; p["upnl"] = 0
    acc = engine.account()
    return dict(account=acc, positions=positions, mock=ex.MOCK, errors=STATE["errors"])


@app.get("/api/signals")
def signals(): return dict(signals=STATE["signals"], updated=STATE["updated"], errors=STATE["errors"], mock=ex.MOCK)


@app.get("/api/ohlcv")
def ohlcv(exchange: str, symbol: str, tf: str = "15m"):
    if exchange not in ex.EXCHANGES or symbol not in ex.SYMBOLS or tf not in ex.TF_MS: raise HTTPException(400, "bad params")
    try: return ex.ohlcv(exchange, symbol, tf, 200)
    except Exception as e: raise HTTPException(502, f"שגיאת בורסה: {str(e)[:150]}")


@app.post("/api/trade")
def trade(t: Trade):
    if t.exchange not in ex.EXCHANGES or t.symbol not in ex.SYMBOLS: raise HTTPException(400, "בורסה/צמד לא נתמך")
    try:
        pid = engine.open_trade(t.exchange, t.symbol, t.side, t.margin, t.leverage, t.sl, t.tp1, t.tp2, t.order_type, t.limit_price, t.source)
    except ValueError as e: raise HTTPException(400, str(e))
    except Exception as e: raise HTTPException(502, f"שגיאת בורסה: {str(e)[:150]}")
    return {"id": pid}


@app.post("/api/close/{pid}")
def close(pid: int):
    try: engine.close_manual(pid)
    except ValueError as e: raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/history")
def history():
    rows = db.q("SELECT * FROM positions WHERE status IN ('closed','cancelled') ORDER BY closed DESC LIMIT 200")
    curve, bal = [], db.q("SELECT start_balance FROM account")[0]["start_balance"]
    for r in sorted((r for r in rows if r["status"] == "closed"), key=lambda r: r["closed"]):
        bal += r["realized"]; curve.append([r["closed"], bal])
    return dict(trades=rows, curve=curve)


@app.post("/api/reset")
def reset(): db.reset(); return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
