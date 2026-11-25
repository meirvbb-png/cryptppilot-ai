"""Signal feed tests: dedupe, sizing for a 500 USDT / 1% account, live tracking, expiry, catch-up."""
import os, tempfile, time
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import pandas as pd, pytest
from app import db, signals
from tests.test_engine import mk, _sleep_bars


def _sig(side=1, sl_d=2.0, tp_d=3.0, hold=600):
    return dict(sig=side, sl_d=sl_d, tp_d=tp_d, hold_min=hold, exit_l=False, exit_s=False, price=100.0)


def setup_function(_):
    db.x("DELETE FROM signals")
    signals._active_cache["ts"] = 0


def test_record_sizing_and_dedupe():
    b = mk()
    s = signals.record(b, "SOLUSDT", _sig(), 100.0, "טרנד")
    assert s["entry"] == 100.0 and s["sl"] == 98.0 and s["tp"] == 103.0
    assert s["risk_usd"] == pytest.approx(5.0, abs=0.01)          # 1% of 500
    assert s["rr"] == pytest.approx(1.5)
    assert signals.record(b, "SOLUSDT", _sig(), 100.5, "טרנד") is None   # same idea still active
    assert signals.record(b, "SOLUSDT", _sig(side=-1), 100.5, "טרנד") is not None


def test_leverage_capped_at_5x():
    b = mk()
    s = signals.record(b, "ETHUSDT", _sig(sl_d=0.5, tp_d=1.0), 2000.0, "טרנד")   # tiny stop -> cap
    assert s["notional"] <= 500 * 5 + 1 and s["risk_usd"] < 5


def test_live_tracking_tp_sl_expiry():
    b = mk()
    signals.record(b, "SOLUSDT", _sig(), 100.0, "טרנד")
    signals.on_tick("SOLUSDT", 0, 103.0)                           # touch only, needs trade-through
    assert db.q("SELECT status FROM signals")[0]["status"] == "active"
    signals._active_cache["ts"] = 0
    signals.on_tick("SOLUSDT", 0, 103.05)
    r = db.q("SELECT * FROM signals")[0]
    assert r["status"] == "tp" and r["result_r"] == pytest.approx(1.5)
    signals.record(b, "SOLUSDT", _sig(side=-1), 100.0, "טרנד")
    signals._active_cache["ts"] = 0
    signals.on_tick("SOLUSDT", 0, 102.5)
    assert db.q("SELECT status, result_r FROM signals WHERE side=-1")[0] == {"status": "sl", "result_r": -1.0}
    s = signals.record(b, "ETHUSDT", _sig(hold=1), 2000.0, "טרנד")
    db.x("UPDATE signals SET expires_ts=? WHERE id=?", (time.time() - 1, s["id"]))
    signals._active_cache["ts"] = 0
    signals.on_tick("ETHUSDT", 0, 2001.0)
    assert db.q("SELECT status FROM signals WHERE id=?", (s["id"],))[0]["status"] == "expired"


def test_catch_up_after_sleep():
    b = mk()
    s = signals.record(b, "SOLUSDT", _sig(), 100.0, "טרנד")
    _sleep_bars(b, "SOLUSDT", s["ts"], [(100, 100.5, 99.5, 100), (100, 101, 97.5, 98)])   # stop hit in 2nd minute
    signals.catch_up(b.store)
    r = db.q("SELECT status, result_r FROM signals WHERE id=?", (s["id"],))[0]
    assert r == {"status": "sl", "result_r": -1.0}
    assert signals.summary()["total"] == 1
