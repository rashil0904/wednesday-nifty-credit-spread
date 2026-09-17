"""
Pure decision-logic functions for the Wednesday NIFTY breakout strategy.

Deliberately free of any Kite/network/file I/O so it can be unit tested
without mocking a broker client. entry_monitor.py / exit_monitor.py call
into these functions and handle all I/O themselves.
"""
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional

from . import config


class Direction(str, Enum):
    BEARISH = "BEARISH"  # futures broke below 3-day low -> Call Credit Spread
    BULLISH = "BULLISH"  # futures broke above 3-day high -> Put Credit Spread


class EntryReason(str, Enum):
    BREAKOUT = "BREAKOUT"
    FALLBACK_2_35 = "FALLBACK_2_35"


@dataclass(frozen=True)
class ThreeDayLevels:
    three_day_high: float
    three_day_low: float
    trading_days_used: tuple


def compute_3day_levels(candles: list, trading_day: date) -> ThreeDayLevels:
    """3-Day High/Low reference levels from NIFTY FUTURES daily OHLC.

    `candles` is whatever broker.fetch_futures_daily_candles() returned
    (raw Kite daily-candle dicts, chronologically ascending, all already
    dated before `trading_day` since the caller's date-range query took
    care of that) -- this function only does the pure crunching: take the
    3 most recent completed sessions and find their high/low. No I/O."""
    if len(candles) < 3:
        raise ValueError(
            f"Only {len(candles)} daily candles found before {trading_day}; need at least 3"
        )

    last_three = candles[-3:]
    trading_days_used = tuple(
        c["date"].date() if isinstance(c["date"], datetime) else c["date"]
        for c in last_three
    )

    return ThreeDayLevels(
        three_day_high=max(c["high"] for c in last_three),
        three_day_low=min(c["low"] for c in last_three),
        trading_days_used=trading_days_used,
    )


def detect_breakout(futures_ltp: float, levels: ThreeDayLevels) -> Optional[Direction]:
    """Entry Logic 1 & 2. Futures below 3-day low takes priority per spec
    ordering (checked first), matching the numbered spec exactly."""
    if futures_ltp < levels.three_day_low:
        return Direction.BEARISH
    if futures_ltp > levels.three_day_high:
        return Direction.BULLISH
    return None


def detect_fallback_direction(current_futures_ltp: float, opening_futures_price: float) -> Direction:
    """Entry Logic 3 — 2:35pm red/green check against the day's opening
    futures price. Red (down) -> bearish/Call spread, Green (up) -> bullish/Put
    spread. A perfectly flat print (== open) is treated as Green, i.e. not
    red, since the spec only defines "<" as red."""
    if current_futures_ltp < opening_futures_price:
        return Direction.BEARISH
    return Direction.BULLISH


def compute_atm_strike(spot_price: float, step: int = config.STRIKE_STEP) -> int:
    """Round to the nearest strike, ties rounding up (not Python's
    round-half-to-even) -- the conventional strike-rounding behavior."""
    import math
    return int(math.floor(spot_price / step + 0.5) * step)


def compute_long_leg_strike(atm_strike: int, direction: Direction,
                             offset: int = config.LONG_LEG_OFFSET) -> int:
    """Long leg is the same option type, offset by `offset` points.
    Bearish (short ATM CE) -> buy CE `offset` points higher.
    Bullish (short ATM PE) -> buy PE `offset` points lower."""
    if direction == Direction.BEARISH:
        return atm_strike + offset
    return atm_strike - offset


def option_type_for_direction(direction: Direction) -> str:
    return "CE" if direction == Direction.BEARISH else "PE"


def compute_basis(futures_price: float, spot_price: float) -> float:
    return futures_price - spot_price


def is_basis_anomalous(futures_price: float, spot_price: float,
                        threshold: float = config.BASIS_WARNING_THRESHOLD_POINTS) -> bool:
    return abs(compute_basis(futures_price, spot_price)) > threshold


