"""
Position state and order placement, together: you can't place an order
without checking/updating state, so these stay in one file.

  - State: positions_wednesday_nifty.json (single open/closed position) and
    weekly_state.json (per-week resolution status, used for the
    one-trade-per-Wednesday guard and the Wed->Thu holiday shift).
  - Execution: leg sequencing, limit->market fallback, margin check,
    dry-run simulation.

Leg sequencing (Q2 proposed default):
  Entry:  BUY (long/hedge) leg first, then SELL (short) leg. A failed BUY
          leaves no position at all. A failed SELL leaves a naked LONG
          (defined-risk, just a cost) -- never a naked short.
  Exit:   BUY-to-close the SELL/short leg first (removes the larger risk
          fastest), then SELL-to-close the BUY/long leg.

Order type (Q5 proposed default):
  LIMIT at mid + small buffer, held for config.ORDER_LIMIT_WAIT_SECONDS,
  then cancelled and replaced with MARKET if still unfilled.
"""
import json
import time as time_module
from datetime import date, timedelta
from typing import Optional

from .. import config
from ..logger import get_logger

logger = get_logger("execution")

EXCHANGE = "NFO"
PRODUCT = "NRML"  # Q4: multi-day hold requires carryforward, never MIS
TICK_SIZE = 0.05

RESOLVED_WEEK_STATUSES = {"TRADED", "NO_TRADE", "SKIPPED_ROLLOVER", "SKIPPED_HOLIDAY_STRETCH"}


# =============================================================================
# State
# =============================================================================

def week_key_for(d: date) -> str:
    """The Wednesday date of the ISO week containing `d`, as an isoformat
    string. Both a Wednesday and its holiday-shifted Thursday map to the
    same key."""
    iso_weekday = d.isoweekday()  # Mon=1 ... Sun=7, Wed=3, Thu=4
    wednesday = d - timedelta(days=iso_weekday - 3)
    return wednesday.isoformat()


def _load_weekly_state() -> dict:
    if not config.WEEKLY_STATE_FILE.exists():
        return {}
    return json.loads(config.WEEKLY_STATE_FILE.read_text())


def _save_weekly_state(state: dict) -> None:
    config.WEEKLY_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_week_status(week_key: str) -> Optional[str]:
    return _load_weekly_state().get(week_key, {}).get("status")


def is_week_resolved(week_key: str) -> bool:
    return get_week_status(week_key) in RESOLVED_WEEK_STATUSES


def set_week_status(week_key: str, status: str, **extra) -> None:
    weekly_state = _load_weekly_state()
    entry = weekly_state.get(week_key, {})
    entry.update({"status": status, **extra})
    weekly_state[week_key] = entry
    _save_weekly_state(weekly_state)
    logger.info("Week %s status -> %s (%s)", week_key, status, extra)


def load_position() -> Optional[dict]:
    if not config.POSITIONS_FILE.exists():
        return None
    return json.loads(config.POSITIONS_FILE.read_text()) or None


def get_open_position() -> Optional[dict]:
    pos = load_position()
    if pos and pos.get("status") in ("OPEN", "PARTIAL"):
        return pos
    return None


def save_position(position: dict) -> None:
    config.POSITIONS_FILE.write_text(json.dumps(position, indent=2, default=str))


def update_position(**fields) -> dict:
    pos = load_position() or {}
    pos.update(fields)
    save_position(pos)
    return pos


# =============================================================================
# Order execution
# =============================================================================

def get_mid_price(kite, tradingsymbol: str) -> float:
    key = f"{EXCHANGE}:{tradingsymbol}"
    quote = kite.quote([key])[key]
    depth = quote.get("depth", {})
    buy_levels = depth.get("buy", [])
    sell_levels = depth.get("sell", [])
    if buy_levels and sell_levels and buy_levels[0]["price"] and sell_levels[0]["price"]:
        return round((buy_levels[0]["price"] + sell_levels[0]["price"]) / 2, 2)
    return quote["last_price"]


