from datetime import date

import pytest

from strategy.levels import compute_3day_levels


def candle(d, high, low):
    return {"date": d, "high": high, "low": low}


def test_takes_last_3_sessions_before_trading_day():
    candles = [
        candle(date(2026, 9, 10), 25200, 25000),
        candle(date(2026, 9, 11), 25300, 25050),  # 3 sessions before Wed -> used
        candle(date(2026, 9, 14), 25400, 25100),  # used
        candle(date(2026, 9, 15), 25250, 24950),  # used (most recent before Wed)
    ]
    levels = compute_3day_levels(candles, before_date=date(2026, 9, 16))
    assert levels.high == 25400
    assert levels.low == 24950
    assert levels.sessions_used == (date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15))


def test_ignores_sessions_on_or_after_trading_day():
    candles = [
        candle(date(2026, 9, 11), 25300, 25050),
        candle(date(2026, 9, 14), 25400, 25100),
        candle(date(2026, 9, 15), 25250, 24950),
        candle(date(2026, 9, 16), 99999, 1),  # same-day candle must never be used
    ]
    levels = compute_3day_levels(candles, before_date=date(2026, 9, 16))
    assert levels.high == 25400
    assert levels.low == 24950


def test_raises_when_fewer_than_3_prior_sessions():
    candles = [candle(date(2026, 9, 15), 25250, 24950)]
    with pytest.raises(ValueError):
        compute_3day_levels(candles, before_date=date(2026, 9, 16))
