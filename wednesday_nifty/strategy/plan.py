"""
Assembles the 2-leg credit-spread entry plan from already-computed
signals (trigger, strikes, expiry, lot size). Pure logic, no I/O.
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum

from strategy.breakout import CALL_SPREAD_TRIGGERS, EntryTrigger


class OptionType(Enum):
    CE = "CE"
    PE = "PE"


class LegRole(Enum):
    SHORT_ATM = "SHORT_ATM"
    LONG_HEDGE = "LONG_HEDGE"


@dataclass(frozen=True)
class Leg:
    role: LegRole
    option_type: OptionType
    strike: float
    expiry: date
    quantity: int


@dataclass(frozen=True)
class EntryPlan:
    trigger: EntryTrigger
    option_type: OptionType
    atm_strike: float
    hedge_strike: float
    expiry: date
    legs: tuple[Leg, Leg]


def option_type_for_trigger(trigger: EntryTrigger) -> OptionType:
    return OptionType.CE if trigger in CALL_SPREAD_TRIGGERS else OptionType.PE


def hedge_direction_sign(trigger: EntryTrigger) -> int:
    """+1 (hedge strike = ATM + offset) for a Call Credit Spread,
    -1 (hedge strike = ATM - offset) for a Put Credit Spread."""
    return 1 if trigger in CALL_SPREAD_TRIGGERS else -1


def build_entry_plan(
    *,
    trigger: EntryTrigger,
    atm_strike: float,
    hedge_strike: float,
    expiry: date,
    lot_size: int,
    lots: int,
) -> EntryPlan:
    """LOW_BREAKOUT/TIME_1435_RED -> short ATM CE + long hedge CE (Call
    Credit Spread). HIGH_BREAKOUT/TIME_1435_GREEN -> short ATM PE + long
    hedge PE (Put Credit Spread). Both legs trade the same quantity (1:1
    defined-risk spread, not a ratio structure)."""
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if lots <= 0:
        raise ValueError(f"lots must be positive, got {lots}")

    option_type = option_type_for_trigger(trigger)
    quantity = lots * lot_size

    legs = (
        Leg(role=LegRole.SHORT_ATM, option_type=option_type, strike=atm_strike,
            expiry=expiry, quantity=quantity),
        Leg(role=LegRole.LONG_HEDGE, option_type=option_type, strike=hedge_strike,
            expiry=expiry, quantity=quantity),
    )

    return EntryPlan(
        trigger=trigger,
        option_type=option_type,
        atm_strike=atm_strike,
        hedge_strike=hedge_strike,
        expiry=expiry,
        legs=legs,
    )