def _buffered_limit_price(mid: float, transaction_type: str) -> float:
    buffer = max(TICK_SIZE, round(mid * 0.001, 2))
    price = mid + buffer if transaction_type == "BUY" else mid - buffer
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


def _wait_for_fill(kite, order_id: str, timeout_seconds: int) -> Optional[dict]:
    deadline = time_module.monotonic() + timeout_seconds
    while time_module.monotonic() < deadline:
        history = kite.order_history(order_id)
        last = history[-1]
        if last["status"] == "COMPLETE":
            return last
        if last["status"] in ("REJECTED", "CANCELLED"):
            return None
        time_module.sleep(1)
    return None


def place_leg(kite, tradingsymbol: str, transaction_type: str, quantity: int,
              dry_run: bool = True) -> dict:
    """Places one leg with LIMIT->MARKET fallback. Returns
    {"status": "COMPLETE"|"REJECTED", "average_price": float, "order_id": str}."""
    mid = get_mid_price(kite, tradingsymbol)

    if dry_run:
        logger.info(
            "[DRY_RUN] Would place %s %s x%d near mid %.2f", transaction_type,
            tradingsymbol, quantity, mid,
        )
        return {"status": "COMPLETE", "average_price": mid, "order_id": "DRY_RUN"}

    limit_price = _buffered_limit_price(mid, transaction_type)
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=EXCHANGE,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=PRODUCT,
        order_type=kite.ORDER_TYPE_LIMIT,
        price=limit_price,
    )
    logger.info("Placed LIMIT %s %s x%d @ %.2f (order_id=%s)",
                transaction_type, tradingsymbol, quantity, limit_price, order_id)

    filled = _wait_for_fill(kite, order_id, config.ORDER_LIMIT_WAIT_SECONDS)
    if filled:
        return {"status": "COMPLETE", "average_price": filled["average_price"], "order_id": order_id}

    logger.warning(
        "LIMIT order %s not filled within %ds, cancelling and switching to MARKET",
        order_id, config.ORDER_LIMIT_WAIT_SECONDS,
    )
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    except Exception:
        logger.exception("Failed to cancel unfilled LIMIT order %s (may have filled just now)", order_id)

    market_order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=EXCHANGE,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=PRODUCT,
        order_type=kite.ORDER_TYPE_MARKET,
    )
    logger.info("Placed MARKET %s %s x%d (order_id=%s)", transaction_type, tradingsymbol, quantity, market_order_id)

    filled = _wait_for_fill(kite, market_order_id, config.ORDER_LIMIT_WAIT_SECONDS)
    if filled:
        return {"status": "COMPLETE", "average_price": filled["average_price"], "order_id": market_order_id}

    return {"status": "REJECTED", "average_price": None, "order_id": market_order_id}


def check_margin(kite, sell_symbol: str, buy_symbol: str, quantity: int, dry_run: bool) -> bool:
    if not config.MARGIN_CHECK_ENABLED:
        return True
    try:
        basket = [
            {"exchange": EXCHANGE, "tradingsymbol": buy_symbol, "transaction_type": "BUY",
             "variety": "regular", "product": PRODUCT, "order_type": "MARKET", "quantity": quantity},
            {"exchange": EXCHANGE, "tradingsymbol": sell_symbol, "transaction_type": "SELL",
             "variety": "regular", "product": PRODUCT, "order_type": "MARKET", "quantity": quantity},
        ]
        margin_result = kite.order_margins(basket)
        required = sum(item["total"] for item in margin_result)
        available = kite.margins()["equity"]["available"]["live_balance"]
        logger.info("Margin check: required=%.2f available=%.2f", required, available)
        if required > available:
            logger.error("Insufficient margin: required %.2f > available %.2f", required, available)
            return False
        return True
    except Exception:
        logger.exception("Margin check failed (API error) — treating as a hard stop, not a silent pass")
        return False if not dry_run else True


