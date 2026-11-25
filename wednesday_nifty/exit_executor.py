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

import time as time_module
from datetime import datetime

import config
import notify
import position_store as ps
from broker.order_exec import execute_order
from broker.xts_client import NSE_FO_SEGMENT
from poll_guard import PollGuard
from results import build_pnl
from results.trade_history import append_closed_trade
from strategy.breakout import EntryTrigger
from strategy.exit_rules import profit_target_hit, time_exit_due
from strategy.stop_loss import should_stop_out

CLOSE_ORDER = (("SHORT_ATM", "BUY"), ("LONG_HEDGE", "SELL"))


def _now() -> datetime:
    return datetime.now(config.IST)


def check_and_exit_if_triggered(client, position: dict, log=print) -> str | None:
    """One evaluation pass (call once per tick/poll iteration). Returns
    the exit_reason if an exit fired and completed (or was already in
    progress), else None."""
    if position.get("status") != "OPEN":
        return None

    trigger = EntryTrigger(position["trigger"])
    legs = {leg["role"]: leg for leg in position["legs"]}
    futures_token = position.get("futures_instrument_token")

    now = _now()
    today = now.date().isoformat()

    # --- stop loss (HIGH_BREAKOUT only, once both legs are filled) ---
    # Checked ONCE a day at SL_CHECK_TIME (15:28), not every tick: the
    # futures price at 15:28 decides it (user rule, 2026-11-04). The first
    # poll at/after 15:28 evaluates it; a failed price fetch is retried on
    # the next poll since sl_checked_on is only written after a check.
    if (
        config.HIGH_BREAKOUT_SL_ENABLED
        and position.get("legs_filled")
        and position.get("sl_level") is not None
        and futures_token is not None
        and now.time() >= config.SL_CHECK_TIME
        and position.get("sl_checked_on") != today
    ):
        if _rollover_check_blocks_sl(client, position, log):
            pass  # flagged and logged; fall through to profit-target/time-exit only
        else:
            futures_ltp = client.get_ltp(NSE_FO_SEGMENT, futures_token)
            ps.update_position(sl_checked_on=today)
            if should_stop_out(trigger, position["sl_level"], futures_ltp):
                log(f"STOP_LOSS triggered at {config.SL_CHECK_TIME}: futures_ltp={futures_ltp} "
                    f"sl_level={position['sl_level']}")
                return _try_exit(client, "STOP_LOSS", log)
            log(f"{config.SL_CHECK_TIME} stop-loss check: futures_ltp={futures_ltp} >= "
                f"sl_level={position['sl_level']} -- holding.")

    # --- profit target (every day the position is open, once legs are filled) ---
    # Positions without a stop loss (call spreads) check it ONCE a day at
    # TARGET_CHECK_TIME -- no quote calls (so no token re-logins) before then.
    once_a_day_target = position.get("sl_level") is None
    target_due = (not once_a_day_target) or (
        now.time() >= config.TARGET_CHECK_TIME and position.get("target_checked_on") != today
    )
    if position.get("legs_filled") and position.get("entry_credit") and target_due:
        short_ltp = client.get_ltp(NSE_FO_SEGMENT, legs["SHORT_ATM"]["instrument_token"])
        hedge_ltp = client.get_ltp(NSE_FO_SEGMENT, legs["LONG_HEDGE"]["instrument_token"])
        current_value = short_ltp - hedge_ltp
        if once_a_day_target:
            ps.update_position(target_checked_on=today)
        if profit_target_hit(position["entry_credit"], current_value, config.PROFIT_TARGET_PCT):
            log(f"PROFIT_TARGET hit: current_value={current_value} entry_credit={position['entry_credit']}")
            return _try_exit(client, "PROFIT_TARGET", log)

    # --- time exit (off the clock, never off ticks) ---
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
        if not position.get("rollover_flag_notified"):
            notify.send(
                f"[FLAG] Futures rolled since entry while a HIGH_BREAKOUT stop loss "
                f"(sl_level={position['sl_level']}) is still active -- SL check is being "
                f"skipped, NOT silently remapped to the new contract. Needs your decision "
                f"on how the SL should map to {current_future['tradingsymbol']}.",
                log=log,
            )
            ps.update_position(rollover_flag_notified=True)
        return True
    return False


def monitor_until_close(client, name: str, log=print, sleep=time_module.sleep) -> str:
    """Polls check_and_exit_if_triggered until market close or the
    position stops being OPEN. A failed poll (network blip, broker
    error) is logged/alerted via PollGuard and retried next poll -- it
    never ends monitoring. Returns why it stopped: "MARKET_CLOSED",
    "EXITED" or "NOT_OPEN"."""
    guard = PollGuard(name, log=log)
    while True:
        if datetime.now(config.IST).time() >= config.MARKET_CLOSE:
            log("Market closed -- ending today's monitoring.")
            return "MARKET_CLOSED"
        position = ps.read_position()
        if position is None or position.get("status") != "OPEN":
            status = position and position.get("status")
            log(f"Position status is {status} -- stopping monitoring.")
            if status != "CLOSED":
                notify.send(f"[CRITICAL] {name}: stopped monitoring, position status is {status} "
                            "(not OPEN or CLOSED). Review manually.", log=log)
            return "NOT_OPEN"
        try:
            exit_reason = check_and_exit_if_triggered(client, position, log=log)
        except Exception as exc:  # noqa: BLE001 -- one bad poll must not end monitoring
            guard.failed(exc)
            sleep(config.POLL_INTERVAL_SECONDS)
            continue
        guard.succeeded()
        if exit_reason:
            log(f"Exited: {exit_reason}")
            return "EXITED"
        sleep(config.POLL_INTERVAL_SECONDS)


