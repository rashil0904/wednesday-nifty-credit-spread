"""
The shared "check every exit condition, close if one fires" procedure,
used by BOTH scripts/run_entry.py (same-day monitoring after a fill,
mainly for the HIGH_BREAKOUT stop loss) and scripts/run_monitor_exit.py
(every later day the position is open). One copy so the two call sites
can never drift apart on exit sequencing or the double-exit guard.

Exit priority on any one check: stop loss, then profit target, then
time exit -- but in practice at most one can plausibly fire on a given
check since begin_exit()'s OPEN->EXITING transition (done while holding
position_store.acquire_exit_lock()) means whichever condition is
evaluated and wins first locks out the others for good.
"""

from datetime import datetime

import config
import position_store as ps
from broker.market_stream import get_ltp_with_fallback
from broker.order_exec import execute_order
from broker.xts_client import NSE_FO_SEGMENT
from strategy.breakout import EntryTrigger
from strategy.exit_rules import profit_target_hit, time_exit_due
from strategy.stop_loss import should_stop_out

CLOSE_ORDER = (("SHORT_ATM", "BUY"), ("LONG_HEDGE", "SELL"))


def check_and_exit_if_triggered(client, stream, position: dict, log=print) -> str | None:
    """One evaluation pass (call once per tick/poll iteration). Returns
    the exit_reason if an exit fired and completed (or was already in
    progress), else None."""
    if position.get("status") != "OPEN":
        return None

    trigger = EntryTrigger(position["trigger"])
    legs = {leg["role"]: leg for leg in position["legs"]}
    futures_token = position.get("futures_instrument_token")

    # --- stop loss (HIGH_BREAKOUT only, only once both legs are confirmed filled) ---
    if (
        config.HIGH_BREAKOUT_SL_ENABLED
        and position.get("legs_filled")
        and position.get("sl_level") is not None
        and futures_token is not None
    ):
        if _rollover_check_blocks_sl(client, position, log):
            pass  # flagged and logged; fall through to profit-target/time-exit only
        else:
            futures_ltp = get_ltp_with_fallback(stream, client, NSE_FO_SEGMENT, futures_token)
            if should_stop_out(trigger, position["sl_level"], futures_ltp):
                log(f"STOP_LOSS triggered: futures_ltp={futures_ltp} sl_level={position['sl_level']}")
                return _try_exit(client, "STOP_LOSS", log)

    # --- profit target (every day the position is open, once legs are filled) ---
    if position.get("legs_filled") and position.get("entry_credit"):
        short_ltp = get_ltp_with_fallback(stream, client, NSE_FO_SEGMENT, legs["SHORT_ATM"]["instrument_token"])
        hedge_ltp = get_ltp_with_fallback(stream, client, NSE_FO_SEGMENT, legs["LONG_HEDGE"]["instrument_token"])
        current_value = short_ltp - hedge_ltp
        if profit_target_hit(position["entry_credit"], current_value, config.PROFIT_TARGET_PCT):
            log(f"PROFIT_TARGET hit: current_value={current_value} entry_credit={position['entry_credit']}")
            return _try_exit(client, "PROFIT_TARGET", log)

    # --- time exit (off the clock, never off ticks) ---
    now = datetime.now(config.IST)
    exit_date = datetime.strptime(position["exit_date"], "%Y-%m-%d").date()
    if time_exit_due(now, exit_date, config.EXIT_TIME):
        log(f"TIME_EXIT due: now={now.isoformat()} exit_date={exit_date}")
        return _try_exit(client, "TIME_EXIT", log)

    return None


def _rollover_check_blocks_sl(client, position: dict, log) -> bool:
    """FLAGS (does not silently resolve) if the near-month future has
    rolled since entry while a HIGH_BREAKOUT SL is active -- the SL
    level was set against the OLD contract's price action; comparing a
    NEW contract's LTP against it may not be meaningful. Per explicit
    instruction: ask, don't guess. Returns True if the SL check should
    be skipped this iteration."""
    today = datetime.now(config.IST).date()
    current_future = client.get_nifty_fut_instrument(today)
    if current_future["instrument_token"] != position["futures_instrument_token"]:
        log(
            f"!! FLAG: near-month future has rolled since entry "
            f"(was instrument_token={position['futures_instrument_token']}, now "
            f"{current_future['tradingsymbol']}/{current_future['instrument_token']}) while a "
            f"HIGH_BREAKOUT stop loss (sl_level={position['sl_level']}) is still active. "
            f"Skipping the SL check this cycle -- NOT silently switching contracts. "
            f"Please confirm how the SL should map to the new contract."
        )
        return True
    return False


def _try_exit(client, reason: str, log) -> str | None:
    with ps.acquire_exit_lock():
        if not ps.begin_exit(reason):
            log(f"Exit '{reason}' lost the race -- another exit already won. Nothing to do.")
            return None
        position = ps.read_position()
        _close_position(client, position, log)
    return reason


def _close_position(client, position: dict, log) -> None:
    """BUY back the short leg first, then SELL the long hedge leg.
    Closing the short first removes the riskier leg; if it can't be
    closed, the hedge leg is left in place (still covering it) and this
    stops rather than leaving a naked short. Dry run (JAINAM_LIVE=false)
    logs what it would do and places nothing."""
    legs = {leg["role"]: leg for leg in position["legs"]}
    closed = set(position.get("closed_roles", []))
    tag_prefix = f"X{position['entry_date'].replace('-', '')}"

    for role, side in CLOSE_ORDER:
        leg = legs.get(role)
        if leg is None or role in closed:
            continue

        if not config.JAINAM_LIVE:
            log(f"  [DRY_RUN] WOULD {side} {leg['quantity']} {leg['tradingsymbol']} to close {role}")
            closed.add(role)
            ps.update_position(closed_roles=sorted(closed))
            continue

        expected_sign = -1 if role == "SHORT_ATM" else 1
        net = client.get_net_quantity(leg["instrument_token"])
        if net * expected_sign < leg["quantity"]:
            log(f"!! {role}: account net {net:+d} doesn't match recorded {leg['quantity']} -- "
                f"NOT trading this leg. Review manually.")
            ps.update_position(status="OPEN")  # release the EXITING lock state for manual intervention
            return

        fill = execute_order(client, leg["instrument_token"], side, leg["quantity"],
                              tag_prefix + role[0], allow_market_fallback=True)
        if not fill.complete:
            log(f"!! {role} closed only {fill.filled}/{leg['quantity']} "
                f"({fill.rejected_reason or 'unfilled'}). Stopping -- remaining legs untouched.")
            return
        closed.add(role)
        ps.update_position(closed_roles=sorted(closed))
        log(f"  closed {role}: {fill.filled} @ avg {fill.avg_price}")

    if closed >= set(legs):
        ps.update_position(status="CLOSED", closed_date=datetime.now(config.IST).date().isoformat())
        log("Exit complete.")
