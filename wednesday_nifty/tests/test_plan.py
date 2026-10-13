from datetime import date

import pytest

from strategy.breakout import EntryTrigger
from strategy.plan import LegRole, OptionType, build_entry_plan

EXPIRY = date(2026, 9, 22)


def test_low_breakout_builds_call_credit_spread():
    plan = build_entry_plan(
        trigger=EntryTrigger.LOW_BREAKOUT,
        atm_strike=25100, hedge_strike=25300, expiry=EXPIRY, lot_size=75, lots=1,
    )
    assert plan.option_type is OptionType.CE
    short, hedge = plan.legs
    assert short.role is LegRole.SHORT_ATM and short.strike == 25100 and short.option_type is OptionType.CE
    assert hedge.role is LegRole.LONG_HEDGE and hedge.strike == 25300 and hedge.option_type is OptionType.CE
    assert short.quantity == hedge.quantity == 75


def test_high_breakout_builds_put_credit_spread():
    plan = build_entry_plan(
        trigger=EntryTrigger.HIGH_BREAKOUT,
        atm_strike=25100, hedge_strike=24900, expiry=EXPIRY, lot_size=75, lots=2,
    )
    assert plan.option_type is OptionType.PE
    short, hedge = plan.legs
    assert short.strike == 25100 and hedge.strike == 24900
    assert short.quantity == hedge.quantity == 150


def test_time_1435_red_builds_call_credit_spread():
    plan = build_entry_plan(
        trigger=EntryTrigger.TIME_1435_RED,
        atm_strike=25100, hedge_strike=25300, expiry=EXPIRY, lot_size=75, lots=1,
    )
    assert plan.option_type is OptionType.CE


def test_time_1435_green_builds_put_credit_spread():
    plan = build_entry_plan(
        trigger=EntryTrigger.TIME_1435_GREEN,
        atm_strike=25100, hedge_strike=24900, expiry=EXPIRY, lot_size=75, lots=1,
    )
    assert plan.option_type is OptionType.PE


def test_both_legs_share_expiry_and_trigger_recorded():
    plan = build_entry_plan(
        trigger=EntryTrigger.TIME_1435_RED,
        atm_strike=25100, hedge_strike=25300, expiry=EXPIRY, lot_size=75, lots=1,
    )
    assert plan.trigger is EntryTrigger.TIME_1435_RED
    assert all(leg.expiry == EXPIRY for leg in plan.legs)


def test_rejects_non_positive_lot_size_or_lots():
    with pytest.raises(ValueError):
        build_entry_plan(trigger=EntryTrigger.LOW_BREAKOUT,
                          atm_strike=25100, hedge_strike=25300, expiry=EXPIRY, lot_size=0, lots=1)
    with pytest.raises(ValueError):
        build_entry_plan(trigger=EntryTrigger.LOW_BREAKOUT,
                          atm_strike=25100, hedge_strike=25300, expiry=EXPIRY, lot_size=75, lots=0)