def compute_max_profit(sell_fill_price: float, buy_fill_price: float,
                        lot_size: int, lots: int) -> float:
    """Max profit for a credit spread = net credit received, from actual
    fills, scaled by quantity. Negative fill data (net debit) is a caller
    error - the strategy is credit-spread only, so this should never happen
    in practice; not defended against here on purpose."""
    credit_per_share = sell_fill_price - buy_fill_price
    return credit_per_share * lot_size * lots


def compute_current_credit_value(current_sell_leg_ltp: float, current_buy_leg_ltp: float) -> float:
    """Cost to close = buy back short leg - sell off long leg. Value
    remaining in the spread (what you'd keep if you closed right now, per
    share) mirrors the same buy-sell convention as at entry."""
    return current_sell_leg_ltp - current_buy_leg_ltp


def captured_profit_fraction(entry_credit_per_share: float, current_credit_per_share: float) -> float:
    """Fraction of max profit captured so far. 1.0 = spread worth zero to
    close (full credit kept). 0.0 = spread still worth the full entry
    credit (no profit captured yet)."""
    if entry_credit_per_share == 0:
        return 0.0
    captured = entry_credit_per_share - current_credit_per_share
    return captured / entry_credit_per_share


def profit_target_hit(entry_credit_per_share: float, current_credit_per_share: float,
                       target_pct: float = config.PROFIT_TARGET_PCT) -> bool:
    return captured_profit_fraction(entry_credit_per_share, current_credit_per_share) >= target_pct


def is_rollover_proximity(futures_expiry: date, trade_date: date,
                           threshold_days: int = config.ROLLOVER_PROXIMITY_DAYS) -> bool:
    return (futures_expiry - trade_date).days <= threshold_days


class WeekAction(str, Enum):
    TRADE_TODAY = "TRADE_TODAY"          # proceed with full entry flow today
    WAIT_NOT_YET = "WAIT_NOT_YET"        # today isn't the resolved trading day for this week
    SKIP_TWO_DAY_HOLIDAY = "SKIP_TWO_DAY_HOLIDAY"  # Wed & Thu both holidays
    ALREADY_RESOLVED = "ALREADY_RESOLVED"  # this week already traded/no-traded/skipped


def resolve_trading_day_action(today: date, is_wednesday: bool, is_thursday: bool,
                                wednesday_is_holiday: Optional[bool],
                                thursday_is_holiday: Optional[bool],
                                week_already_resolved: bool) -> WeekAction:
    """Decides what today's process run should do, given the Wed->Thu
    holiday-shift rule. Wednesday/Thursday holiday flags are looked up by
    the caller (holidays.py) and passed in as Optional[bool] (None only in
    tests that don't care about that branch).

    Two-day-holiday-stretch (both Wed and Thu holidays) is never resolved
    automatically -- see config.TWO_DAY_HOLIDAY_STRETCH_ACTION and the
    report. Default behavior is to skip the week."""
    if week_already_resolved:
        return WeekAction.ALREADY_RESOLVED

    if is_wednesday:
        if wednesday_is_holiday:
            return WeekAction.WAIT_NOT_YET  # shifts to Thursday; nothing to do today
        return WeekAction.TRADE_TODAY

    if is_thursday:
        if not wednesday_is_holiday:
            # Wednesday traded (or resolved) normally; Thursday has nothing to do.
            return WeekAction.ALREADY_RESOLVED
        if thursday_is_holiday:
            return WeekAction.SKIP_TWO_DAY_HOLIDAY
        return WeekAction.TRADE_TODAY

    return WeekAction.WAIT_NOT_YET


def reference_level_window(trading_day: date, all_prior_trading_days: list) -> list:
    """Given trading_day (the actual Wed-or-shifted-Thu trade date) and a
    chronologically ascending list of prior trading-day dates (already
    holiday/weekend-aware, supplied by the caller from Kite's historical
    data), return the 3 immediately before trading_day."""
    prior = [d for d in all_prior_trading_days if d < trading_day]
    return prior[-3:]
