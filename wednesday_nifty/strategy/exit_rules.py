"""
Exit conditions: profit target (checked on every tick of either leg) and
the Tuesday time exit (checked off the clock, never off ticks). Pure
logic, no I/O. There is deliberately NO stop loss anywhere -- do not add
one here or elsewhere.
"""

from datetime import date, datetime, time


def profit_target_hit(entry_credit: float, current_spread_value: float, target_pct: float = 0.90) -> bool:
    """entry_credit = net credit received at entry (short fill - long
    fill), i.e. max profit. current_spread_value = short_ltp - long_ltp,
    i.e. what it would cost to close right now. Target is hit once the
    captured fraction of max profit -- (entry_credit -
    current_spread_value) / entry_credit -- reaches target_pct.

    Computed as a fraction (not `current <= entry_credit * (1 -
    target_pct)`) to avoid float-precision misses exactly at the
    boundary, e.g. 70.0 * (1 - 0.90) != 7.0 in IEEE754."""
    if entry_credit <= 0:
        return False
    captured = (entry_credit - current_spread_value) / entry_credit
    return captured >= target_pct


def time_exit_due(now: datetime, exit_date: date, exit_time: time) -> bool:
    """True once `now` is on or after exit_date at exit_time IST."""
    if now.date() < exit_date:
        return False
    if now.date() > exit_date:
        return True
    return now.time() >= exit_time
