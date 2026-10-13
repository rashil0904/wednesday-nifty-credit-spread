from strategy.breakout import EntryTrigger
from strategy.stop_loss import should_stop_out

SL_LEVEL = 25400.0  # the 3D_HIGH that was breached at entry


def test_high_breakout_stops_out_below_sl_level():
    assert should_stop_out(EntryTrigger.HIGH_BREAKOUT, SL_LEVEL, 25399.9)


def test_high_breakout_does_not_stop_out_at_exactly_sl_level():
    assert not should_stop_out(EntryTrigger.HIGH_BREAKOUT, SL_LEVEL, 25400.0)


def test_high_breakout_does_not_stop_out_above_sl_level():
    assert not should_stop_out(EntryTrigger.HIGH_BREAKOUT, SL_LEVEL, 25450.0)


def test_high_breakout_gap_down_open_below_sl_fires_on_first_tick():
    # A later day's opening tick can be far below the level -- still fires.
    assert should_stop_out(EntryTrigger.HIGH_BREAKOUT, SL_LEVEL, 25000.0)


def test_low_breakout_never_stops_out_regardless_of_price():
    assert not should_stop_out(EntryTrigger.LOW_BREAKOUT, SL_LEVEL, 0.0)
    assert not should_stop_out(EntryTrigger.LOW_BREAKOUT, SL_LEVEL, 99999.0)


def test_time_1435_red_never_stops_out():
    assert not should_stop_out(EntryTrigger.TIME_1435_RED, SL_LEVEL, 0.0)


def test_time_1435_green_never_stops_out():
    assert not should_stop_out(EntryTrigger.TIME_1435_GREEN, SL_LEVEL, 0.0)
