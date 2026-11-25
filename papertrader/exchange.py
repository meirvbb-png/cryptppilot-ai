"""Public market data only (ccxt, no API keys). PT_MOCK=1 gives a clearly-labelled offline simulator for tests."""
import os, time, math, random, threading
import ccxt

MOCK = os.environ.get("PT_MOCK") == "1"
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT", "BNB/USDT:USDT"]
EXCHANGES = ["bybit", "mexc"]
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
_inst = {}
_lock = threading.Lock()


def _ex(name):
    with _lock:
        if name not in _inst:
            _inst[name] = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 10000, "options": {"defaultType": "swap"}})
        return _inst[name]


def _mock_series(symbol, tf, limit):
    live = _mock_full(symbol, "15m")[-1][4]          # one shared live price per symbol
    full = _mock_full(symbol, tf)
    k = live / full[-1][4]
    return [[t, o * k, h * k, l * k, c * k, v] for t, o, h, l, c, v in full[-(limit + 1):]]


def _mock_full(symbol, tf, limit=400):
    base = {"BTC": 95000, "ETH": 3400, "SOL": 210, "XRP": 2.4, "DOGE": 0.38, "BNB": 700}.get(symbol.split("/")[0], 100)
    ms = TF_MS[tf]
    now = int(time.time() * 1000) // ms * ms
    rnd = random.Random(sum(map(ord, symbol + tf)))
    trend = [1, -1, 1, -1, 1, 1][SYMBOLS.index(symbol) % 6] if symbol in SYMBOLS else 0
    p, out = base, []
    for i in range(limit + 1):
        t = now - (limit - i) * ms
        drift = trend * 0.0012 + math.sin(i / 40) * 0.0004
        o = p
        c = o * (1 + drift + rnd.gauss(0, 0.002))
        h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.001))), min(o, c) * (1 - abs(rnd.gauss(0, 0.001)))
        out.append([t, o, h, l, c, rnd.uniform(100, 1000)])
        p = c
    # live candle wobbles slightly around a stable level so prices "move" consistently across calls
    w = 1 + math.sin(time.time() / 7) * 0.0003
    out[-1][4] *= w; out[-1][2] = max(out[-1][2], out[-1][4]); out[-1][3] = min(out[-1][3], out[-1][4])
    return out


def ohlcv(exchange, symbol, tf="15m", limit=300):
    if MOCK:
        return _mock_series(symbol, tf, limit)
    return _ex(exchange).fetch_ohlcv(symbol, tf, limit=limit)


def last_price(exchange, symbol):
    if MOCK:
        return _mock_series(symbol, "1m", 3)[-1][4]
    return float(_ex(exchange).fetch_ticker(symbol)["last"])


def orderbook(exchange, symbol, depth=10):
    if MOCK:
        p = last_price(exchange, symbol)
        return {"bids": [[p * (1 - 0.0002 * i), 1] for i in range(1, depth + 1)], "asks": [[p * (1 + 0.0002 * i), 1] for i in range(1, depth + 1)]}
    ob = _ex(exchange).fetch_order_book(symbol, depth)
    return {"bids": ob["bids"][:depth], "asks": ob["asks"][:depth]}
