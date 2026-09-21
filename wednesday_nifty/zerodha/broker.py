"""
All broker/Kite-connection and external-data concerns in one place:
  - Session handling: daily access-token cache + the login CLI that
    produces it.
  - Instrument resolution: current-month futures contract, weekly option
    expiry, individual option tradingsymbols, spot LTP -- all read fresh
    from Kite's live instrument master, nothing hardcoded.
  - Futures daily-candle fetch (thin wrapper; the high/low crunching itself
    lives in strategy.py so that logic stays pure/testable).
  - NSE trading-holiday lookup (not a Kite endpoint, but the same
    "resolve external facts before deciding what to do" concern).

Run `python -m wednesday_nifty.zerodha.broker` each trading morning to refresh the
day's session before the monitors run.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple, Optional, Set
from urllib.parse import parse_qs, urlparse

import requests
from kiteconnect import KiteConnect

from .. import config
from ..logger import get_logger

logger = get_logger("broker")

NIFTY_SPOT_EXCHANGE_SYMBOL = "NSE:NIFTY 50"


# =============================================================================
# Session handling (daily access-token cache)
# =============================================================================
#
# Kite Connect has no silent refresh token: access_token must be
# regenerated daily via the login flow and is invalidated overnight.
# get_client() only *reads* a cached token produced by login/main()
# below -- it never attempts to log in on its own. If the cached token is
# missing or not from today (IST), callers get None and must log-and-skip
# rather than guess.

def _today_ist_str() -> str:
    return datetime.now(config.IST).date().isoformat()


def save_session(access_token: str) -> None:
    config.SESSION_FILE.write_text(
        json.dumps({"access_token": access_token, "date": _today_ist_str()}, indent=2)
    )
    logger.info("Session token cached for %s", _today_ist_str())


def load_cached_token() -> Optional[str]:
    if not config.SESSION_FILE.exists():
        return None
    try:
        data = json.loads(config.SESSION_FILE.read_text())
    except Exception:
        logger.exception("Failed to parse session cache file")
        return None

    if data.get("date") != _today_ist_str():
        logger.warning(
            "Cached Kite session is from %s, not today (%s) — stale, refusing to use it. "
            "Run `python -m wednesday_nifty.zerodha.broker`.", data.get("date"), _today_ist_str(),
        )
        return None

    return data.get("access_token")


def get_client() -> Optional[KiteConnect]:
    """Returns a ready-to-use KiteConnect client, or None if no fresh
    session is available. Callers must handle None by logging and
    skipping — never by attempting to log in themselves."""
    if not config.KITE_API_KEY:
        logger.error("KITE_API_KEY not set in environment/.env")
        return None

    token = load_cached_token()
    if token is None:
        logger.error(
            "No valid Kite access token for today. Run "
            "`python -m wednesday_nifty.zerodha.broker` before market open."
        )
        return None

    kite = KiteConnect(api_key=config.KITE_API_KEY)
    kite.set_access_token(token)
    return kite


# =============================================================================
# Login CLI
# =============================================================================
#
# Default flow is manual (prints the login URL, you complete browser login,
# paste back the redirect URL). An optional auto-login path exists if you
# set KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env, but it is
# OFF by default and not recommended without reading the report's caveats:
# it hits Zerodha's own (undocumented, not officially supported) login
# endpoints directly with your password + TOTP secret, which is a real
# credential-security tradeoff and could break without notice if Zerodha
# changes their login flow.

def _manual_login(kite: KiteConnect) -> str:
    print(f"1. Open this URL in a browser and log in:\n\n   {kite.login_url()}\n")
    redirect_url = input("2. Paste the full redirect URL you land on after login: ").strip()
    query = parse_qs(urlparse(redirect_url).query)
    request_token = query.get("request_token", [None])[0]
    if not request_token:
        raise ValueError("Could not find request_token in the pasted URL")
    return request_token


def _auto_login(kite: KiteConnect) -> str:
    """Best-effort, unsupported automation of Zerodha's own login API using
    password + TOTP. Not maintained against Zerodha changing their login
    flow -- if this breaks, fall back to the manual flow above."""
    import pyotp

    session = requests.Session()
    login_resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": config.KITE_USER_ID, "password": config.KITE_PASSWORD},
        timeout=10,
    ).json()
    request_id = login_resp["data"]["request_id"]

    totp = pyotp.TOTP(config.KITE_TOTP_SECRET).now()
    session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": config.KITE_USER_ID,
            "request_id": request_id,
            "twofa_value": totp,
            "twofa_type": "totp",
        },
        timeout=10,
    ).raise_for_status()

    resp = session.get(
        "https://kite.zerodha.com/connect/login",
        params={"api_key": config.KITE_API_KEY, "v": "3"},
        allow_redirects=True,
        timeout=10,
    )
    query = parse_qs(urlparse(resp.url).query)
    request_token = query.get("request_token", [None])[0]
    if not request_token:
        raise ValueError(
            "Auto-login did not yield a request_token — Zerodha's login flow "
            "may have changed. Fall back to manual login."
        )
    return request_token


def login_main() -> int:
    if not config.KITE_API_KEY or not config.KITE_API_SECRET:
        logger.error("KITE_API_KEY / KITE_API_SECRET not set — check your .env")
        return 1

    kite = KiteConnect(api_key=config.KITE_API_KEY)

    try:
        if config.AUTO_LOGIN_ENABLED:
            logger.info("Attempting auto-login (opt-in, unsupported)")
            request_token = _auto_login(kite)
        else:
            request_token = _manual_login(kite)

        session_data = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
        save_session(session_data["access_token"])
        print("Login successful — session cached for today.")
        return 0
    except Exception:
        logger.exception("Login failed")
        print("Login failed — see log for details.")
        return 1


# =============================================================================
# Instrument resolution
# =============================================================================
#
# Nothing here is hardcoded: futures contract, weekly-expiry weekday, and
# lot size are all read fresh so an NSE calendar/lot-size change never
# silently breaks this.

class FuturesContract(NamedTuple):
    tradingsymbol: str
    instrument_token: int
    expiry: date


class OptionInstrument(NamedTuple):
    tradingsymbol: str
    instrument_token: int
    expiry: date
    strike: int
    lot_size: int


def _instruments_cache_path_for_today() -> Path:
    today = datetime.now(config.IST).date().isoformat()
    return config.INSTRUMENTS_CACHE_DIR / f"nfo_instruments_{today}.json"


def get_nfo_instruments(kite) -> list:
    """Fetches kite.instruments('NFO') once per calendar day, caching to
    disk so repeated calls within a day (entry + exit monitors) don't
    re-fetch the full dump."""
    cache_path = _instruments_cache_path_for_today()
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    instruments = kite.instruments("NFO")
    # Kite returns date objects for `expiry`; make JSON-serializable.
    serializable = []
    for row in instruments:
        row = dict(row)
        if isinstance(row.get("expiry"), (date, datetime)):
            row["expiry"] = row["expiry"].isoformat()
        serializable.append(row)

    cache_path.write_text(json.dumps(serializable))
    return serializable


def resolve_current_month_future(kite, today: date) -> FuturesContract:
    """Nearest-expiry NIFTY future with expiry >= today ("current month" /
    near-month contract). Caller is responsible for checking rollover
    proximity via strategy.is_rollover_proximity before trading it."""
    instruments = get_nfo_instruments(kite)
    candidates = [
        row for row in instruments
        if row["name"] == "NIFTY" and row["segment"] == "NFO-FUT"
        and datetime.strptime(row["expiry"], "%Y-%m-%d").date() >= today
    ]
    if not candidates:
        raise LookupError("No live NIFTY futures contract found with expiry >= today")

    nearest = min(candidates, key=lambda r: r["expiry"])
    return FuturesContract(
        tradingsymbol=nearest["tradingsymbol"],
        instrument_token=nearest["instrument_token"],
        expiry=datetime.strptime(nearest["expiry"], "%Y-%m-%d").date(),
    )


def resolve_weekly_expiry(kite, trade_date: date) -> date:
    """Nearest NIFTY option expiry >= trade_date. Never assumes a weekday —
    purely observed from the live instrument dump."""
    instruments = get_nfo_instruments(kite)
    expiries = {
        datetime.strptime(row["expiry"], "%Y-%m-%d").date()
        for row in instruments
        if row["name"] == "NIFTY" and row["segment"] == "NFO-OPT"
        and datetime.strptime(row["expiry"], "%Y-%m-%d").date() >= trade_date
    }
    if not expiries:
        raise LookupError("No live NIFTY option expiries found >= trade_date")
    return min(expiries)


def resolve_option(kite, expiry: date, strike: int, option_type: str) -> OptionInstrument:
    instruments = get_nfo_instruments(kite)
    for row in instruments:
        if (
            row["name"] == "NIFTY"
            and row["segment"] == "NFO-OPT"
            and datetime.strptime(row["expiry"], "%Y-%m-%d").date() == expiry
            and int(row["strike"]) == strike
            and row["instrument_type"] == option_type
        ):
            return OptionInstrument(
                tradingsymbol=row["tradingsymbol"],
                instrument_token=row["instrument_token"],
                expiry=expiry,
                strike=strike,
                lot_size=int(row["lot_size"]),
            )
    raise LookupError(f"No NIFTY {option_type} {strike} instrument found for expiry {expiry}")


def get_nifty_spot_ltp(kite) -> float:
    quote = kite.ltp([NIFTY_SPOT_EXCHANGE_SYMBOL])
    return quote[NIFTY_SPOT_EXCHANGE_SYMBOL]["last_price"]


def get_futures_opening_price(kite, future: FuturesContract, trading_day: date) -> float:
    """`trading_day` is unused here (Kite's quote always reflects the
    current session) -- present only so the signature matches
    jainam.broker's, which needs it to read a specific day's 09:15 candle."""
    key = f"NFO:{future.tradingsymbol}"
    return kite.quote([key])[key]["ohlc"]["open"]


def get_latest_futures_price(kite, future: FuturesContract) -> float:
    key = f"NFO:{future.tradingsymbol}"
    return kite.ltp([key])[key]["last_price"]


def fetch_futures_daily_candles(kite, future_instrument_token: int, trading_day: date) -> list:
    """Fetches enough daily candles ending before `trading_day` to find the
    3 most recent completed sessions. Just the fetch -- strategy.py's
    compute_3day_levels() does the pure high/low crunching from whatever
    this returns."""
    from datetime import timedelta

    to_date = trading_day - timedelta(days=1)
    from_date = to_date - timedelta(days=14)  # generous buffer past weekends/holidays
    return kite.historical_data(
        future_instrument_token, from_date=from_date, to_date=to_date, interval="day",
    )


# =============================================================================
# NSE trading-holiday lookup
# =============================================================================
#
# Source of truth: NSE's own public holiday-master endpoint
# (https://www.nseindia.com/api/holiday-master?type=trading), which returns
# the official exchange holiday list for the current year (segments include
# "CM" cash-market and "FO" futures-and-options -- we use both). This is
# NOT hardcoded/scraped HTML -- it's NSE's own JSON API, fetched fresh and
# cached locally so a transient network hiccup on a trading morning doesn't
# block the whole day.
#
# NSE's site requires a warmed-up session (cookies from an initial homepage
# hit + a browser-like User-Agent) before the API responds; this is
# standard practice for this endpoint, not a ToS workaround for anything
# auth-related (it's public data with no login involved).
#
# Fallback chain if the live fetch fails on a given day:
#   1. Local cache file (config.HOLIDAY_CACHE_FILE), if present.
#   2. Local manual override file (config.HOLIDAY_OVERRIDE_FILE) -- a
#      user-editable JSON list of extra/override dates, for the case where
#      NSE's endpoint is unreachable or has changed shape and nobody has
#      fixed this module yet.
#   3. If neither is available: fail *open* is NOT used here. Callers treat
#      "cannot determine holiday status" (HolidayLookupError) as a reason
#      to log CRITICAL and skip trading that day rather than guess.

_NSE_HOME_URL = "https://www.nseindia.com"
_NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class HolidayLookupError(Exception):
    """Raised when holiday status cannot be determined from any source."""


def _fetch_live_holidays() -> Set[date]:
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    session.get(_NSE_HOME_URL, timeout=10)  # warm up cookies
    resp = session.get(_NSE_HOLIDAY_URL, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    holidays = set()
    for segment in ("FO", "CM"):
        for entry in payload.get(segment, []):
            # NSE's date format has historically been "DD-Mon-YYYY", e.g. "26-Jan-2026".
            raw = entry.get("tradingDate") or entry.get("date")
            if not raw:
                continue
            parsed = datetime.strptime(raw, "%d-%b-%Y").date()
            holidays.add(parsed)

    if not holidays:
        raise HolidayLookupError("NSE holiday API returned no dates — unexpected response shape")

    return holidays


def _load_holiday_cache() -> Optional[Set[date]]:
    if not config.HOLIDAY_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(config.HOLIDAY_CACHE_FILE.read_text())
        return {datetime.strptime(d, "%Y-%m-%d").date() for d in data["dates"]}
    except Exception:
        logger.exception("Failed to parse holiday cache file")
        return None


def _save_holiday_cache(holidays: Set[date]) -> None:
    config.HOLIDAY_CACHE_FILE.write_text(
        json.dumps({"fetched_at": datetime.now().isoformat(),
                    "dates": sorted(d.isoformat() for d in holidays)}, indent=2)
    )


def _load_holiday_overrides() -> Set[date]:
    if not config.HOLIDAY_OVERRIDE_FILE.exists():
        return set()
    try:
        data = json.loads(config.HOLIDAY_OVERRIDE_FILE.read_text())
        return {datetime.strptime(d, "%Y-%m-%d").date() for d in data.get("dates", [])}
    except Exception:
        logger.exception("Failed to parse holiday override file")
        return set()


def get_holiday_set() -> Set[date]:
    """Live fetch, falling back to cache, raising HolidayLookupError if
    neither works. Overrides are always unioned in regardless."""
    overrides = _load_holiday_overrides()
    try:
        live = _fetch_live_holidays()
        _save_holiday_cache(live)
        return live | overrides
    except Exception as exc:
        logger.warning("Live NSE holiday fetch failed (%s); falling back to cache", exc)
        cached = _load_holiday_cache()
        if cached is not None:
            return cached | overrides
        if overrides:
            logger.warning(
                "No live data and no cache — proceeding with override file only. "
                "This is a degraded, likely-incomplete holiday list."
            )
            return overrides
        raise HolidayLookupError(
            "Could not determine NSE holiday calendar: live fetch failed, "
            "no local cache, no override file"
        ) from exc


def is_trading_holiday(check_date: date) -> bool:
    return check_date in get_holiday_set()


if __name__ == "__main__":
    sys.exit(login_main())
