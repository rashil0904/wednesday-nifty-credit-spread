"""
Jainam (XTS) order-execution layer -- dry-run only.

State handling (week/position JSON files: week_key_for, get_week_status,
is_week_resolved, set_week_status, load_position, get_open_position,
save_position, update_position) is pure JSON/file I/O with zero broker
coupling -- reused directly from zerodha.execution rather than duplicated
here.

Only the order-execution half is broker-specific, and it's dry-run only:
no real XTS order placement/cancel/status endpoint exists anywhere (not
in the Nifty-option-BTST repo this was ported from, not here), and none
of it can be verified without a live Jainam session. config.py already
hard-blocks BROKER=jainam + DRY_RUN=false at import time, so place_leg's
dry_run=False branch below should be unreachable in practice -- it raises
NotImplementedError rather than silently doing nothing, in case that
guard is ever bypassed.

Signature note: enter_spread here takes OptionInstrument objects (needs
.instrument_token for XTS quotes), not bare tradingsymbol strings like
zerodha.execution.enter_spread (Kite can quote directly by tradingsymbol).
monitor.py's _execute_entry branches on config.BROKER for this one call.
exit_spread takes the same `position` dict either way and re-resolves
each leg's instrument_token from the saved strike/expiry/direction, since
Jainam has no cached instrument-master dump to look a tradingsymbol back
up in (unlike Kite's get_nfo_instruments).
"""
from typing import Optional

from .. import strategy
from ..logger import get_logger
from ..zerodha.execution import (  # noqa: F401 -- broker-agnostic state I/O, reused as-is
    week_key_for,
    get_week_status,
    is_week_resolved,
    set_week_status,
    load_position,
    get_open_position,
    save_position,
    update_position,
)
from . import broker
from .client import NSE_FO_SEGMENT, XTSDataClient

logger = get_logger("jainam.execution")


def get_mid_price(client: XTSDataClient, instrument_token: int) -> float:
    """XTS quotes give LTP via the ported client, not book depth -- falls
    back to LTP directly rather than a true bid/ask mid."""
    return client.get_ltp(NSE_FO_SEGMENT, instrument_token)


def place_leg(client: XTSDataClient, instrument_token: int, transaction_type: str,
              quantity: int, dry_run: bool = True) -> dict:
    """Returns {"status": "COMPLETE"|"REJECTED", "average_price": float,
    "order_id": str}. Only dry_run=True is implemented -- see module
    docstring."""
    if not dry_run:
        raise NotImplementedError(
            "Jainam live order placement is not implemented -- no XTS order "
            "endpoint code exists or has been verified against a live session."
        )

    mid = get_mid_price(client, instrument_token)
    logger.info(
        "[DRY_RUN] Would place %s instrument_token=%s x%d near LTP %.2f",
        transaction_type, instrument_token, quantity, mid,
    )
    return {"status": "COMPLETE", "average_price": mid, "order_id": "DRY_RUN"}


def check_margin(client: XTSDataClient, sell_instrument_token: int, buy_instrument_token: int,
                  quantity: int, dry_run: bool) -> bool:
    """XTS margin-check endpoint isn't in the ported client and is out of
    scope for this dry-run-only pass -- always passes."""
    return True


def enter_spread(client: XTSDataClient, sell_instrument, buy_instrument,
                  quantity: int, dry_run: bool) -> dict:
    """BUY (long) leg first, then SELL (short) leg -- same sequencing as
    zerodha.execution.enter_spread. Takes OptionInstrument objects (needs
    .instrument_token), not tradingsymbol strings -- see module docstring."""
    if not check_margin(client, sell_instrument.instrument_token, buy_instrument.instrument_token,
                         quantity, dry_run):
        return {"status": "ABORTED_MARGIN"}

    buy_fill = place_leg(client, buy_instrument.instrument_token, "BUY", quantity, dry_run)
    if buy_fill["status"] != "COMPLETE":
        logger.error("Entry BUY leg failed to fill — no position taken, aborting entry")
        return {"status": "ABORTED_NO_FILL", "leg": "BUY"}

    sell_fill = place_leg(client, sell_instrument.instrument_token, "SELL", quantity, dry_run)
    if sell_fill["status"] != "COMPLETE":
        logger.critical(
            "SELL leg failed — BUY leg is filled and left OPEN as a naked long. "
            "PARTIAL position, manual intervention required."
        )
        return {"status": "PARTIAL", "buy_fill": buy_fill["average_price"], "sell_fill": None}

    return {
        "status": "FILLED",
        "buy_fill": buy_fill["average_price"],
        "sell_fill": sell_fill["average_price"],
    }


def _resolve_leg_instrument_token(client: XTSDataClient, position: dict, leg_key: str) -> Optional[int]:
    """Re-resolves a saved leg's instrument_token from its persisted
    strike + the position's expiry_date/direction -- there's no
    tradingsymbol->token lookup available from the ported client alone
    (unlike Kite's cached full instrument dump)."""
    from datetime import date as _date

    leg = position[leg_key]
    if leg.get("fill_price") is None:
        return None
    expiry = _date.fromisoformat(position["expiry_date"])
    option_type = strategy.option_type_for_direction(position["direction"])
    instrument = broker.resolve_option(client, expiry, leg["strike"], option_type)
    return instrument.instrument_token


def exit_spread(client: XTSDataClient, position: dict, dry_run: bool) -> dict:
    """BUY-to-close the SELL/short leg first, then SELL-to-close the
    BUY/long leg -- same sequencing and same `position` dict shape as
    zerodha.execution.exit_spread."""
    quantity = position["quantity"]
    has_short_leg = position["sell_leg"].get("fill_price") is not None

    close_short_fill = None
    if not has_short_leg:
        logger.info("Position has no filled short leg (PARTIAL entry) — closing long leg only")
    else:
        sell_token = _resolve_leg_instrument_token(client, position, "sell_leg")
        close_short_fill = place_leg(client, sell_token, "BUY", quantity, dry_run)
        if close_short_fill["status"] != "COMPLETE":
            logger.critical(
                "Failed to close SHORT leg (%s) — short remains OPEN. This is the "
                "higher-risk failure mode; manual intervention required immediately.",
                position["sell_leg"]["tradingsymbol"],
            )
            return {"status": "PARTIAL_EXIT", "short_closed": False}

    buy_token = _resolve_leg_instrument_token(client, position, "buy_leg")
    close_long_fill = place_leg(client, buy_token, "SELL", quantity, dry_run)
    if close_long_fill["status"] != "COMPLETE":
        logger.critical(
            "Short leg closed but failed to close LONG leg (%s) — long remains open "
            "(defined-risk, just a cost). Manual intervention required.",
            position["buy_leg"]["tradingsymbol"],
        )
        return {
            "status": "PARTIAL_EXIT",
            "short_closed": True,
            "close_short_fill": close_short_fill["average_price"] if close_short_fill else None,
            "long_closed": False,
        }

    return {
        "status": "CLOSED",
        "close_short_fill": close_short_fill["average_price"] if close_short_fill else None,
        "close_long_fill": close_long_fill["average_price"],
    }
