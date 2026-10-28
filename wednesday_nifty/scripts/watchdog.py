"""
Dead-process watchdog -- run from cron every couple of minutes during
market hours:

    */2 9-15 * * 1-5 cd .../wednesday_nifty && .venv/bin/python scripts/watchdog.py >> logs/watchdog.log 2>&1

2026-10-28: run_entry.py died at 12:17 and nothing noticed -- its one
[CRASH] alert was lost to the same network blip, and nothing restarted
it, so the 14:35 fallback never ran. A crash handler can't cover a
process that's killed outright (OOM, SIGKILL, reboot) either, so this
checks from the outside instead.

Works out which script SHOULD be running right now and checks that its
lock is held by a live process (both scripts take their lock first
thing). If not: Telegram [DEAD] alert and restart it. Both scripts are
safe to restart -- run_entry.py re-derives the day (or resumes same-day
monitoring), run_monitor_exit.py just resumes polling.
"""

import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import notify
import position_store as ps
from strategy.calendar import WeekAction, resolve_entry_day_action

WATCH_FROM = time(9, 16)   # both scripts are launched by cron at 09:10
WATCH_UNTIL = time(15, 25)


def log(msg: str) -> None:
    print(f"[{datetime.now(config.IST).isoformat()}] {msg}", flush=True)


def expected_script(today, position) -> tuple[str, Path] | None:
    """(script name, its lock path) that should be alive right now, or None."""
    if position and position.get("traded") and position.get("status") == "OPEN":
        if position["entry_date"] == today.isoformat():
            return "run_entry.py", config.LOCK_DIR / f"entry_{today.isoformat()}.lock"
        return "run_monitor_exit.py", config.LOCK_DIR / f"monitor_{today.isoformat()}.lock"

    is_wed, is_thu = today.weekday() == 2, today.weekday() == 3
    if not (is_wed or is_thu):
        return None
    wednesday = today if is_wed else today - timedelta(days=1)
    resolved = bool(position and position.get("entry_date") == today.isoformat())
    action = resolve_entry_day_action(today, is_wed, is_thu, wednesday in config.NSE_HOLIDAYS,
                                      wednesday + timedelta(days=1) in config.NSE_HOLIDAYS, resolved)
    if action is WeekAction.TRADE_TODAY:
        return "run_entry.py", config.LOCK_DIR / f"entry_{today.isoformat()}.lock"
    return None


def main() -> int:
    now = datetime.now(config.IST)
    today = now.date()
    if today.weekday() >= 5 or today in config.NSE_HOLIDAYS or not (WATCH_FROM <= now.time() < WATCH_UNTIL):
        return 0

    expected = expected_script(today, ps.read_position())
    if expected is None:
        return 0
    script, lock_path = expected
    if ps.holder_pid_alive(lock_path):
        return 0

    script_path = config.BASE_DIR / "scripts" / script
    log_path = config.BASE_DIR / "logs" / script.replace(".py", ".log")
    with open(log_path, "a") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(script_path)], cwd=config.BASE_DIR,
            stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
        )
    log(f"{script} was NOT running -- restarted it (pid {proc.pid}).")
    notify.send(f"[DEAD] wednesday_nifty {script} was not running at {now.strftime('%H:%M')} "
                f"-- restarted it (pid {proc.pid}). Check logs/{log_path.name}.", log=log)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        notify.send(f"[CRASH] wednesday_nifty watchdog.py crashed:\n{traceback.format_exc()[-3500:]}")
        raise
