import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog  # noqa: E402

WED = date(2026, 10, 28)
THU = date(2026, 10, 29)


def _open(entry_date):
    return {"entry_date": entry_date.isoformat(), "traded": True, "status": "OPEN"}


def test_unresolved_wednesday_expects_run_entry():
    script, lock = watchdog.expected_script(WED, None)
    assert script == "run_entry.py" and lock.name == "entry_2026-10-28.lock"


def test_position_entered_today_expects_run_entry_to_keep_monitoring():
    assert watchdog.expected_script(WED, _open(WED))[0] == "run_entry.py"


def test_position_from_an_earlier_day_expects_run_monitor_exit():
    script, lock = watchdog.expected_script(THU, _open(WED))
    assert script == "run_monitor_exit.py" and lock.name == "monitor_2026-10-29.lock"


def test_no_trade_day_resolved_expects_nothing():
    no_trade = {"entry_date": WED.isoformat(), "traded": False}
    assert watchdog.expected_script(WED, no_trade) is None


def test_closed_position_on_a_later_day_expects_nothing():
    closed = {"entry_date": WED.isoformat(), "traded": True, "status": "CLOSED"}
    assert watchdog.expected_script(THU, closed) is None
