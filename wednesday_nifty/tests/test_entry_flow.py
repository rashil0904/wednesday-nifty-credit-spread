"""
Integration-ish tests for the entry-day decision sequence: feeding
synthetic tick sequences through the real strategy functions the same
way scripts/run_entry.py's loop does, without any real broker/socket.
Covers the required cases that don't fit neatly into a single module's
unit tests: first-breach-wins, no double entry under rapid ticks /
re-runs, the no-breakout -> 14:35 fallback chain, and the equal-price
no-trade case.
"""

from datetime import date

import pytest

import config
import position_store as ps
from strategy.breakout import EntryTrigger, check_breakout, fallback_trigger
from strategy.levels import ThreeDayLevels
from strategy.plan import build_entry_plan

LEVELS = ThreeDayLevels(high=25400.0, low=24950.0, sessions_used=(date(2026, 9, 11),))


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(ps, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    (tmp_path / "locks").mkdir()


def _scan_for_breakout(ticks):
    """Mirrors run_entry.py's `while True: ... if trigger: break` loop
    body exactly: evaluate ticks one at a time, stop at the first
    breach. Returns (trigger, ticks_actually_consumed)."""
    consumed = 0
    for ltp in ticks:
        consumed += 1
        trigger = check_breakout(ltp, LEVELS)
        if trigger:
            return trigger, consumed
    return None, consumed


# =============================================================================
# First breach wins / rapid ticks never cause a second (or different) entry
# =============================================================================

def test_first_breach_wins_even_when_a_later_tick_implies_the_opposite_direction():
    ticks = [25100, 25200, 24900, 25450]  # LOW breach at index 2, HIGH breach at index 3
    trigger, consumed = _scan_for_breakout(ticks)
    assert trigger is EntryTrigger.LOW_BREAKOUT
    assert consumed == 3  # stopped right after the breach -- the HIGH breach tick is never looked at


def test_rapid_burst_of_ticks_after_breach_does_not_change_the_outcome():
    # A "burst" of 50 ticks right after the breach, all strongly BULLISH --
    # must never be consulted because the scan already stopped.
    ticks = [25100, 24900] + [30000] * 50
    trigger, consumed = _scan_for_breakout(ticks)
    assert trigger is EntryTrigger.LOW_BREAKOUT
    assert consumed == 2


def test_no_breakout_within_range_scans_every_tick_and_finds_nothing():
    ticks = [25100, 25200, 25050, 25300]
    trigger, consumed = _scan_for_breakout(ticks)
    assert trigger is None
    assert consumed == len(ticks)


# =============================================================================
# No double entry: a second attempt on an already-resolved day must be
# blocked by the entry lock (simulating a script re-run / overlapping run)
# =============================================================================

def test_second_entry_attempt_same_day_is_blocked_by_the_lock():
    today = date(2026, 9, 16)
    with ps.acquire_entry_lock(today):
        # Simulates a second invocation of run_entry.py (re-run, or an
        # overlapping cron trigger) while the first is still active.
        with pytest.raises(ps.LockHeldError):
            with ps.acquire_entry_lock(today):
                pass  # would re-enter if the lock didn't block this


def test_lock_release_lets_a_genuinely_new_day_proceed():
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        pass
    # A later, independent run (lock released) must not be blocked.
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        pass


# =============================================================================
# No breakout all day -> 14:35 fallback (red/green), and the equal-price
# no-trade case
# =============================================================================

def test_no_breakout_then_1435_fallback_red_builds_call_credit_spread():
    ticks = [25100, 25200, 25050, 25300]  # never breaches all day
    trigger, _ = _scan_for_breakout(ticks)
    assert trigger is None

    price_1435 = 25000.0
    open_0915 = 25100.0
    fallback = fallback_trigger(price_1435, open_0915)
    assert fallback is EntryTrigger.TIME_1435_RED

    plan = build_entry_plan(trigger=fallback, atm_strike=25000, hedge_strike=25200, expiry=date(2026, 9, 22),
                             lot_size=75, lots=1)
    assert plan.option_type.value == "CE"


def test_no_breakout_then_1435_fallback_green_builds_put_credit_spread():
    ticks = [25100, 25200, 25050, 25300]
    trigger, _ = _scan_for_breakout(ticks)
    assert trigger is None

    fallback = fallback_trigger(25200.0, 25100.0)
    assert fallback is EntryTrigger.TIME_1435_GREEN

    plan = build_entry_plan(trigger=fallback, atm_strike=25200, hedge_strike=25000, expiry=date(2026, 9, 22),
                             lot_size=75, lots=1)
    assert plan.option_type.value == "PE"


def test_no_breakout_then_1435_exactly_equal_is_a_no_trade_day():
    ticks = [25100, 25200, 25050, 25300]
    trigger, _ = _scan_for_breakout(ticks)
    assert trigger is None

    fallback = fallback_trigger(25100.0, 25100.0)
    assert fallback is None

    today = date(2026, 9, 16)
    ps.write_no_trade(today, "14:35 fallback: price exactly equal to 09:15 open")
    data = ps.read_position()
    assert data["traded"] is False
    assert "exactly equal" in data["reason"]
