from datetime import date, datetime

import pytest

from wednesday_nifty import strategy


# --- 3-Day levels from futures OHLC (pure crunching over already-fetched candles) --

def test_3day_levels_use_last_3_completed_sessions_before_trading_day():
    candles = [
        {"date": datetime(2026, 9, 10), "open": 24800, "high": 24950, "low": 24700, "close": 24900},
        {"date": datetime(2026, 9, 11), "open": 24900, "high": 25100, "low": 24850, "close": 25050},
        {"date": datetime(2026, 9, 14), "open": 25050, "high": 25200, "low": 24980, "close": 25150},
        {"date": datetime(2026, 9, 15), "open": 25150, "high": 25300, "low": 25050, "close": 25250},
    ]
    result = strategy.compute_3day_levels(candles, trading_day=date(2026, 9, 16))
    # Should use Sep 11, 14, 15 (the 3 sessions immediately before Sep 16),
    # NOT Sep 10 which is a 4th day back.
    assert result.three_day_high == 25300  # max of the three highs (25100, 25200, 25300)
    assert result.three_day_low == 24850   # min of the three lows (24850, 24980, 25050)
    assert result.trading_days_used == (date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15))


def test_3day_levels_raises_when_insufficient_history():
    candles = [
        {"date": datetime(2026, 9, 14), "high": 25200, "low": 24980},
        {"date": datetime(2026, 9, 15), "high": 25300, "low": 25050},
    ]
    with pytest.raises(ValueError):
        strategy.compute_3day_levels(candles, trading_day=date(2026, 9, 16))


def make_levels(low=100.0, high=200.0):
    return strategy.ThreeDayLevels(three_day_high=high, three_day_low=low,
                                    trading_days_used=(date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)))


# --- Breakout detection (futures only) ----------------------------------------

def test_breakout_bearish_when_below_3day_low():
    levels = make_levels(low=100.0, high=200.0)
    assert strategy.detect_breakout(99.9, levels) == strategy.Direction.BEARISH


def test_breakout_bullish_when_above_3day_high():
    levels = make_levels(low=100.0, high=200.0)
    assert strategy.detect_breakout(200.1, levels) == strategy.Direction.BULLISH


def test_no_breakout_within_range():
    levels = make_levels(low=100.0, high=200.0)
    assert strategy.detect_breakout(150.0, levels) is None


def test_breakout_at_exact_boundary_is_not_a_breach():
    levels = make_levels(low=100.0, high=200.0)
    assert strategy.detect_breakout(100.0, levels) is None
    assert strategy.detect_breakout(200.0, levels) is None


def test_bearish_takes_priority_when_both_conceivably_true():
    # Degenerate levels where low > high would be a data bug, but the spec
    # explicitly checks the below-low condition first (Entry Logic 1 before 2).
    levels = make_levels(low=200.0, high=100.0)
    assert strategy.detect_breakout(150.0, levels) == strategy.Direction.BEARISH


# --- 2:35pm fallback (Entry Logic 3, futures only) ------------------------------

def test_fallback_red_when_below_open():
    assert strategy.detect_fallback_direction(current_futures_ltp=99.0, opening_futures_price=100.0) \
        == strategy.Direction.BEARISH


def test_fallback_green_when_above_open():
    assert strategy.detect_fallback_direction(current_futures_ltp=101.0, opening_futures_price=100.0) \
        == strategy.Direction.BULLISH


def test_fallback_flat_treated_as_green():
    assert strategy.detect_fallback_direction(current_futures_ltp=100.0, opening_futures_price=100.0) \
        == strategy.Direction.BULLISH


# --- Strike selection (spot only) -----------------------------------------------

def test_atm_rounds_to_nearest_50():
    assert strategy.compute_atm_strike(24980) == 25000
    assert strategy.compute_atm_strike(24924) == 24900
    assert strategy.compute_atm_strike(24925) == 24950  # exact halfway rounds up


def test_long_leg_bearish_is_atm_plus_200_same_type_ce():
    atm = strategy.compute_atm_strike(25000)
    long_strike = strategy.compute_long_leg_strike(atm, strategy.Direction.BEARISH)
    assert long_strike == 25200
    assert strategy.option_type_for_direction(strategy.Direction.BEARISH) == "CE"


