"""FastAPI server: JSON API + the mobile web app. Listens on 0.0.0.0 (see README for phone access)."""
import asyncio, logging, time
from contextlib import asynccontextmanager
from pathlib import Path
import numpy as np, pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from . import config as C, db
from .market import BarStore
from .bots import Engine, MODE_HE
from .brain import Brain
from .broker_live import LiveBroker
from .regime import regime_detail
from . import backup
from research.bt.data import resample

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")
STATIC = Path(__file__).parent / "static"
S = {}


async def _periodic():
    while True:
        await asyncio.sleep(300)
        await asyncio.to_thread(S["store"].save)


async def _live_sync():
    while True:
        await asyncio.sleep(15)
        if S["live"].ok:
            await asyncio.to_thread(S["live"].sync, S["engine"])


@asynccontextmanager
async def lifespan(app):
    store = BarStore(C.SYMBOLS); S["store"] = store
    live = LiveBroker(); S["live"] = live
    tasks = [asyncio.create_task(store.run_ws()), asyncio.create_task(store.flush_minutes()),
             asyncio.create_task(store.run_repair())]
    await asyncio.to_thread(store.bootstrap)
    await asyncio.to_thread(live.check)
    engine = Engine(store, live); S["engine"] = engine
    brain = Brain(store, engine); S["brain"] = brain
    await engine.warm_start()
    for b in engine.bots.values():
        b.catch_up()                     # settle trades that hit SL/TP while the app was off
    tasks += [asyncio.create_task(brain.run()), asyncio.create_task(_periodic()), asyncio.create_task(_live_sync()),
              asyncio.create_task(backup.loop(store))]
    log.info("ready on http://%s:%s", C.HOST, C.PORT)
    yield
    for t in tasks:
        t.cancel()
    store.save()
    await asyncio.to_thread(backup.upload)


app = FastAPI(lifespan=lifespan, title="CryptoPilot Bots")


@app.middleware("http")
async def auth(request: Request, call_next):
    if C.APP_TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/ping":
        tok = request.headers.get("x-token") or request.cookies.get("token") or request.query_params.get("token")
        if tok != C.APP_TOKEN:
            return JSONResponse({"error": "נדרש קוד גישה"}, status_code=401)
    if "engine" not in S and request.url.path.startswith("/api") and request.url.path != "/api/ping":
        return JSONResponse({"error": "המערכת נטענת (מוריד היסטוריה)…", "loading": True,
                             "msg": S.get("store").status if S.get("store") else {}}, status_code=503)
    return await call_next(request)


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 6)
    if isinstance(o, np.integer):
        return int(o)
    return o


def _bot_json(b):
    row, opens, unreal = b.snapshot()
    closed = db.q("SELECT COUNT(*) n, COALESCE(SUM(pnl_net),0) pnl, COALESCE(SUM(pnl_net>0),0) w FROM trades "
                  "WHERE bot_id=? AND status='closed'", (b.id,))[0]
    eq = row["equity"] + unreal
    return dict(id=b.id, name=row["name"], strategy=row["strategy"], tf=b.spec["tf"], desc=b.spec["desc"],
                mode=row["mode"], mode_he=MODE_HE[row["mode"]], paused=bool(row["paused"]),
                allocation=row["allocation"], realized=row["equity"], equity=eq, unrealized=unreal,
                pnl=eq - row["allocation"], pnl_pct=100 * (eq / row["allocation"] - 1), risk=row["risk"],
                closed=closed["n"], wins=closed["w"], bench=b.spec["bench"], open=opens,
                settings=b.settings, last_eval={s: {k: v for k, v in e.items() if k != "info" or True}
                                                for s, e in b.last_eval.items()})


@app.get("/api/ping")
def ping():
    st = S.get("store")
    return dict(ok=True, ready="engine" in S, status=st.status if st else {})


@app.get("/api/overview")
def overview():
    st = S["store"]
    bots = [_bot_json(b) for b in S["engine"].bots.values()]
    prices = {s: st.last.get(s, (None, float(st.bars[s].close.iloc[-1]) if len(st.bars[s]) else None))[1] for s in C.SYMBOLS}
    tot_alloc = sum(b["allocation"] for b in bots); tot_eq = sum(b["equity"] for b in bots)
    return _clean(dict(bots=bots, prices=prices, status=st.status, live=dict(ok=S["live"].ok, reason=S["live"].reason),
                       backup=backup.STATUS,
                       total=dict(allocation=tot_alloc, equity=tot_eq, pnl=tot_eq - tot_alloc),
                       regimes=S["brain"].state.get("regimes", {}), server_time=time.time()))


