"""
Stop loss for HIGH_BREAKOUT entries only. Pure logic, no I/O.

Scope: applies ONLY when the position's entry trigger is HIGH_BREAKOUT
(the Put Credit Spread entered because NIFTY futures broke above
3D_HIGH). There is NO stop loss for LOW_BREAKOUT, TIME_1435_RED, or
TIME_1435_GREEN entries -- do not add one for them anywhere.
"""

from strategy.breakout import EntryTrigger


def should_stop_out(trigger: EntryTrigger, sl_level: float, futures_ltp: float) -> bool:
    """SL level = the 3D_HIGH that was breached at entry -- fixed at
    entry time, never recomputed daily (the caller reads it back from
    the position file across a restart, see position_store.py).

    Triggers when futures LTP is strictly below sl_level. NOT tick-level:
    the caller (exit_executor) evaluates this once a day, on the futures
    price at config.SL_CHECK_TIME (15:28) -- intraday dips that recover
    by 15:28 don't stop out (user rule, 2026-11-04)."""
    if trigger is not EntryTrigger.HIGH_BREAKOUT:
        return False
    return futures_ltp < sl_level
