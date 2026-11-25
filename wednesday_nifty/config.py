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
# The SL is NOT tick-level: it's evaluated once a day, off the futures
# price at this time -- below sl_level at 15:28 -> exit (user rule,
# 2026-11-04). Applies on the entry day and every later day.
SL_CHECK_TIME = time(15, 28)

# Positions with NO stop loss (call spreads: LOW_BREAKOUT / TIME_1435_RED)
# don't poll quotes all day: the profit target is checked ONCE a day at
# this time (user rule, 2026-11-19, after e-session-0007 re-login churn
# from all-day quote polling). Set to 15:28 not 15:30 because the poll
# loop ends at MARKET_CLOSE (15:30) and orders can't fill at the bell.
# The time exit is off the clock and needs no quotes.
TARGET_CHECK_TIME = time(15, 28)

# --- Sizing -------------------------------------------------------------------
LOTS = 4  # fixed lots per trade, both legs (confirmed by user, 2026-11-01; previously 1); lot size itself comes from the instrument master

# --- NSE trading holidays -----------------------------------------------------
# No holiday calendar feed -- add dates here as they come up.
NSE_HOLIDAYS: set[date] = {
    date(2026, 10, 2),  # Gandhi Jayanti
    date(2026, 11, 10),  # Holiday (confirmed by user, 2026-11-07)
    date(2026, 11, 24),  # Holiday (confirmed by user, 2026-11-23)
}

# --- Order execution -----------------------------------------------------------
LIMIT_PRICE_BUFFER_PCTS = (0.03, 0.06, 0.10)  # increasing aggression per retry
# CONFIRM BEFORE LIVE: no futures-rollover-near-expiry guard exists elsewhere
# in this strategy (it trades options, not the future itself), but the
# signal reads the near-month FUTURE -- skip the week rather than risk
# reading a rolled/stale contract this close to its own expiry.
ROLLOVER_PROXIMITY_DAYS = 2

# --- Market data ---------------------------------------------------------------
# REST polling only -- the WebSocket feed was removed (not working,
# to be revisited later). See git history for broker/market_stream.py
# if it needs to come back.
# Raised from 5 to 15 on 2026-11-25: 5 other live processes on this box
# (4 nifty-strangle-options strategies + this one) share ONE Jainam
# market-data login, and every real token refresh kicks out whoever
# else is holding the old one -- e-session-0007 "Invalid Token" noise
# in run_entry.py/run_monitor_exit.py every 1-3 min. It always
# self-heals within 1-2 retries (nothing was ever lost), but polling
# less often lowers how often THIS process's own poll lands in that
# window. 15s is still far finer than a 3-day-breakout/90%-target
# strategy needs.
POLL_INTERVAL_SECONDS = 15

# --- Jainam (XTS) credentials --------------------------------------------------
JAINAM_BASE_URL = os.environ.get("JAINAM_BASE_URL", "")
JAINAM_MARKET_API_KEY = os.environ.get("JAINAM_MARKET_API_KEY", "")
JAINAM_MARKET_API_SECRET = os.environ.get("JAINAM_MARKET_API_SECRET", "")
JAINAM_INTERACTIVE_API_KEY = os.environ.get("JAINAM_INTERACTIVE_API_KEY", "")
JAINAM_INTERACTIVE_API_SECRET = os.environ.get("JAINAM_INTERACTIVE_API_SECRET", "")
JAINAM_SOURCE = os.environ.get("JAINAM_SOURCE", "WEBAPI")
JAINAM_CLIENT_ID = os.environ.get("JAINAM_CLIENT_ID", "")  # required for order placement, not for dry run
# Our API login under that client (e.g. SNM3358A21). The client account is
# shared with other logins; position checks count only this login's trades.
JAINAM_LOGIN_ID = os.environ.get("JAINAM_LOGIN_ID", "")

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
