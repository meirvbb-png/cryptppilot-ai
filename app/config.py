"""Settings (from .env / environment). Secrets are read from the environment only."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(k, default=None):
    return os.environ.get(k, default)


SYMBOLS = [s.strip() for s in env("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")]
TICK = {"BTCUSDT": 0.1, "ETHUSDT": 0.01, "SOLUSDT": 0.01}
DATA_DIR = Path(env("DATA_DIR", ROOT / "app_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bots.db"
# optional local history (the research archive) used as a cache
HIST_DIR = Path(env("HIST_DIR", ROOT / "research" / "data" / "raw1m"))
HISTORY_DAYS = int(env("HISTORY_DAYS", "130"))      # needed by FreqAI (90d train + warm-up)
ARCHIVE_DAYS = int(env("ARCHIVE_DAYS", "7"))        # days of tick archives (taker buy/sell volume)

HOST = env("HOST", "0.0.0.0")
PORT = int(env("PORT", "8000"))
APP_TOKEN = env("APP_TOKEN", "")                    # optional access code for the web app

# Costs (Bybit non-VIP USDT perpetual) - same as the backtest
TAKER = float(env("FEE_TAKER", "0.00055"))
MAKER = float(env("FEE_MAKER", "0.0002"))
SLIP = float(env("SLIPPAGE", "0.0002"))
FUNDING_8H = float(env("FUNDING_8H", "0.0001"))

# Live trading is OFF unless explicitly enabled AND keys are present
LIVE_ENABLED = env("BYBIT_LIVE_ENABLED", "false").lower() == "true"
BYBIT_ENV = env("BYBIT_ENV", "demo")                # demo | mainnet | testnet
BYBIT_KEY = env("BYBIT_API_KEY", "")
BYBIT_SECRET = env("BYBIT_API_SECRET", "")
REST_BASE = {"mainnet": "https://api.bybit.com", "demo": "https://api-demo.bybit.com",
             "testnet": "https://api-testnet.bybit.com"}
WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
