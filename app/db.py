"""SQLite storage shared by the bots, the web API and the Meta-AI."""
import json, sqlite3, threading, time
from . import config as C

_lock = threading.RLock()
_con = sqlite3.connect(C.DB_PATH, check_same_thread=False)
_con.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(
  id TEXT PRIMARY KEY, name TEXT, strategy TEXT, mode TEXT, initial REAL, equity REAL,
  allocation REAL, risk REAL, paused INTEGER DEFAULT 0, settings TEXT, created REAL);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, symbol TEXT, side INTEGER, mode TEXT,
  status TEXT, entry_ts REAL, entry_px REAL, sl REAL, tp REAL, qty REAL, notional REAL,
  leverage REAL, max_hold_min INTEGER, exit_ts REAL, exit_px REAL, reason TEXT, fees REAL,
  funding REAL, pnl_net REAL, pnl_pct REAL, regime TEXT, hour INTEGER, equity_before REAL,
  order_id TEXT, info TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_bot ON trades(bot_id, status);
CREATE TABLE IF NOT EXISTS equity(bot_id TEXT, ts REAL, equity REAL);
CREATE TABLE IF NOT EXISTS events(ts REAL, bot_id TEXT, level TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS insights(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot_id TEXT,
  kind TEXT, severity TEXT, text TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot_id TEXT,
  kind TEXT, title TEXT, text TEXT, payload TEXT, status TEXT DEFAULT 'pending', decided_ts REAL);
"""
with _lock:
    _con.executescript(SCHEMA)
    _con.commit()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _con.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _con.execute(sql, args)
        _con.commit()
        return cur.lastrowid


def event(bot_id, text, level="info"):
    x("INSERT INTO events VALUES(?,?,?,?)", (time.time(), bot_id, level, text))


def jdump(o):
    return json.dumps(o, ensure_ascii=False)