def enter_spread(kite, sell_symbol: str, buy_symbol: str, quantity: int, dry_run: bool) -> dict:
    """BUY (long) leg first, then SELL (short) leg. See module docstring."""
    if not check_margin(kite, sell_symbol, buy_symbol, quantity, dry_run):
        return {"status": "ABORTED_MARGIN"}

    buy_fill = place_leg(kite, buy_symbol, "BUY", quantity, dry_run)
    if buy_fill["status"] != "COMPLETE":
        logger.error("Entry BUY leg failed to fill — no position taken, aborting entry")
        return {"status": "ABORTED_NO_FILL", "leg": "BUY"}

    sell_fill = None
    for attempt in range(1, config.LEG_RETRY_COUNT + 1):
        sell_fill = place_leg(kite, sell_symbol, "SELL", quantity, dry_run)
        if sell_fill["status"] == "COMPLETE":
            break
        logger.warning("Entry SELL leg attempt %d/%d failed", attempt, config.LEG_RETRY_COUNT)
        time_module.sleep(config.LEG_RETRY_DELAY_SECONDS)

    if not sell_fill or sell_fill["status"] != "COMPLETE":
        logger.critical(
            "SELL leg failed after %d attempts — BUY leg (%s) is filled and left OPEN as a "
            "naked long. PARTIAL position, manual intervention required.",
            config.LEG_RETRY_COUNT, buy_symbol,
        )
        return {"status": "PARTIAL", "buy_fill": buy_fill["average_price"], "sell_fill": None}

    return {
        "status": "FILLED",
        "buy_fill": buy_fill["average_price"],
        "sell_fill": sell_fill["average_price"],
    }


def exit_spread(kite, position: dict, dry_run: bool) -> dict:
    """BUY-to-close the SELL/short leg first, then SELL-to-close the BUY/long leg.

    If the position is a PARTIAL entry (short leg never filled -- see
    enter_spread), there is no short leg to close; this skips straight to
    closing the long leg."""
    quantity = position["quantity"]
    sell_symbol = position["sell_leg"]["tradingsymbol"]
    buy_symbol = position["buy_leg"]["tradingsymbol"]
    has_short_leg = position["sell_leg"].get("fill_price") is not None

    close_short_fill = None
    if not has_short_leg:
        logger.info("Position has no filled short leg (PARTIAL entry) — closing long leg only")
    else:
        for attempt in range(1, config.LEG_RETRY_COUNT + 1):
            close_short_fill = place_leg(kite, sell_symbol, "BUY", quantity, dry_run)
            if close_short_fill["status"] == "COMPLETE":
                break
            logger.warning("Exit close-short attempt %d/%d failed", attempt, config.LEG_RETRY_COUNT)
            time_module.sleep(config.LEG_RETRY_DELAY_SECONDS)

        if not close_short_fill or close_short_fill["status"] != "COMPLETE":
            logger.critical(
                "Failed to close SHORT leg (%s) after %d attempts — short remains OPEN. "
                "This is the higher-risk failure mode; manual intervention required immediately.",
                sell_symbol, config.LEG_RETRY_COUNT,
            )
            return {"status": "PARTIAL_EXIT", "short_closed": False}

    close_long_fill = None
    for attempt in range(1, config.LEG_RETRY_COUNT + 1):
        close_long_fill = place_leg(kite, buy_symbol, "SELL", quantity, dry_run)
        if close_long_fill["status"] == "COMPLETE":
            break
        logger.warning("Exit close-long attempt %d/%d failed", attempt, config.LEG_RETRY_COUNT)
        time_module.sleep(config.LEG_RETRY_DELAY_SECONDS)

    if not close_long_fill or close_long_fill["status"] != "COMPLETE":
        logger.critical(
            "Short leg closed but failed to close LONG leg (%s) after %d attempts — "
            "long remains open (defined-risk, just a cost). Manual intervention required.",
            buy_symbol, config.LEG_RETRY_COUNT,
        )
        return {
            "status": "PARTIAL_EXIT",
            "short_closed": True,
            "close_short_fill": close_short_fill["average_price"],
            "long_closed": False,
        }

    return {
        "status": "CLOSED",
        "close_short_fill": close_short_fill["average_price"] if close_short_fill else None,
        "close_long_fill": close_long_fill["average_price"],
    }
