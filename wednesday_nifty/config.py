"""
Central configuration for the Wednesday NIFTY breakout credit-spread module.

Everything here is a constant or reads from environment variables (via a
local .env file, never committed). No other module in wednesday_nifty/
should hardcode a magic number that belongs here.
"""
import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IST = ZoneInfo("Asia/Kolkata")

# --- Kite Connect credentials -------------------------------------------------
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")

# Optional opt-in auto-login (off unless the user explicitly sets these).
AUTO_LOGIN_ENABLED = bool(os.environ.get("KITE_USER_ID")) and bool(
    os.environ.get("KITE_PASSWORD")
) and bool(os.environ.get("KITE_TOTP_SECRET"))
KITE_USER_ID = os.environ.get("KITE_USER_ID", "")
KITE_PASSWORD = os.environ.get("KITE_PASSWORD", "")
KITE_TOTP_SECRET = os.environ.get("KITE_TOTP_SECRET", "")

# --- Safety switch -------------------------------------------------------------
# Defaults to True on purpose: real orders require an explicit opt-out.
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")

# --- Market timing (all in IST, always compared against zoneinfo-aware clocks) --
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# Entry Logic 3 fallback check — updated from 2:30pm to 2:35pm.
FALLBACK_CHECK_TIME = time(14, 35)
EXPIRY_EXIT_TIME = time(15, 0)

# --- Strategy parameters ---------------------------------------------------
STRIKE_STEP = 50
LONG_LEG_OFFSET = 200
PROFIT_TARGET_PCT = 0.90
POSITION_LOTS = int(os.environ.get("POSITION_LOTS", "1"))  # Q1 default — fixed lots
ROLLOVER_PROXIMITY_DAYS = 2  # futures-expiry proximity guard (see instruments.py)

# Futures/spot basis sanity check (Q7) — warn always, hard-abort only if opted in.
BASIS_WARNING_THRESHOLD_POINTS = 50
BASIS_HARD_ABORT = os.environ.get("BASIS_HARD_ABORT", "false").strip().lower() in (
    "1", "true", "yes",
)

# --- Order execution (Q5) ---------------------------------------------------
ORDER_LIMIT_WAIT_SECONDS = 10
LEG_RETRY_COUNT = 3
LEG_RETRY_DELAY_SECONDS = 2
MARGIN_CHECK_ENABLED = True  # Q3 default: ON

# --- Polling / websocket ----------------------------------------------------
WS_RECONNECT_GRACE_SECONDS = 10
REST_FALLBACK_POLL_SECONDS = 5
EXIT_MONITOR_POLL_SECONDS = 300  # recommended manual re-run cadence (no scheduler wired up yet)

# --- Holiday handling --------------------------------------------------------
# If Wednesday AND the shifted Thursday are both NSE holidays, do not guess
# whether to push further (e.g. to Friday) — default is to skip the week
# entirely and flag it loudly. See holidays.py / report.
TWO_DAY_HOLIDAY_STRETCH_ACTION = "skip_week"

# --- File locations ----------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "wednesday_nifty.log"
SESSION_FILE = BASE_DIR / ".kite_session.json"
POSITIONS_FILE = BASE_DIR / "positions_wednesday_nifty.json"
WEEKLY_STATE_FILE = BASE_DIR / "weekly_state.json"
INSTRUMENTS_CACHE_DIR = BASE_DIR / "cache"
HOLIDAY_CACHE_FILE = INSTRUMENTS_CACHE_DIR / "nse_holidays_cache.json"
HOLIDAY_OVERRIDE_FILE = BASE_DIR / "nse_holidays_override.json"

LOG_DIR.mkdir(exist_ok=True)
INSTRUMENTS_CACHE_DIR.mkdir(exist_ok=True)
