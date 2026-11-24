"""Engine tests: run with  DATA_DIR=<tmp dir> python -m pytest tests -q"""
import asyncio, os, tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import pandas as pd, pytest
from app import config as C, db
from app.bots import Bot
from app.strategies import BOTS


class FakeStore:
    def __init__(self):
        idx = pd.date_range("2026-01-01", periods=60 * 24 * 30, freq="1min")
        self.bars = {s: pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0,
                                      "quote_vol": 100.0, "buy_vol": 0.5, "trades": 1}, index=idx) for s in C.SYMBOLS}
        self.last = {}

    def frame(self, s, include_current=False):
        return self.bars[s]


def mk(bot_id="bot3"):
    spec = next(b for b in BOTS if b["id"] == bot_id)
    db.x("DELETE FROM trades WHERE bot_id=?", (bot_id,)); db.x("DELETE FROM bots WHERE id=?", (bot_id,))
    return Bot(spec, FakeStore())


def test_tp_close_net_of_fees():
    b = mk()
    b.open("SOLUSDT", dict(sig=1, sl_d=2.0, tp_d=3.0, hold_min=600), 100.0, "טרנד")
    t = b.open_trades()[0]
    assert t["sl"] < t["entry_px"] < t["tp"]
    b.on_tick("SOLUSDT", 0, t["tp"] + 0.005)            # touching TP is not enough (needs trade-through)
    assert b.open_trades()
    b.on_tick("SOLUSDT", 0, t["tp"] + 0.02)
    c = db.q("SELECT * FROM trades WHERE id=?", (t["id"],))[0]
    gross = t["qty"] * (t["tp"] - t["entry_px"])
    fees = t["qty"] * t["entry_px"] * C.TAKER + t["qty"] * t["tp"] * C.MAKER
    assert c["reason"] == "tp" and c["pnl_net"] == pytest.approx(gross - fees, abs=1e-3)
    assert b.row["equity"] == pytest.approx(500 + c["pnl_net"])


def test_sl_short():
    b = mk()
    b.open("ETHUSDT", dict(sig=-1, sl_d=20, tp_d=40, hold_min=600), 2000.0, "טרנד")
    t = b.open_trades()[0]
    assert t["tp"] < t["entry_px"] < t["sl"]
    b.on_tick("ETHUSDT", 0, t["sl"] + 0.5)
    c = db.q("SELECT * FROM trades WHERE id=?", (t["id"],))[0]
    assert c["reason"] == "sl" and c["pnl_net"] < 0
    assert abs(c["pnl_net"]) < 500 * 0.01 / 3 * 1.5        # ~ risk per trade (1% / 3 coins) + costs


def test_live_mode_blocked():
    b = mk()
    with pytest.raises(PermissionError):
        b.set_mode("live")


def test_bar_close_trigger_only_on_tf_boundary():
    b = mk("bot3")                                         # 1h strategy
    calls = []
    b.adapter.evaluate = lambda df: calls.append(df.index[-1]) or None
    asyncio.run(b.on_bar("BTCUSDT", pd.Timestamp("2026-01-10 10:30")))
    assert calls == []
    asyncio.run(b.on_bar("BTCUSDT", pd.Timestamp("2026-01-10 10:59")))
    assert calls == [pd.Timestamp("2026-01-10 10:59")]
