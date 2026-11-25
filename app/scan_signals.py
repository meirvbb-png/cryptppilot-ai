"""Trade signals for the scanner's top coins (demo / manual use only - nothing is ever ordered).

Runs Bot 1's exact Donchian breakout rule (research/bt/strategies.donchian, same params) on 4h candles
fetched from Bybit's public REST for the highest-scoring liquid coins. A breakout on the last CLOSED
4h bar becomes a signal through signals.record(): entry, stop-loss, take-profit and a suggested size
for a $500 account risking 1%. It is then tracked against live prices like every other signal.

The strategy was only tested on BTC/ETH/SOL - on altcoins it is untested, and every signal says so."""
import logging, math, time
import httpx, pandas as pd
from research.bt import strategies as RS
from . import config as C, signals
from .strategies import BOTS

log = logging.getLogger("scan_signals")
BOT1 = next(b for b in BOTS if b["id"] == "bot1")
TOP_N = int(C.env("SCAN_SIG_TOP", "30"))                 # how many scanner coins get the strategy
# 0 = no score gate: a trend breakout needs liquid coins, not hype (in a calm market almost every score is < 30)
MIN_SCORE = float(C.env("SCAN_SIG_MIN_SCORE", "0"))
BAR_S = 4 * 3600
MIN_TURNOVER = float(C.env("SCAN_SIG_MIN_TURNOVER", "5000000"))
SKIP = set(C.SYMBOLS)                                    # the 4 bots already cover these


class _ScanBot:
    """The minimal 'bot' interface signals.record() needs."""
    id = "scan"
    spec = dict(name="🔭 סורק · פריצת מגמה", bench=dict(verdict="לא נבדק על אלטים"))


BOT = _ScanBot()


def tick_for(price):
    """Fallback price step when Bybit's instrument list is unreachable: ~5 significant digits."""
    if price <= 0:
        return 0.01
    return 10 ** (math.floor(math.log10(price)) - 4)


def klines_4h(symbol, limit=400):
    r = httpx.get(f"{C.REST_BASE['mainnet']}/v5/market/kline", timeout=20,
                  params=dict(category="linear", symbol=symbol, interval="240", limit=limit))
    j = r.json()
    rows = (j.get("result") or {}).get("list") or []     # newest first; [0] is the bar still forming
    if j.get("retCode") not in (0, None) or len(rows) < 2:
        return None
    df = pd.DataFrame([[int(x[0]), *map(float, x[1:6])] for x in rows[1:]],
                      columns=["t", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    return df.sort_index()


def evaluate(df):
    """Bot 1's rule on the last closed 4h bar -> same dict shape the bots pass to signals.record()."""
    if df is None or len(df) < 250:
        return None
    a = RS.donchian(df, "4h", **BOT1["params"])
    return dict(sig=int(a["sig"][-1]), sl_d=float(a["sl_d"][-1]), tp_d=float(a["tp_d"][-1]),
                hold_min=int(a["hold"][-1]), bar_time=str(df.index[-1]))


class ScanSignals:
    def __init__(self):
        self.seen = {}                                   # symbol -> last evaluated bar time
        self.last = {}                                   # symbol -> last result (for the UI)

    def candidates(self, rows):
        ok = [r for r in rows if r["symbol"] not in SKIP and r["turnover"] >= MIN_TURNOVER
              and r["score"] >= MIN_SCORE and not any("פאמפ" in f for f in r["flags"])]
        ok.sort(key=lambda r: (-r["score"], -r["turnover"]))   # hottest first, then the most liquid
        return ok[:TOP_N]

    @staticmethod
    def last_closed_bar(now=None):
        now = time.time() if now is None else now
        return str(pd.Timestamp(int(now // BAR_S * BAR_S - BAR_S), unit="s"))

    def run(self, rows, live_price):
        new = []
        closed = self.last_closed_bar()
        for row in self.candidates(rows):
            s = row["symbol"]
            if self.seen.get(s) == closed:
                continue                                 # no new 4h bar since the last check -> no request
            try:
                df = klines_4h(s)
                r = evaluate(df)
            except Exception as e:
                log.warning("%s: %s", s, e); continue
            if r is None:
                self.last[s] = dict(status="warmup"); continue
            self.last[s] = dict(status="ok", sig=r["sig"], bar_time=r["bar_time"], ts=time.time())
            if self.seen.get(s) == r["bar_time"]:
                continue                                 # this bar was already handled
            first = s not in self.seen
            self.seen[s] = r["bar_time"]
            if first or r["sig"] == 0:
                continue                                 # never act on a signal that was old when we started
            price = live_price(s) or row["price"]
            C.TICK.setdefault(s, tick_for(price))
            rec = signals.record(BOT, s, r, price, f"סורק · ציון {row['score']}")
            if rec:
                new.append(rec)
                log.info("scanner signal %s %s @ %s", s, "long" if r["sig"] > 0 else "short", rec["entry"])
        return new