def _try_exit(client, reason: str, log) -> str | None:
    with ps.acquire_exit_lock():
        if not ps.begin_exit(reason):
            log(f"Exit '{reason}' lost the race -- another exit already won. Nothing to do.")
            return None
        position = ps.read_position()
        if not position.get("exit_retrying"):
            notify.send(f"[EXIT_SIGNAL] {reason} triggered -- closing position.", log=log)
        try:
            _close_position(client, position, reason, log)
        except Exception:
            # Back to OPEN so the next poll retries the exit (PollGuard
            # alerts on the failure). Safe against double-closing: legs
            # already closed are in closed_roles, and each leg's login-net
            # check refuses a leg our login already bought/sold back today.
            if (ps.read_position() or {}).get("status") == "EXITING":
                ps.update_position(status="OPEN", exit_retrying=True)
            raise
    return reason


def _close_position(client, position: dict, reason: str, log) -> None:
    """BUY back the short leg first, then SELL the long hedge leg.
    Closing the short first removes the riskier leg; if it can't be
    closed, the hedge leg is left in place (still covering it) and this
    stops rather than leaving a naked short. Dry run (JAINAM_LIVE=false)
    logs what it would do and places nothing."""
    legs = {leg["role"]: leg for leg in position["legs"]}
    closed = set(position.get("closed_roles", []))
    tag_prefix = f"X{position['entry_date'].replace('-', '')}"
    mode = "LIVE" if config.JAINAM_LIVE else "DRY RUN"
    fill_notes = []

    for role, side in CLOSE_ORDER:
        leg = legs.get(role)
        if leg is None or role in closed:
            continue

        if not config.JAINAM_LIVE:
            log(f"  [DRY_RUN] WOULD {side} {leg['quantity']} {leg['tradingsymbol']} to close {role}")
            fill_notes.append(f"{role}: WOULD {side} {leg['quantity']} {leg['tradingsymbol']}")
            closed.add(role)
            ps.update_position(closed_roles=sorted(closed))
            continue

        # Only our login's trades count (the client account is shared with
        # other logins). The trade book is today-only, so a leg entered on
        # an earlier day starts from its recorded quantity; today's trades
        # by our login (e.g. a manual close) then adjust it.
        expected_sign = -1 if role == "SHORT_ATM" else 1
        entered_today = position["entry_date"] == datetime.now(config.IST).date().isoformat()
        carried = 0 if entered_today else expected_sign * leg["quantity"]
        net = carried + client.get_login_net_quantity(leg["instrument_token"])
        if net * expected_sign < leg["quantity"]:
            log(f"!! {role}: {config.JAINAM_LOGIN_ID} net {net:+d} doesn't match recorded {leg['quantity']} -- "
                f"NOT trading this leg. Review manually.")
            notify.send(
                f"[CRITICAL] {reason} exit blocked: {role} {config.JAINAM_LOGIN_ID} net {net:+d} doesn't match "
                f"recorded {leg['quantity']} -- NOT trading this leg. Review manually.",
                log=log,
            )
            ps.update_position(status="OPEN")  # release the EXITING lock state for manual intervention
            return

        fill = execute_order(client, leg["instrument_token"], side, leg["quantity"],
                              tag_prefix + role[0], allow_market_fallback=True)
        if not fill.complete:
            log(f"!! {role} closed only {fill.filled}/{leg['quantity']} "
                f"({fill.rejected_reason or 'unfilled'}). Stopping -- remaining legs untouched.")
            notify.send(
                f"[CRITICAL] {reason} exit: {role} closed only {fill.filled}/{leg['quantity']} "
                f"({fill.rejected_reason or 'unfilled'}). Remaining legs untouched -- MANUAL REVIEW.",
                log=log,
            )
            return
        closed.add(role)
        ps.record_leg_exit(role, fill.filled, fill.avg_price)
        ps.update_position(closed_roles=sorted(closed))
        fill_notes.append(f"{role}: {side} {fill.filled} @ {fill.avg_price}")
        log(f"  closed {role}: {fill.filled} @ avg {fill.avg_price}")

    if closed >= set(legs):
        ps.update_position(status="CLOSED", closed_date=datetime.now(config.IST).date().isoformat())
        log("Exit complete.")
        notify.send(
            f"[EXIT] ({mode}) {reason} complete.\n" + "\n".join(fill_notes) +
            f"\nEntry credit was {position.get('entry_credit', 0):.2f}.",
            log=log,
        )
        if config.JAINAM_LIVE:
            try:
                final_position = ps.read_position()
                append_closed_trade(final_position)
                build_pnl.main()
            except Exception as exc:  # noqa: BLE001 -- PnL bookkeeping, must never fail a real exit
                log(f"    WARNING: PnL history/report sync failed ({exc}); trade closed fine regardless.")