@app.get("/api/bots/{bot_id}")
def bot(bot_id: str):
    b = S["engine"].bots.get(bot_id) or _404()
    d = _bot_json(b)
    d["trades"] = db.q("SELECT * FROM trades WHERE bot_id=? AND status='closed' ORDER BY exit_ts DESC LIMIT 200", (bot_id,))
    st = S["brain"].state.get("stats", {}).get(bot_id, {})
    d["live_stats"] = dict(pf=st.get("all", {}).get("pf"), max_dd=st.get("max_dd"))
    d["equity_curve"] = db.q("SELECT ts, equity FROM equity WHERE bot_id=? ORDER BY ts", (bot_id,))
    d["events"] = db.q("SELECT * FROM events WHERE bot_id=? ORDER BY ts DESC LIMIT 50", (bot_id,))
    return _clean(d)


@app.get("/api/trades")
def trades(status: str = "all", limit: int = 200):
    w = "" if status == "all" else "WHERE status=?"
    args = () if status == "all" else (status,)
    rows = db.q(f"SELECT * FROM trades {w} ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT {int(limit)}", args)
    eng = S["engine"]; st = S["store"]
    for r in rows:
        r["bot_name"] = eng.bots[r["bot_id"]].spec["name"]
        if r["status"] == "open":
            px = st.last.get(r["symbol"], (0, r["entry_px"]))[1]; r["price"] = px
            u = r["side"] * r["qty"] * (px - r["entry_px"]) - r["fees"] - r["qty"] * px * C.TAKER
            r["upnl"] = u; r["upnl_pct"] = 100 * u / r["equity_before"]
    return _clean(rows)


@app.get("/api/candles")
def candles(symbol: str = "BTCUSDT", tf: str = "1h", limit: int = 300):
    st = S["store"]
    if symbol not in st.bars:
        _404()
    tfm = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}.get(tf, "1h")
    mins = pd.Timedelta(tfm).total_seconds() / 60
    df = st.frame(symbol, include_current=True).iloc[-int(limit * mins) - 1:]
    if tfm != "1min":
        df = resample(df, tfm)
    df = df.iloc[-limit:]
    out = [dict(time=int(t.timestamp()), open=r.open, high=r.high, low=r.low, close=r.close)
           for t, r in zip(df.index, df.itertuples())]
    return _clean(out)


@app.post("/api/bots/{bot_id}/mode")
async def set_mode(bot_id: str, request: Request):
    b = S["engine"].bots.get(bot_id) or _404()
    body = await request.json()
    try:
        b.set_mode(body.get("mode"))
    except (PermissionError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/bots/{bot_id}/pause")
async def pause(bot_id: str, request: Request):
    b = S["engine"].bots.get(bot_id) or _404()
    body = await request.json(); p = 1 if body.get("paused") else 0
    db.x("UPDATE bots SET paused=? WHERE id=?", (p, bot_id))
    db.event(bot_id, "הבוט הושהה" if p else "הבוט הופעל")
    return {"ok": True}


@app.post("/api/trades/{tid}/close")
def close_trade(tid: int):
    t = db.q("SELECT * FROM trades WHERE id=? AND status='open'", (tid,))
    if not t:
        _404()
    t = t[0]; b = S["engine"].bots[t["bot_id"]]
    px = S["store"].last.get(t["symbol"], (0, t["entry_px"]))[1]
    b.close(t, px, "manual")
    return {"ok": True}


@app.get("/api/ai")
def ai():
    br = S["brain"]
    return _clean(dict(state=br.state,
                       insights=db.q("SELECT * FROM insights ORDER BY CASE severity WHEN 'high' THEN 0 ELSE 1 END, ts DESC"),
                       proposals=db.q("SELECT * FROM proposals ORDER BY status='pending' DESC, ts DESC LIMIT 50"),
                       names={k: b.spec["name"] for k, b in S["engine"].bots.items()}))


@app.post("/api/ai/refresh")
async def ai_refresh():
    await asyncio.to_thread(S["brain"].evaluate)
    return {"ok": True}


@app.post("/api/proposals/{pid}/{action}")
def decide(pid: int, action: str):
    if action not in ("approve", "reject"):
        _404()
    try:
        S["brain"].decide(pid, action == "approve")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/events")
def events(limit: int = 100):
    return db.q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))


def _404():
    raise HTTPException(404, "not found")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": "CryptoPilot בוטים", "short_name": "בוטים", "start_url": "/", "display": "standalone",
                         "background_color": "#0b0f17", "theme_color": "#0b0f17", "dir": "rtl", "lang": "he",
                         "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})
