"""
Exit-monitor script for every trading day AFTER the entry day, while a
position is open. Polls NIFTY futures over REST (for the HIGH_BREAKOUT
stop loss) and both legs (for the profit target), and applies the
Tuesday (or holiday-shifted) time exit off the clock.

Meant to be started once each such morning (e.g. via cron at ~09:10
IST) and left running through market close -- it polls continuously
rather than being re-invoked every few minutes, matching run_entry.py's
own structure. The entry day itself is handled by run_entry.py's own
post-entry monitoring, not this script.

DRY RUN by default (JAINAM_LIVE=false in .env): polls real market data
and logs exactly what it would do; places no orders.

    python scripts/run_monitor_exit.py
"""

import sys
import time as time_module
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import notify
import position_store as ps
from broker.xts_client import XTSDataClient
from exit_executor import check_and_exit_if_triggered


def log(msg: str) -> None:
    print(f"[{datetime.now(config.IST).isoformat()}] {msg}")


def main() -> int:
    now = datetime.now(config.IST)
    today = now.date()

    if today.weekday() >= 5:
        log("Weekend -- nothing to do.")
        return 0
    if today in config.NSE_HOLIDAYS:
        log("Today is an NSE holiday -- nothing to do.")
        return 0

    position = ps.read_position()
    if position is None or not position.get("traded"):
        log("No position on record -- nothing to monitor.")
        return 0
    if position.get("status") != "OPEN":
        log(f"Position status is {position.get('status')} -- nothing to do.")
        return 0

    entry_date = date.fromisoformat(position["entry_date"])
    if entry_date >= today:
        log(f"Position was entered on {entry_date} -- today's monitoring is run_entry.py's job, not this script's.")
        return 0

    try:
        with ps.acquire_monitor_lock(today):
            return _monitor_today(now, today, position)
    except ps.LockHeldError:
        log("Lock already held for today (another monitor run in progress) -- exiting.")
        notify.send(f"[WARN] run_monitor_exit.py: monitor lock already held on {today} -- "
                     "another run in progress or a stuck prior run.")
        return 1


def _monitor_today(now: datetime, today: date, position: dict) -> int:
    client = XTSDataClient.from_env()

    _wait_until(config.MARKET_OPEN)

    log("Monitoring...")
    while True:
        now_ist = datetime.now(config.IST)
        if now_ist.time() >= config.MARKET_CLOSE:
            log("Market closed -- ending today's monitoring.")
            break
        current = ps.read_position()
        if current is None or current.get("status") != "OPEN":
            log("Position no longer OPEN -- stopping monitoring.")
            break
        exit_reason = check_and_exit_if_triggered(client, current, log=log)
        if exit_reason:
            log(f"Exited: {exit_reason}")
            break
        time_module.sleep(config.POLL_INTERVAL_SECONDS)

    return 0


def _wait_until(target_time) -> None:
    now = datetime.now(config.IST)
    if now.time() >= target_time:
        return
    target_dt = now.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)
    wait_seconds = (target_dt - now).total_seconds()
    log(f"Waiting {wait_seconds:.0f}s until {target_time}")
    time_module.sleep(max(0, wait_seconds))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        notify.send(f"[CRASH] run_monitor_exit.py crashed:\n{traceback.format_exc()[-3500:]}")
        raise
