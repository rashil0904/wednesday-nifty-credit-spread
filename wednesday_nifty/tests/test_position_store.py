from datetime import date

import pytest

import config
import position_store as ps
from strategy.breakout import EntryTrigger
from strategy.levels import ThreeDayLevels

LEVELS = ThreeDayLevels(high=25400.0, low=24950.0, sessions_used=(date(2026, 9, 11),))
EXPIRY = date(2026, 9, 22)
EXIT_DATE = date(2026, 9, 22)
FUTURES_TOKEN = 9001


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(ps, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    (tmp_path / "locks").mkdir()


def legs():
    return [
        ps.StoredLeg(role="SHORT_ATM", option_type="PE", strike=25100, instrument_token=1,
                     tradingsymbol="NIFTY...PE", quantity=75, avg_price=150.0),
        ps.StoredLeg(role="LONG_HEDGE", option_type="PE", strike=24900, instrument_token=2,
                     tradingsymbol="NIFTY...PE", quantity=75, avg_price=80.0),
    ]


# =============================================================================
# write/read round trip
# =============================================================================

def test_read_position_returns_none_when_no_file():
    assert ps.read_position() is None


def test_write_no_trade_round_trips():
    ps.write_no_trade(date(2026, 9, 16), "FLAT at 14:35")
    data = ps.read_position()
    assert data["traded"] is False
    assert data["reason"] == "FLAT at 14:35"


def test_write_position_sets_sl_level_only_for_high_breakout():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    data = ps.read_position()
    assert data["sl_level"] == 25400.0
    assert data["status"] == "OPEN"
    assert data["legs_filled"] is False
    assert len(data["legs"]) == 2


def test_write_position_sl_level_is_null_for_other_triggers():
    ps.write_position(date(2026, 9, 16), EntryTrigger.LOW_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    data = ps.read_position()
    assert data["sl_level"] is None


def test_update_position_merges_fields():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    ps.update_position(legs_filled=True)
    assert ps.read_position()["legs_filled"] is True


def test_update_position_raises_without_existing_file():
    with pytest.raises(RuntimeError):
        ps.update_position(status="CLOSED")


# =============================================================================
# begin_exit: atomic OPEN -> EXITING, exactly-once guard
# =============================================================================

def test_begin_exit_succeeds_from_open():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    assert ps.begin_exit("STOP_LOSS") is True
    data = ps.read_position()
    assert data["status"] == "EXITING"
    assert data["exit_reason"] == "STOP_LOSS"


def test_begin_exit_fails_when_already_exiting():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    assert ps.begin_exit("STOP_LOSS") is True
    # A second exit path (e.g. profit target on the same tick) must lose.
    assert ps.begin_exit("PROFIT_TARGET") is False
    assert ps.read_position()["exit_reason"] == "STOP_LOSS"  # unchanged, first writer wins


def test_begin_exit_fails_when_no_position():
    assert ps.begin_exit("STOP_LOSS") is False


def test_begin_exit_fails_when_already_closed():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    ps.update_position(status="CLOSED")
    assert ps.begin_exit("STOP_LOSS") is False


# =============================================================================
# sl_level survives a "restart" (just re-reading the file fresh)
# =============================================================================

def test_sl_level_readable_after_simulated_restart():
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, EXIT_DATE, FUTURES_TOKEN, legs(), entry_credit=70.0)
    # Simulate a process restart: nothing in memory, re-read from disk.
    data = ps.read_position()
    assert data["sl_level"] == 25400.0
    assert data["trigger"] == "HIGH_BREAKOUT"


# =============================================================================
# File locks
# =============================================================================

def test_entry_lock_blocks_concurrent_acquisition():
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        with pytest.raises(ps.LockHeldError):
            with ps.acquire_entry_lock(date(2026, 9, 16)):
                pass


def test_entry_lock_released_after_context_exits():
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        pass
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        pass  # must not raise -- previous lock was released


def test_entry_lock_is_per_day():
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        with ps.acquire_entry_lock(date(2026, 9, 17)):
            pass  # different day, different lock file -- must not raise


def test_exit_lock_blocks_concurrent_acquisition():
    with ps.acquire_exit_lock():
        with pytest.raises(ps.LockHeldError):
            with ps.acquire_exit_lock():
                pass


def test_monitor_lock_is_separate_from_entry_lock():
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        with ps.acquire_monitor_lock(date(2026, 9, 16)):
            pass  # different lock namespace, same day -- must not raise


def test_monitor_lock_blocks_concurrent_acquisition():
    with ps.acquire_monitor_lock(date(2026, 9, 17)):
        with pytest.raises(ps.LockHeldError):
            with ps.acquire_monitor_lock(date(2026, 9, 17)):
                pass


def test_lock_left_by_a_dead_process_is_taken_over():
    import subprocess
    dead = subprocess.Popen(["true"])
    dead.wait()
    (config.LOCK_DIR / "entry_2026-09-16.lock").write_text(str(dead.pid))
    with ps.acquire_entry_lock(date(2026, 9, 16)):
        pass  # must not raise -- the holder is gone


def test_lock_held_by_a_live_process_still_blocks():
    import os
    (config.LOCK_DIR / "entry_2026-09-16.lock").write_text(str(os.getppid()))
    with pytest.raises(ps.LockHeldError):
        with ps.acquire_entry_lock(date(2026, 9, 16)):
            pass
