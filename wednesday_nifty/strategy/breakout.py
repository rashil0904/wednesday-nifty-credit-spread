"""
Entry-day signal: the 3-day breakout check (evaluated on every tick from
09:15-14:35) and the 14:35 red/green fallback (evaluated once, off the
clock, never off ticks). Pure logic, no I/O.

EntryTrigger is the single source of truth for which of the 4 possible
entries happened -- used both to pick the option side (CE/PE, Call vs
Put Credit Spread) and, downstream, to decide whether a stop loss
applies at all (HIGH_BREAKOUT only -- see strategy/stop_loss.py).
"""

from enum import Enum

from strategy.levels import ThreeDayLevels


class EntryTrigger(Enum):
    LOW_BREAKOUT = "LOW_BREAKOUT"          # futures < 3D_LOW -> Call Credit Spread
    HIGH_BREAKOUT = "HIGH_BREAKOUT"        # futures > 3D_HIGH -> Put Credit Spread
    TIME_1435_RED = "TIME_1435_RED"        # 14:35 fallback, below 09:15 open -> Call Credit Spread
    TIME_1435_GREEN = "TIME_1435_GREEN"    # 14:35 fallback, above 09:15 open -> Put Credit Spread


# Which side of the market each trigger implies -- CE side (Call Credit
# Spread: short ATM CE, hedge CE further OTM) or PE side (Put Credit
# Spread: short ATM PE, hedge PE further OTM).
CALL_SPREAD_TRIGGERS = frozenset({EntryTrigger.LOW_BREAKOUT, EntryTrigger.TIME_1435_RED})
PUT_SPREAD_TRIGGERS = frozenset({EntryTrigger.HIGH_BREAKOUT, EntryTrigger.TIME_1435_GREEN})


def check_breakout(ltp: float, levels: ThreeDayLevels) -> EntryTrigger | None:
    """First breach wins. `3D_LOW < 3D_HIGH` always holds for valid data,
    so a single tick can never satisfy both conditions -- no priority
    ordering is needed between them."""
    if ltp < levels.low:
        return EntryTrigger.LOW_BREAKOUT
    if ltp > levels.high:
        return EntryTrigger.HIGH_BREAKOUT
    return None


def fallback_trigger(price_at_1435: float, open_0915: float) -> EntryTrigger | None:
    """14:35 fallback: below the 09:15 open (red) -> TIME_1435_RED/Call
    Credit Spread; above (green) -> TIME_1435_GREEN/Put Credit Spread;
    exactly equal -> None (no trade)."""
    if price_at_1435 < open_0915:
        return EntryTrigger.TIME_1435_RED
    if price_at_1435 > open_0915:
        return EntryTrigger.TIME_1435_GREEN
    return None
