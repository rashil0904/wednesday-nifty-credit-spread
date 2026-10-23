from datetime import date

from strategy.calendar import (
    WeekAction,
    following_tuesday,
    pick_nearest_expiry,
    resolve_entry_day_action,
    resolve_exit_date,
)


# =============================================================================
# resolve_entry_day_action
# =============================================================================

def test_normal_wednesday_not_a_holiday_trades_today():
    action = resolve_entry_day_action(
        date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=False, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action is WeekAction.TRADE_TODAY


def test_wednesday_holiday_waits_for_thursday():
    action = resolve_entry_day_action(
        date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=True, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action is WeekAction.WAIT_NOT_YET


def test_thursday_trades_when_wednesday_was_holiday_and_thursday_is_not():
    action = resolve_entry_day_action(
        date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=True, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action is WeekAction.TRADE_TODAY


def test_thursday_noop_when_wednesday_already_traded_normally():
    action = resolve_entry_day_action(
        date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=False, thursday_is_holiday=False, week_already_resolved=False,
    )
    assert action is WeekAction.ALREADY_RESOLVED


def test_two_day_holiday_stretch_is_flagged_not_auto_shifted_to_friday():
    action = resolve_entry_day_action(
        date(2026, 9, 17), is_wednesday=False, is_thursday=True,
        wednesday_is_holiday=True, thursday_is_holiday=True, week_already_resolved=False,
    )
    assert action is WeekAction.SKIP_HOLIDAY_STRETCH


def test_already_resolved_week_short_circuits_everything_else():
    action = resolve_entry_day_action(
        date(2026, 9, 16), is_wednesday=True, is_thursday=False,
        wednesday_is_holiday=True, thursday_is_holiday=True, week_already_resolved=True,
    )
    assert action is WeekAction.ALREADY_RESOLVED


# =============================================================================
# following_tuesday
# =============================================================================

def test_following_tuesday_from_wednesday_entry():
    # Wed 2026-09-16 -> next Tuesday is 2026-09-22
    assert following_tuesday(date(2026, 9, 16), expiry_weekday=1) == date(2026, 9, 22)


def test_following_tuesday_from_thursday_entry():
    # Thu 2026-09-17 -> next Tuesday is still 2026-09-22
    assert following_tuesday(date(2026, 9, 17), expiry_weekday=1) == date(2026, 9, 22)


def test_following_tuesday_never_returns_entry_date_itself():
    # Entry day is never itself a Tuesday per the strategy, but if it were,
    # this must roll to next week, not return today.
    assert following_tuesday(date(2026, 9, 22), expiry_weekday=1) == date(2026, 9, 29)


# =============================================================================
# pick_nearest_expiry
# =============================================================================

def test_pick_nearest_expiry_matches_anchor_exactly_when_listed():
    candidates = [date(2026, 9, 22), date(2026, 9, 29)]
    assert pick_nearest_expiry(candidates, anchor=date(2026, 9, 22)) == date(2026, 9, 22)


def test_pick_nearest_expiry_picks_earlier_shifted_expiry_not_next_week():
    # Tuesday 2026-09-22 is a holiday -> NSE lists the real expiry on
    # Monday 2026-09-21 instead. A plain >= filter would wrongly skip to
    # next week's 2026-09-29; this must pick 2026-09-21.
    candidates = [date(2026, 9, 21), date(2026, 9, 29)]
    assert pick_nearest_expiry(candidates, anchor=date(2026, 9, 22)) == date(2026, 9, 21)


def test_pick_nearest_expiry_picks_further_back_shift_over_next_week():
    # Both Monday and Tuesday are holidays -> real expiry shifts to Friday
    # 2026-09-18 (4 days before the anchor), which is still far closer
    # than next week's 2026-09-29 (7 days after).
    candidates = [date(2026, 9, 18), date(2026, 9, 29)]
    assert pick_nearest_expiry(candidates, anchor=date(2026, 9, 22)) == date(2026, 9, 18)


def test_pick_nearest_expiry_rejects_empty_candidates():
    import pytest
    with pytest.raises(ValueError):
        pick_nearest_expiry([], anchor=date(2026, 9, 22))


# =============================================================================
# resolve_exit_date
# =============================================================================

def test_exit_date_is_expiry_when_not_a_holiday():
    assert resolve_exit_date(date(2026, 9, 22), holidays=set()) == date(2026, 9, 22)


def test_exit_date_shifts_to_previous_trading_day_when_expiry_is_a_holiday():
    holidays = {date(2026, 9, 22)}
    assert resolve_exit_date(date(2026, 9, 22), holidays) == date(2026, 9, 21)


def test_exit_date_skips_back_over_weekend_if_needed():
    # Tuesday is a holiday, Monday is also a holiday -> skip to Friday.
    holidays = {date(2026, 9, 22), date(2026, 9, 21)}
    assert resolve_exit_date(date(2026, 9, 22), holidays) == date(2026, 9, 18)
