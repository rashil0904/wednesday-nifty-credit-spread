"""
Central configuration for the Wednesday 3-Day Breakout Credit Spread
strategy. Everything here is a constant or reads from environment
variables (via a local .env file, never committed). No other module
should hardcode a magic number that belongs here.

Anything marked CONFIRM BEFORE LIVE is a place where this depends on an
external fact (exchange rules, broker behavior) that should be
re-verified against live data before this trades real capital -- see
the project README's "Open items" section.
"""

import os
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IST = ZoneInfo("Asia/Kolkata")

# --- Entry/decision timing (IST) --------------------------------------------
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
ENTRY_DECISION_START = time(9, 15)
FALLBACK_TRIGGER_TIME = time(14, 35)  # off the clock, never off ticks

# --- Strike selection --------------------------------------------------------
HEDGE_OFFSET_POINTS = 200  # hedge leg = ATM +/- this many points, snapped to grid

# --- Expiry / exit ------------------------------------------------------------
# NSE Nifty weekly expiry weekday, date.weekday() numbering (Monday=0).
# CONFIRM BEFORE LIVE: re-verify this is still the live expiry weekday --
# NSE has changed it before (Thursday -> Tuesday, Sep 2025) and may again.
EXPIRY_WEEKDAY = 1  # Tuesday
EXIT_TIME = time(15, 15)
PROFIT_TARGET_PCT = 0.90

# Stop loss applies ONLY to HIGH_BREAKOUT entries (Put Credit Spread from
# a breakout above 3D_HIGH) -- SL level = the 3D_HIGH that was breached,
# fixed at entry. No stop loss exists, or should be added, for
# LOW_BREAKOUT / TIME_1435_RED / TIME_1435_GREEN entries. See
# strategy/stop_loss.py.
HIGH_BREAKOUT_SL_ENABLED = True

# --- Sizing -------------------------------------------------------------------
LOTS = 1  # fixed lots per trade; lot size itself comes from the instrument master

# --- NSE trading holidays -----------------------------------------------------
# No holiday calendar feed -- add dates here as they come up.
NSE_HOLIDAYS: set[date] = {
    date(2026, 10, 2),  # Gandhi Jayanti
}

# --- Order execution -----------------------------------------------------------
LIMIT_PRICE_BUFFER_PCTS = (0.03, 0.06, 0.10)  # increasing aggression per retry
# CONFIRM BEFORE LIVE: no futures-rollover-near-expiry guard exists elsewhere
# in this strategy (it trades options, not the future itself), but the
# signal reads the near-month FUTURE -- skip the week rather than risk
# reading a rolled/stale contract this close to its own expiry.
ROLLOVER_PROXIMITY_DAYS = 2

# --- Market-data streaming ------------------------------------------------------
USE_WEBSOCKET = True
STALE_TICK_SECONDS = 10
RECONNECT_MAX_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 5  # REST fallback poll cadence

# CONFIRM BEFORE LIVE -- best guess, unverified. Every verified REST
# market-data call on this account needed /apibinarymarketdata/ instead
# of Symphony's documented /apimarketdata/ (see broker/xts_client.py),
# so this defaults to the same binary-prefixed path. Run
# scripts/socket_smoke_test.py (optionally with --path to try the
# alternate) before trusting this.
JAINAM_SOCKET_PATH = os.environ.get("JAINAM_SOCKET_PATH", "/apibinarymarketdata/socket.io")

# --- Jainam (XTS) credentials --------------------------------------------------
JAINAM_BASE_URL = os.environ.get("JAINAM_BASE_URL", "")
JAINAM_MARKET_API_KEY = os.environ.get("JAINAM_MARKET_API_KEY", "")
JAINAM_MARKET_API_SECRET = os.environ.get("JAINAM_MARKET_API_SECRET", "")
JAINAM_INTERACTIVE_API_KEY = os.environ.get("JAINAM_INTERACTIVE_API_KEY", "")
JAINAM_INTERACTIVE_API_SECRET = os.environ.get("JAINAM_INTERACTIVE_API_SECRET", "")
JAINAM_SOURCE = os.environ.get("JAINAM_SOURCE", "WEBAPI")
JAINAM_CLIENT_ID = os.environ.get("JAINAM_CLIENT_ID", "")  # required for order placement, not for dry run

# --- Telegram notifications -------------------------------------------------------
# Every entry, exit, order failure, and skipped week sends a message here
# -- best-effort only (see notify.py): a Telegram outage never blocks or
# crashes the trading flow. Leave both unset to disable notifications
# entirely. See .env.example for how to create a bot and get a chat id.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Safety switch --------------------------------------------------------------
# Defaults to False on purpose: real orders require an explicit opt-in.
JAINAM_LIVE = os.environ.get("JAINAM_LIVE", "false").strip().lower() == "true"

# --- File locations ---------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "wednesday_nifty.log"
POSITIONS_FILE = BASE_DIR / "positions.json"
LOCK_DIR = BASE_DIR / "locks"
INSTRUMENTS_CACHE_DIR = BASE_DIR / "cache"

LOG_DIR.mkdir(exist_ok=True)
LOCK_DIR.mkdir(exist_ok=True)
INSTRUMENTS_CACHE_DIR.mkdir(exist_ok=True)
