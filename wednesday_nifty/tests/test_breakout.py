from datetime import date

from strategy.breakout import EntryTrigger, check_breakout, fallback_trigger
from strategy.levels import ThreeDayLevels

LEVELS = ThreeDayLevels(high=25400, low=24950, sessions_used=(date(2026, 9, 11),))


def test_breakout_low_breakout_when_below_3day_low():
    assert check_breakout(24900, LEVELS) is EntryTrigger.LOW_BREAKOUT


def test_breakout_high_breakout_when_above_3day_high():
    assert check_breakout(25450, LEVELS) is EntryTrigger.HIGH_BREAKOUT


def test_no_breakout_within_range():
    assert check_breakout(25100, LEVELS) is None


def test_breakout_at_exact_boundary_is_not_a_breach():
    assert check_breakout(LEVELS.high, LEVELS) is None
    assert check_breakout(LEVELS.low, LEVELS) is None


def test_fallback_time_1435_red_when_below_open():
    assert fallback_trigger(25000, 25100) is EntryTrigger.TIME_1435_RED


def test_fallback_time_1435_green_when_above_open():
    assert fallback_trigger(25200, 25100) is EntryTrigger.TIME_1435_GREEN


def test_fallback_none_when_exactly_equal():
    assert fallback_trigger(25100, 25100) is None
