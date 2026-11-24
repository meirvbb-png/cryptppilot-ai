"""Market-data tests. The REST mock reproduces the real Bybit v5 kline payload
(verified against api.bybit.com): newest-first list of string arrays, max 1000 per call."""
import os, tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import httpx, numpy as np, pandas as pd
from app import market as M


def bybit_mock(calls):
    def handler(req):
        p = dict(req.url.params); calls.append(p)
        a, b, lim = int(p["start"]), int(p["end"]), int(p["limit"])
        ts = list(range(a - a % 60000, b + 1, 60000))[:lim][::-1]          # newest first, like Bybit
        rows = [[str(t), "100.5", "101", "99", "100", "2.5", "250"] for t in ts]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"symbol": p["symbol"],
                              "category": "linear", "list": rows}, "time": 0})
    return httpx.Client(base_url="https://api.bybit.com", transport=httpx.MockTransport(handler))


def test_rest_fill_paginates_and_fills_only_missing():
    idx = pd.date_range("2026-11-20", periods=3000, freq="1min")
    df = pd.DataFrame(1.0, index=idx, columns=M.COLS).drop(idx[100:2600])       # 2500 missing minutes
    calls = []
    out = M._rest_fill("BTCUSDT", df, idx[0], idx[-1] + pd.Timedelta("1min"), client=bybit_mock(calls))
    assert len(out) == 2500 and out.index.equals(idx[100:2600])
    assert len(calls) == 3                                                     # 1000 + 1000 + 500
    assert np.isnan(out.buy_vol).all() and (out.close == 100).all()
    merged = M._merge([df, out])
    assert merged.index.is_monotonic_increasing and len(merged) == 3000


def test_partial_first_bar_after_connect_is_dropped():
    st = M.BarStore(["BTCUSDT"])
    t0 = pd.Timestamp("2026-11-24 23:01").value // 10**6
    st.connected_min = pd.Timestamp("2026-11-24 23:01")
    st.on_trade("BTCUSDT", t0 + 47_000, 100.0, 1.0, True)       # joined at 23:01:47 -> partial
    st.on_trade("BTCUSDT", t0 + 61_000, 101.0, 1.0, False)      # 23:02 -> closes 23:01 bar
    st.on_trade("BTCUSDT", t0 + 121_000, 102.0, 1.0, True)      # 23:03 -> closes 23:02 bar
    assert list(st.bars["BTCUSDT"].index) == [pd.Timestamp("2026-11-24 23:02")]


def test_bootstrap_keeps_live_bars():
    live = pd.DataFrame(2.0, index=pd.date_range("2026-11-24 23:02", periods=3, freq="1min"), columns=M.COLS)
    hist = pd.DataFrame(1.0, index=pd.date_range("2026-11-24 22:00", periods=62, freq="1min"), columns=M.COLS)
    m = M._merge([live, hist])
    assert len(m) == 65 and m.loc["2026-11-24 23:03", "close"] == 2.0
