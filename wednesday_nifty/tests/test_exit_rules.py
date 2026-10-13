from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from strategy.exit_rules import profit_target_hit, time_exit_due

IST = ZoneInfo("Asia/Kolkata")


# =============================================================================
# profit_target_hit
# =============================================================================

def test_profit_target_not_hit_when_spread_still_worth_full_credit():
    assert not profit_target_hit(entry_credit=70.0, current_spread_value=70.0)


def test_profit_target_hit_at_exactly_90_percent_captured():
    # 90% captured -> only 10% of entry credit left to buy back
    assert profit_target_hit(entry_credit=70.0, current_spread_value=7.0, target_pct=0.90)


def test_profit_target_not_hit_just_under_90_percent():
    assert not profit_target_hit(entry_credit=70.0, current_spread_value=7.5, target_pct=0.90)


def test_profit_target_never_hit_when_entry_credit_non_positive():
    assert not profit_target_hit(entry_credit=0.0, current_spread_value=0.0)


# =============================================================================
# time_exit_due
# =============================================================================

def test_time_exit_not_due_before_exit_date():
    now = datetime(2026, 9, 21, 15, 20, tzinfo=IST)
    assert not time_exit_due(now, exit_date=date(2026, 9, 22), exit_time=time(15, 15))


def test_time_exit_not_due_on_exit_date_before_exit_time():
    now = datetime(2026, 9, 22, 15, 0, tzinfo=IST)
    assert not time_exit_due(now, exit_date=date(2026, 9, 22), exit_time=time(15, 15))


def test_time_exit_due_on_exit_date_at_exit_time():
    now = datetime(2026, 9, 22, 15, 15, tzinfo=IST)
    assert time_exit_due(now, exit_date=date(2026, 9, 22), exit_time=time(15, 15))


def test_time_exit_due_on_exit_date_after_exit_time():
    now = datetime(2026, 9, 22, 15, 20, tzinfo=IST)
    assert time_exit_due(now, exit_date=date(2026, 9, 22), exit_time=time(15, 15))


def test_time_exit_due_if_somehow_run_after_exit_date():
    now = datetime(2026, 9, 23, 9, 0, tzinfo=IST)
    assert time_exit_due(now, exit_date=date(2026, 9, 22), exit_time=time(15, 15))
