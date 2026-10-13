"""
JSON-backed position store + the file-lock guards that make the
one-trade-per-week entry and the single-exit-path guarantee safe
against a burst of ticks, a script re-run, or two scripts overlapping.

mode "dry"  = what a dry run computed; nothing was traded.
mode "live" = real orders were placed; legs carry the ACTUAL filled
              quantity and average price.
status      = "OPEN" -> (optionally) "EXITING" -> "CLOSED".
              "EXITING" is a short-lived guard: begin_exit() sets it
              atomically (read-check-write) so that if the profit-target
              check and the stop-loss check both fire for the same
              position, only the first one to call begin_exit() wins --
              the second sees status != "OPEN" and backs off. Combined
              with the exit file lock (acquire_exit_lock), this also
              protects against two separate script runs (e.g.
              run_entry.py's own post-entry monitoring and a separately
              cron'd run_monitor_exit.py) racing on the same day.

positions.json / the lock files live under this project's root and are
gitignored -- local run state, not source.
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import config
from strategy.breakout import EntryTrigger
from strategy.levels import ThreeDayLevels

IST = config.IST
POSITIONS_PATH = config.POSITIONS_FILE


@dataclass(frozen=True)
class StoredLeg:
    role: str
    option_type: str
    strike: float
    instrument_token: int
    tradingsymbol: str
    quantity: int
    avg_price: float = 0.0  # actual fill price (live only)


# =============================================================================
# Position read/write
# =============================================================================

def write_no_trade(entry_date_: date, reason: str, mode: str = "dry") -> None:
    _write({
        "entry_date": entry_date_.isoformat(),
        "mode": mode,
        "traded": False,
        "reason": reason,
        "written_at": datetime.now(IST).isoformat(),
    })


def write_position(
    entry_date_: date,
    trigger: EntryTrigger,
    levels: ThreeDayLevels,
    expiry: date,
    exit_date: date,
    futures_instrument_token: int,
    legs: list[StoredLeg],
    entry_credit: float,
    mode: str = "dry",
    status: str = "OPEN",
) -> None:
    """sl_level is set ONLY for HIGH_BREAKOUT (the 3D_HIGH that was
    breached, fixed here forever -- never recomputed daily). Every other
    trigger gets sl_level=None, i.e. no stop loss exists for it.

    futures_instrument_token is the near-month future used to compute
    the signal/SL at entry -- stored so a later day's monitor can detect
    if that contract has since rolled (see the rollover-flag check in
    scripts/run_monitor_exit.py) rather than silently comparing a new
    contract's price against an old SL level."""
    sl_level = levels.high if trigger is EntryTrigger.HIGH_BREAKOUT else None
    _write({
        "entry_date": entry_date_.isoformat(),
        "mode": mode,
        "status": status,
        "traded": True,
        "trigger": trigger.value,
        "levels": {
            "3d_high": levels.high,
            "3d_low": levels.low,
            "sessions_used": [d.isoformat() for d in levels.sessions_used],
        },
        "sl_level": sl_level,
        "futures_instrument_token": futures_instrument_token,
        "expiry": expiry.isoformat(),
        "exit_date": exit_date.isoformat(),
        "legs": [asdict(leg) for leg in legs],
        "entry_credit": entry_credit,
        "legs_filled": False,  # run_entry.py flips this once BOTH legs are confirmed filled
        "closed_roles": [],
        "exit_reason": None,  # PROFIT_TARGET / STOP_LOSS / TIME_EXIT, set by update_position
        "written_at": datetime.now(IST).isoformat(),
    })


def update_position(**fields) -> dict:
    """Merge fields into the stored position (e.g. legs_filled,
    closed_roles, status, exit_reason)."""
    data = read_position()
    if data is None:
        raise RuntimeError("No positions.json to update")
    data.update(fields)
    data["updated_at"] = datetime.now(IST).isoformat()
    _write(data)
    return data


def read_position() -> dict | None:
    """Returns the stored position dict, or None if no file exists yet."""
    if not POSITIONS_PATH.exists():
        return None
    return json.loads(POSITIONS_PATH.read_text())


def begin_exit(reason: str) -> bool:
    """Atomically (read-check-write, meant to be called while holding
    acquire_exit_lock()) transitions status OPEN -> EXITING and records
    exit_reason. Returns False without writing if the position isn't
    OPEN (already EXITING or CLOSED) -- the caller must then NOT place
    any exit order, since someone else's exit already won."""
    data = read_position()
    if data is None or data.get("status") != "OPEN":
        return False
    data["status"] = "EXITING"
    data["exit_reason"] = reason
    data["updated_at"] = datetime.now(IST).isoformat()
    _write(data)
    return True


def _write(data: dict) -> None:
    tmp = POSITIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(POSITIONS_PATH)  # atomic: a crash can't leave a half-written file


# =============================================================================
# File locks -- guard against concurrent entry/exit (re-run, overlap)
# =============================================================================

class LockHeldError(Exception):
    """Raised when a lock is already held (by this or another process)."""


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockHeldError(f"Lock already held: {self.path}") from None
        os.write(self._fd, str(os.getpid()).encode())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fd is not None:
            os.close(self._fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def acquire_entry_lock(entry_date_: date) -> _FileLock:
    """One lock per calendar day -- a stale lock from a prior day can
    never block today, and within today it's a singleton guard against
    a stuck/overlapping run."""
    return _FileLock(config.LOCK_DIR / f"entry_{entry_date_.isoformat()}.lock")


def acquire_monitor_lock(day: date) -> _FileLock:
    """One lock per calendar day, separate from acquire_entry_lock --
    used by run_monitor_exit.py to guard against two overlapping
    monitor runs on the same later day."""
    return _FileLock(config.LOCK_DIR / f"monitor_{day.isoformat()}.lock")


def acquire_exit_lock() -> _FileLock:
    """A single exit lock (not per-day): held only for the brief
    begin_exit()-through-order-placement critical section, so the
    profit-target check, the stop-loss check, and the time-exit check
    can never all race into closing the same position twice."""
    return _FileLock(config.LOCK_DIR / "exit.lock")
