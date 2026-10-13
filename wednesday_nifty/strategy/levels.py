"""
3-Day High/Low reference levels from NIFTY futures daily candles.
Pure logic, no I/O -- the caller fetches candles (broker/xts_client.py)
and hands them here already filtered to sessions before the trading day.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ThreeDayLevels:
    high: float
    low: float
    sessions_used: tuple[date, ...]


def compute_3day_levels(daily_candles: list[dict], before_date: date) -> ThreeDayLevels:
    """
    daily_candles: chronologically ascending list of {"date": date, "high":
    float, "low": float, ...}, already weekend/holiday-aware (whatever the
    broker's daily-candle feed actually returned -- this function doesn't
    second-guess which sessions existed, it just takes the last 3 that are
    strictly before `before_date`).
    """
    prior = [c for c in daily_candles if c["date"] < before_date]
    if len(prior) < 3:
        raise ValueError(
            f"Only {len(prior)} daily candles found before {before_date}; need at least 3"
        )
    last_three = prior[-3:]
    return ThreeDayLevels(
        high=max(c["high"] for c in last_three),
        low=min(c["low"] for c in last_three),
        sessions_used=tuple(c["date"] for c in last_three),
    )
