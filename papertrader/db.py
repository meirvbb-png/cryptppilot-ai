"""SQLite persistence for the paper-trading account (no real money, ever)."""
import os, sqlite3, threading, time

DB_PATH = os.environ.get("PT_DB", os.path.join(os.path.dirname(__file__), "data", "paper.db"))
START_BALANCE = 450.0
_lock = threading.RLock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        init(_conn)
    return _conn


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), balance REAL, start_balance REAL, created REAL);
    CREATE TABLE IF NOT EXISTS positions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, exchange TEXT, symbol TEXT, side TEXT,
      status TEXT, order_type TEXT, limit_price REAL,
      qty REAL, init_qty REAL, entry REAL, leverage REAL, margin REAL, sl REAL, tp1 REAL, tp2 REAL,
      tp1_hit INTEGER DEFAULT 0, liq REAL, realized REAL DEFAULT 0, fees REAL DEFAULT 0,
      opened REAL, closed REAL, close_price REAL, close_reason TEXT, last_check REAL, source TEXT);
    """)
    if not c.execute("SELECT 1 FROM account").fetchone():
        c.execute("INSERT INTO account VALUES(1,?,?,?)", (START_BALANCE, START_BALANCE, time.time()))
    c.commit()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def reset():
    with _lock:
        c = conn()
        c.execute("DELETE FROM positions")
        c.execute("UPDATE account SET balance=?, start_balance=?, created=?", (START_BALANCE, START_BALANCE, time.time()))
        c.commit()
