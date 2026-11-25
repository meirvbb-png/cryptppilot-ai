import os, sys, time, tempfile
os.environ["PT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["PT_MOCK"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
import db, engine, exchange as ex, agents

PRICE = {"p": 100.0}
CANDLES = {"c": []}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    db.reset()
    PRICE["p"] = 100.0
    CANDLES["c"] = []
    monkeypatch.setattr(ex, "last_price", lambda e, s: PRICE["p"])
    monkeypatch.setattr(ex, "orderbook", lambda e, s, d=5: {"bids": [[PRICE["p"], 1]], "asks": [[PRICE["p"], 1]]})
    monkeypatch.setattr(ex, "ohlcv", lambda e, s, tf="15m", limit=300: CANDLES["c"] or [[int(time.time() * 1000) // 60000 * 60000, PRICE["p"], PRICE["p"], PRICE["p"], PRICE["p"], 1]])


def candle(hi, lo, c=None):
    t = (int(time.time() * 1000) // 60000 + 1) * 60000   # next minute: strictly after entry
    CANDLES["c"] = [[t, PRICE["p"], hi, lo, c or hi, 1]]
    db.x("UPDATE positions SET last_check=last_check-120")  # make the candle count as "since last check"


def test_start_balance():
    assert engine.account()["equity"] == 450


def test_long_tp_win_and_fees():
    pid = engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 5, 98, 104)   # notional 500, qty 5
    a = engine.account()
    assert abs(a["balance"] - (450 - 500 * 0.00055)) < 1e-6
    candle(105, 100)
    engine.tick()
    p = db.q("SELECT * FROM positions WHERE id=?", (pid,))[0]
    assert p["status"] == "closed" and p["close_reason"] == "TP" and p["close_price"] == 104
    expected = 450 + 5 * 4 - 500 * 0.00055 - 104 * 5 * 0.00055
    assert abs(engine.account()["balance"] - expected) < 1e-6
    assert engine.account()["win_rate"] == 100


def test_short_sl_loss():
    pid = engine.open_trade("mexc", "ETH/USDT:USDT", "SHORT", 50, 10, 102, 95)
    candle(103, 99)
    engine.tick()
    p = db.q("SELECT * FROM positions WHERE id=?", (pid,))[0]
    assert p["close_reason"] == "SL" and p["realized"] < 0
    assert engine.account()["win_rate"] == 0


def test_sl_priority_when_both_touched():
    pid = engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 5, 98, 104)
    candle(110, 90)
    engine.tick()
    assert db.q("SELECT close_reason FROM positions")[0]["close_reason"] == "SL"


def test_tp1_partial_then_breakeven():
    pid = engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 5, 98, 102, 106)
    candle(103, 100)
    engine.tick()
    p = db.q("SELECT * FROM positions")[0]
    assert p["status"] == "open" and p["tp1_hit"] == 1 and abs(p["qty"] - 2.5) < 1e-9 and p["sl"] == 100
    candle(101, 99)
    engine.tick()
    p = db.q("SELECT * FROM positions")[0]
    assert p["status"] == "closed" and p["close_reason"] == "SL-BE"


def test_limit_order_fills():
    pid = engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 5, 94, 110, order_type="limit", limit_price=95)
    assert db.q("SELECT status FROM positions")[0]["status"] == "pending"
    candle(101, 94.5)
    engine.tick()
    assert db.q("SELECT status FROM positions")[0]["status"] == "open"


def test_validation():
    with pytest.raises(ValueError): engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 5, 105, 110)  # SL above entry
    with pytest.raises(ValueError): engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 1000, 5, 90, 110)  # too big
    with pytest.raises(ValueError): engine.open_trade("bybit", "BTC/USDT:USDT", "SHORT", 100, 5, 110, 120)  # TP wrong side


def test_liquidation():
    pid = engine.open_trade("bybit", "BTC/USDT:USDT", "LONG", 100, 20, None, None)
    candle(100, 90)
    engine.tick()
    p = db.q("SELECT * FROM positions")[0]
    assert p["close_reason"] == "LIQUIDATION" and p["realized"] < -89


def test_agents_pipeline_and_risk_bounds():
    raw = ex._mock_series("BTC/USDT:USDT", "15m", 300)
    htf = ex._mock_series("BTC/USDT:USDT", "1h", 250)
    found = 0
    for sym in ex.SYMBOLS:
        for phase in range(40):
            r = ex._mock_series(sym, "15m", 300 + phase); h = ex._mock_series(sym, "1h", 250 + phase)
            s = agents.build_signal("bybit", sym, r[:300 + phase - phase // 2], h, 450)
            if not s: continue
            found += 1
            assert s["side"] in ("LONG", "SHORT")
            d = 1 if s["side"] == "LONG" else -1
            assert d * (s["tp1"] - s["entry"]) > 0 and d * (s["tp2"] - s["tp1"]) > 0 and d * (s["entry"] - s["sl"]) > 0
            assert 0.015 - 1e-9 <= s["risk_pct"] <= 0.02 + 1e-9
            assert s["risk_usd"] <= 450 * 0.02 + 1e-6 and s["margin"] <= 450
            assert len(s["discussion"]) == 3
    assert found > 0
