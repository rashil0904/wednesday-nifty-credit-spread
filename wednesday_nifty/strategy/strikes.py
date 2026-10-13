"""
ATM/hedge strike calculation. Pure logic, no I/O.

The strike interval itself (e.g. 50 points) must be resolved by the
caller from the live instrument master at runtime -- it is passed in
here, never hardcoded.
"""

import math


def snap_to_strike(price: float, strike_interval: float) -> float:
    """Snap an arbitrary price to the nearest point on the strike grid.
    Ties round up, matching common trading-system convention rather than
    Python's banker's rounding."""
    if strike_interval <= 0:
        raise ValueError(f"strike_interval must be positive, got {strike_interval}")
    steps = math.floor(price / strike_interval + 0.5)
    return steps * strike_interval


def calculate_atm_strike(spot_price: float, strike_interval: float) -> float:
    """ATM = NIFTY spot price rounded to the nearest valid strike."""
    return snap_to_strike(spot_price, strike_interval)


def calculate_hedge_strike(
    atm_strike: float, offset_points: float, strike_interval: float, *, direction_sign: int
) -> float:
    """
    Hedge strike = ATM +/- offset_points, same option type, snapped to
    the strike grid.

    direction_sign: +1 for a Call Credit Spread (hedge CE further OTM,
    i.e. ATM + offset), -1 for a Put Credit Spread (hedge PE further
    OTM, i.e. ATM - offset). Callers derive the sign from the
    EntryTrigger (see strategy/plan.py) -- this module doesn't know
    about triggers, just the grid math.
    """
    if direction_sign not in (1, -1):
        raise ValueError(f"direction_sign must be 1 or -1, got {direction_sign}")
    raw = atm_strike + (direction_sign * offset_points)
    return snap_to_strike(raw, strike_interval)
