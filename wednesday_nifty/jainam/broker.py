"""
Jainam (XTS) instrument-resolution + session layer -- mirrors
zerodha/broker.py's public function names/signatures (FuturesContract,
OptionInstrument, resolve_current_month_future, resolve_weekly_expiry,
resolve_option, get_nifty_spot_ltp, fetch_futures_daily_candles,
get_futures_opening_price, get_latest_futures_price, get_client) so
monitor.py can call either broker module identically. See
jainam/client.py's module docstring for the underlying API and its
CONFIRM-BEFORE-LIVE caveats.

NSE trading-holiday lookup (is_trading_holiday / HolidayLookupError /
get_holiday_set) is NOT reimplemented here -- it's not a broker-specific
concern (hits NSE's own public endpoint, not Kite's or Jainam's), so it's
re-exported straight from zerodha.broker where it already lives.
"""
from datetime import date, time, timedelta
from typing import Optional

from .. import config
from ..logger import get_logger
from ..zerodha.broker import (  # noqa: F401 -- re-exported, not broker-specific
    FuturesContract,
    OptionInstrument,
    HolidayLookupError,
    get_holiday_set,
    is_trading_holiday,
)
from .client import NSE_FO_SEGMENT, XTSDataClient

logger = get_logger("jainam.broker")


def get_client() -> Optional[XTSDataClient]:
    """Returns a ready-to-use XTSDataClient, or None if config is missing
    or login fails. Unlike Kite, Jainam/XTS auth is a plain API-key/secret
    POST that returns a token directly -- no daily manual browser login
    step, so this authenticates fresh on every call rather than reading a
    cached session file."""
    required = {
        "JAINAM_BASE_URL": config.JAINAM_BASE_URL,
        "JAINAM_MARKET_API_KEY": config.JAINAM_MARKET_API_KEY,
        "JAINAM_MARKET_API_SECRET": config.JAINAM_MARKET_API_SECRET,
        "JAINAM_INTERACTIVE_API_KEY": config.JAINAM_INTERACTIVE_API_KEY,
        "JAINAM_INTERACTIVE_API_SECRET": config.JAINAM_INTERACTIVE_API_SECRET,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        logger.error("Missing in .env: %s", ", ".join(missing))
        return None

    try:
        return XTSDataClient.from_env()
    except Exception:
        logger.exception("Jainam login failed")
        return None


def resolve_current_month_future(client: XTSDataClient, today: date) -> FuturesContract:
    result = client.get_nifty_fut_instrument(today)
    return FuturesContract(
        tradingsymbol=result["tradingsymbol"],
        instrument_token=result["instrument_token"],
        expiry=result["expiry"],
    )


def resolve_weekly_expiry(client: XTSDataClient, trade_date: date) -> date:
    """Nearest NIFTY option expiry >= trade_date. Never assumes a weekday —
    purely observed from the live instrument master, same posture as
    zerodha.broker.resolve_weekly_expiry."""
    return min(client.list_nifty_option_expiries(trade_date))


def resolve_option(client: XTSDataClient, expiry: date, strike: int, option_type: str) -> OptionInstrument:
    result = client.resolve_option_instrument(strike, option_type, expiry)
    return OptionInstrument(
        tradingsymbol=result["tradingsymbol"],
        instrument_token=result["instrument_token"],
        expiry=expiry,
        strike=strike,
        lot_size=int(result["lot_size"]),
    )


def get_nifty_spot_ltp(client: XTSDataClient) -> float:
    return client.get_nifty_spot_ltp()


def fetch_futures_daily_candles(client: XTSDataClient, future_instrument_token: int,
                                 trading_day: date) -> list:
    """Fetches enough daily candles ending before `trading_day` to find the
    3 most recent completed sessions, in the same dict shape Kite's
    historical_data() returns (so strategy.compute_3day_levels works
    unchanged regardless of broker). See client.get_daily_candles's
    CONFIRM-BEFORE-LIVE note on the daily compressionValue."""
    to_date = trading_day - timedelta(days=1)
    from_date = to_date - timedelta(days=14)  # generous buffer past weekends/holidays
    candles = client.get_daily_candles(NSE_FO_SEGMENT, future_instrument_token, from_date, to_date)
    return [
        {"date": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
        for c in candles
    ]


def get_futures_opening_price(client: XTSDataClient, future: FuturesContract, trading_day: date) -> float:
    return client.get_candle_at(NSE_FO_SEGMENT, future.instrument_token, trading_day, time(9, 15)).open


def get_latest_futures_price(client: XTSDataClient, future: FuturesContract) -> float:
    return client.get_ltp(NSE_FO_SEGMENT, future.instrument_token)
