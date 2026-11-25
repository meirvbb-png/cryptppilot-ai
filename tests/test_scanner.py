"""Scanner: watch-list alerts (kinds, cooldown, liquidity filter) and Bot-1 trade signals on scanner coins
(no stale signal at start, entry/SL/TP/size, live tracking, catch-up of coins without 1m history). Offline."""
import os, tempfile, time
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import numpy as np, pandas as pd, pytest
from app import db, signals, scan_signals as SS, scanner as SC, config as C


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    db.x("DELETE FROM signals"); db.x("DELETE FROM scan_alerts")
    signals._active_cache["ts"] = 0
    sent = []
    monkeypatch.setattr(signals, "telegram_send", lambda t: sent.append(t) or True)
    return sent


def row(sym, score=10, turn=5e6, tx=1.0, oi=0.0, flags=(), price=1.0):
    return dict(symbol=sym, base=sym[:-4], price=price, chg24=5.0, turnover=turn, turn_x=tx, oi=1e6, oi_chg=oi,
                funding=0.01, trending=None, reddit=0, days_listed=None, score=score, flags=list(flags))


def test_alert_kinds_cooldown_and_liquidity(_clean):
    sc = SC.Scanner(); sc.fresh = {"NEWUSDT"}
    rows = [row("AAAUSDT", 75), row("BBBUSDT", 30, tx=4.0), row("CCCUSDT", 20, oi=35),
            row("LOWUSDT", 40, turn=1e5, tx=9, oi=90), row("NEWUSDT", 15)]
    sc.alerts(rows, time.time())
    got = {(a["symbol"], a["kind"]) for a in SC.recent_alerts()}
    assert got == {("AAAUSDT", "score"), ("BBBUSDT", "volume"), ("CCCUSDT", "oi"), ("NEWUSDT", "new")}
    n = len(_clean)
    sc.alerts(rows, time.time())                       # same coins again inside the cooldown
    assert len(_clean) == n == 4


def test_alerts_capped_per_scan():
    sc = SC.Scanner()
    sc.alerts([row(f"C{i}USDT", 90) for i in range(20)], time.time())
    assert len(SC.recent_alerts(50)) == SC.ALERT_MAX_PER_SCAN


def _bars(n=400, breakout=False):
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    c = 100 + np.sin(np.arange(n) / 9.0) + np.arange(n) * 0.02
    if breakout:
        c[-1] = c.max() + 5
    return pd.DataFrame(dict(open=c, high=c + 0.5, low=c - 0.5, close=c, volume=1.0), index=idx)


def test_donchian_matches_bot1_on_breakout():
    assert SS.evaluate(_bars(100)) is None             # not enough history
    r = SS.evaluate(_bars(breakout=True))
    assert r["sig"] == 1 and r["sl_d"] > 0 and r["tp_d"] == pytest.approx(5 * r["sl_d"])   # 2 vs 10 ATR


def test_candidates_filter():
    sc = SS.ScanSignals()
    rows = [row("BTCUSDT", 90, 1e9), row("LOWUSDT", 90, 1e5), row("PUMPUSDT", 90, 1e8, flags=["זינוק חריג - סכנת פאמפ"]),
            row("QUIETUSDT", 0, 5e8), row("WEAKUSDT", 5, 1e8), row("GOODUSDT", 50, 1e8)]
    # no score gate by default: hottest first, then the most liquid; BTC (bots), illiquid and pumps excluded
    assert [r["symbol"] for r in sc.candidates(rows)] == ["GOODUSDT", "WEAKUSDT", "QUIETUSDT"]


def test_last_closed_bar_skips_refetch(monkeypatch):
    t = pd.Timestamp("2026-11-25 14:45").timestamp()
    assert SS.ScanSignals.last_closed_bar(t) == "2026-11-25 08:00:00"   # 12:00 bar is still forming
    sc = SS.ScanSignals(); calls = []
    monkeypatch.setattr(SS, "klines_4h", lambda s: calls.append(s) or _bars())
    monkeypatch.setattr(SS.ScanSignals, "last_closed_bar", staticmethod(lambda now=None: str(_bars().index[-1])))
    rows = [row("GOODUSDT", 50, 1e8)]
    sc.run(rows, lambda s: 1.0); sc.run(rows, lambda s: 1.0)
    assert calls == ["GOODUSDT"]                       # second scan inside the same 4h bar: no request


def test_no_stale_signal_then_new_bar_signal_and_tracking(monkeypatch):
    sc = SS.ScanSignals(); rows = [row("GOODUSDT", 50, 1e8, price=2.0)]
    df = {"v": _bars(breakout=True)}
    monkeypatch.setattr(SS, "klines_4h", lambda s: df["v"])
    assert sc.run(rows, lambda s: 2.0) == []           # breakout already there at start -> ignored
    nxt = _bars(401, breakout=True); df["v"] = nxt     # a new 4h bar closes with a breakout
    new = sc.run(rows, lambda s: float(nxt.close.iloc[-1]))
    assert len(new) == 1
    s = new[0]
    assert s["bot_id"] == "scan" and s["side"] == 1 and s["sl"] < s["entry"] < s["tp"]
    assert s["risk_usd"] == pytest.approx(5.0, abs=0.05)
    assert sc.run(rows, lambda s: 2.0) == []           # same bar again -> nothing
    signals.on_tick("GOODUSDT", 0, s["tp"] + 1)
    assert db.q("SELECT status FROM signals WHERE id=?", (s["id"],))[0]["status"] == "tp"


def test_catch_up_skips_coins_without_history():
    class Store: bars = {"BTCUSDT": None}
    now = time.time()
    for sym, exp in (("OLDUSDT", now - 10), ("LIVEUSDT", now + 3600)):
        db.x("INSERT INTO signals(ts,bot_id,bot_name,verdict,symbol,side,entry,sl,tp,rr,hold_min,expires_ts,qty,"
             "notional,leverage,risk_usd,status) VALUES(?,?,?,?,?,1,1,0.9,1.5,5,1,?,1,1,1,0.1,'active')",
             (now - 99, "scan", "n", "v", sym, exp))
    signals.catch_up(Store())
    st = {r["symbol"]: r["status"] for r in db.q("SELECT symbol,status FROM signals")}
    assert st == {"OLDUSDT": "expired", "LIVEUSDT": "active"}


def test_tick_fallback_and_dotenv_utf8(tmp_path, monkeypatch):
    assert SS.tick_for(0.00000612) == pytest.approx(1e-10) and SS.tick_for(1.23) == pytest.approx(1e-4)
    p = tmp_path / ".env"; p.write_bytes("﻿# הערה בעברית\nZZ_TEST_KEY=שלום\n".encode("utf-8"))
    monkeypatch.setattr(C, "ROOT", tmp_path); monkeypatch.delenv("ZZ_TEST_KEY", raising=False)
    C._load_dotenv()
    assert os.environ["ZZ_TEST_KEY"] == "שלום"
