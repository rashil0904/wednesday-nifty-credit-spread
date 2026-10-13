"""
Calendar/holiday logic: which day is the entry day this week, and where
the expiry/exit date lands. Pure logic, no I/O -- holiday status is
looked up by the caller (config.NSE_HOLIDAYS) and passed in.
"""

from datetime import date, timedelta
from enum import Enum


class WeekAction(Enum):
    TRADE_TODAY = "TRADE_TODAY"                      # proceed with entry today
    WAIT_NOT_YET = "WAIT_NOT_YET"                    # Wed is a holiday; shifts to Thu
    SKIP_HOLIDAY_STRETCH = "SKIP_HOLIDAY_STRETCH"    # Wed AND Thu both holidays
    ALREADY_RESOLVED = "ALREADY_RESOLVED"            # this week already traded/no-traded/skipped


def resolve_entry_day_action(
    today: date,
    is_wednesday: bool,
    is_thursday: bool,
    wednesday_is_holiday: bool,
    thursday_is_holiday: bool,
    week_already_resolved: bool,
) -> WeekAction:
    """Decides what today's run should do, given the Wed->Thu holiday-shift
    rule. A Wed+Thu holiday stretch is never auto-pushed to Friday --
    the week is skipped and flagged instead."""
    if week_already_resolved:
        return WeekAction.ALREADY_RESOLVED

    if is_wednesday:
        if wednesday_is_holiday:
            return WeekAction.WAIT_NOT_YET
        return WeekAction.TRADE_TODAY

    if is_thursday:
        if not wednesday_is_holiday:
            # Wednesday already was (or should have been) the entry day.
            return WeekAction.ALREADY_RESOLVED
        if thursday_is_holiday:
            return WeekAction.SKIP_HOLIDAY_STRETCH
        return WeekAction.TRADE_TODAY

    return WeekAction.WAIT_NOT_YET


def following_tuesday(entry_date: date, expiry_weekday: int) -> date:
    """The nearest date strictly after entry_date that falls on
    expiry_weekday (Tuesday=1 by default, date.weekday() numbering).
    Entry is always Wednesday(2) or Thursday(3), so this is always in
    [5, 6] days ahead for Wed, [4, 5] for Thu -- i.e. always next week,
    never today.

    This is a calendar anchor only: the REAL listed weekly expiry (which
    already reflects any NSE holiday shift) must be resolved live from
    the instrument master (broker/xts_client.py), picking the nearest
    expiry >= this date -- never trust this date alone as the expiry."""
    days_ahead = (expiry_weekday - entry_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return entry_date + timedelta(days=days_ahead)


def resolve_exit_date(expiry: date, holidays: set[date]) -> date:
    """If the expiry date (the Tuesday the position is held to) is itself
    an NSE holiday, exit on the previous trading day instead."""
    exit_date = expiry
    while exit_date in holidays or exit_date.weekday() >= 5:
        exit_date -= timedelta(days=1)
    return exit_date