def test_long_leg_bullish_is_atm_minus_200_same_type_pe():
    atm = strategy.compute_atm_strike(25000)
    long_strike = strategy.compute_long_leg_strike(atm, strategy.Direction.BULLISH)
    assert long_strike == 24800
    assert strategy.option_type_for_direction(strategy.Direction.BULLISH) == "PE"


def test_futures_and_spot_never_conflated_when_they_diverge_meaningfully():
    """The core anti-regression test: breakout/direction must come only
    from futures, strikes only from spot, even when the two prices diverge
    well beyond a normal basis (simulating a volatile session)."""
    futures_price = 25180.0   # breaks above a 25150 3-day high -> BULLISH
    spot_price = 24930.0      # 250-point basis -- deliberately large
    levels = make_levels(low=25000.0, high=25150.0)

    direction = strategy.detect_breakout(futures_price, levels)
    assert direction == strategy.Direction.BULLISH  # decided purely from futures

    atm = strategy.compute_atm_strike(spot_price)
    assert atm == 24950  # derived purely from spot, not futures
    long_strike = strategy.compute_long_leg_strike(atm, direction)
    assert long_strike == 24750

    assert strategy.is_basis_anomalous(futures_price, spot_price, threshold=50)
    assert strategy.compute_basis(futures_price, spot_price) == 250.0


# --- Max profit / 90% target from real fills -------------------------------------

def test_max_profit_from_actual_fills():
    # Sold ATM CE at 82.35, bought hedge CE at 24.10 (actual fills, not mid).
    max_profit = strategy.compute_max_profit(sell_fill_price=82.35, buy_fill_price=24.10,
                                              lot_size=75, lots=1)
    assert round(max_profit, 2) == round((82.35 - 24.10) * 75, 2)


def test_profit_target_not_hit_when_spread_still_worth_full_credit():
    entry_credit = 58.25
    current_credit = 58.25  # unchanged
    assert not strategy.profit_target_hit(entry_credit, current_credit)


def test_profit_target_hit_at_exactly_90_percent_captured():
    entry_credit = 100.0
    current_credit = 10.0  # 90% of the credit has decayed away -> 90% captured
    assert strategy.profit_target_hit(entry_credit, current_credit, target_pct=0.90)


def test_profit_target_not_hit_just_under_90_percent():
    entry_credit = 100.0
    current_credit = 10.01
    assert not strategy.profit_target_hit(entry_credit, current_credit, target_pct=0.90)


# --- Rollover proximity -----------------------------------------------------------

def test_rollover_proximity_flagged_within_threshold():
    trade_date = date(2026, 9, 24)
    futures_expiry = date(2026, 9, 25)  # 1 day out
    assert strategy.is_rollover_proximity(futures_expiry, trade_date, threshold_days=2)


def test_rollover_not_flagged_when_far_enough():
    trade_date = date(2026, 9, 17)
    futures_expiry = date(2026, 9, 25)
    assert not strategy.is_rollover_proximity(futures_expiry, trade_date, threshold_days=2)


# --- Holiday shift (Wed -> Thu) ---------------------------------------------------

def test_normal_wednesday_not_a_holiday_trades_today():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=False, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action == strategy.WeekAction.TRADE_TODAY


def test_wednesday_holiday_waits_for_thursday():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=True, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action == strategy.WeekAction.WAIT_NOT_YET


def test_thursday_trades_when_wednesday_was_holiday_and_thursday_is_not():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=True, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action == strategy.WeekAction.TRADE_TODAY


def test_thursday_noop_when_wednesday_already_traded_normally():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=False, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action == strategy.WeekAction.ALREADY_RESOLVED


def test_two_day_holiday_stretch_is_flagged_not_auto_shifted_to_friday():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=True, thursday_is_holiday=True, week_already_resolved=False,
    )
    assert action == strategy.WeekAction.SKIP_TWO_DAY_HOLIDAY


def test_already_resolved_week_short_circuits_everything_else():
    action = strategy.resolve_trading_day_action(
        today=date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=False, thursday_is_holiday=False, week_already_resolved=True,
    )
    assert action == strategy.WeekAction.ALREADY_RESOLVED


def test_reference_level_window_takes_3_immediately_before_trading_day():
    prior_days = [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10),
                  date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]
    window = strategy.reference_level_window(date(2026, 9, 16), prior_days)
    assert window == [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]
