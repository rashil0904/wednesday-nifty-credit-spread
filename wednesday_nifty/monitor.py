"""
Entry and exit monitors, together: they share the same broker/ticker/state
plumbing and only differ in when they're triggered and what they check for.

No scheduler is wired up yet (no launchd/cron/systemd) -- both are started
by hand for now:

    python -m wednesday_nifty.monitor entry   # run manually on Wednesday/Thursday mornings
    python -m wednesday_nifty.monitor exit    # run manually, periodically, while a position may be open

--- Entry monitor ---
Meant to be started manually on both Wednesday and Thursday mornings
(~9:10am IST, before market open) -- this script itself decides, using
strategy.resolve_trading_day_action, whether today is the day it should
actually act on. See strategy.py / zerodha/broker.py for the Wed->Thu
holiday-shift rule and the two-day-holiday-stretch case (never
auto-resolved past Thursday). It runs to completion (either a trade is
entered, or the day is skipped/deferred) and then exits.

--- Exit monitor ---
A single-shot check: run it by hand, repeatedly, on any day a position may
be open (entry day through the following Tuesday). Each run no-ops
immediately and cheaply if there's no open position or the market is
closed. config.EXIT_MONITOR_POLL_SECONDS (5 minutes) documents the
recommended re-run cadence until this is wired up to a real scheduler.

--- Broker selection ---
config.BROKER picks which broker/execution module pair is active
("kite" -> zerodha.broker/zerodha.execution, "jainam" -> jainam.broker/
jainam.execution). Both pairs expose the same function names/signatures
(FuturesContract/OptionInstrument, resolve_*, enter_spread/exit_spread,
etc.) so everything below this point is written against `broker`/
`execution` generically and doesn't care which broker is active -- the
one exception is real-time breakout detection, which has two concrete
implementations (_run_ticker_loop for Kite's websocket, _run_poll_loop
for Jainam's REST-only client) selected in run_entry_monitor.
"""
import sys
import time as time_module
from datetime import date, datetime, timedelta

from . import config, strategy
from .logger import get_logger

if config.BROKER == "jainam":
    from .jainam import broker, execution
else:
    from .zerodha import broker, execution

logger = get_logger("monitor")


# =============================================================================
# Entry monitor
# =============================================================================

def _wait_until_market_open(now_ist: datetime) -> None:
    if now_ist.time() >= config.MARKET_OPEN:
        return
    open_dt = now_ist.replace(
        hour=config.MARKET_OPEN.hour, minute=config.MARKET_OPEN.minute, second=0, microsecond=0
    )
    wait_seconds = (open_dt - now_ist).total_seconds()
    logger.info("Waiting %.0fs for market open", wait_seconds)
    time_module.sleep(max(0, wait_seconds))


def _run_ticker_loop(kite, future: broker.FuturesContract,
                      levels_data: strategy.ThreeDayLevels) -> dict:
    """Real-time breakout monitoring via KiteTicker (websocket push, not
    polled REST -- sidesteps REST rate limits for the continuous part of
    the day). Falls back to a REST poll only if the socket is disconnected
    for longer than config.WS_RECONNECT_GRACE_SECONDS."""
    from kiteconnect import KiteTicker

    result = {"triggered": False, "direction": None, "futures_price": None}
    quote_key = f"NFO:{future.tradingsymbol}"

    kws = KiteTicker(config.KITE_API_KEY, kite.access_token)

    def on_ticks(ws, ticks):
        for tick in ticks:
            ltp = tick["last_price"]
            direction = strategy.detect_breakout(ltp, levels_data)
            if direction:
                result.update(triggered=True, direction=direction, futures_price=ltp)
                try:
                    ws.close()
                except Exception:
                    pass
                return

    def on_connect(ws, response):
        ws.subscribe([future.instrument_token])
        ws.set_mode(ws.MODE_LTP, [future.instrument_token])

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.connect(threaded=True)

    disconnected_since = None
    try:
        while not result["triggered"]:
            now_ist = datetime.now(config.IST)
            if now_ist.time() >= config.FALLBACK_CHECK_TIME or now_ist.time() >= config.MARKET_CLOSE:
                break

            if kws.is_connected():
                disconnected_since = None
                time_module.sleep(1)
                continue

            if disconnected_since is None:
                disconnected_since = time_module.monotonic()
            if time_module.monotonic() - disconnected_since >= config.WS_RECONNECT_GRACE_SECONDS:
                logger.warning("Ticker disconnected >%ds, using REST fallback poll",
                                config.WS_RECONNECT_GRACE_SECONDS)
                ltp = kite.ltp([quote_key])[quote_key]["last_price"]
                direction = strategy.detect_breakout(ltp, levels_data)
                if direction:
                    result.update(triggered=True, direction=direction, futures_price=ltp)
                    break
                time_module.sleep(config.REST_FALLBACK_POLL_SECONDS)
            else:
                time_module.sleep(1)
    finally:
        try:
            kws.close()
        except Exception:
            pass

    return result


def _run_poll_loop(client, future: broker.FuturesContract,
                    levels_data: strategy.ThreeDayLevels) -> dict:
    """Jainam/XTS equivalent of _run_ticker_loop: the ported client has no
    streaming/websocket support (see jainam/client.py's module docstring),
    so breakout detection here is a plain REST LTP poll every
    config.REST_FALLBACK_POLL_SECONDS -- same loop-exit conditions and
    return shape as the ticker loop, just without the websocket-push path."""
    result = {"triggered": False, "direction": None, "futures_price": None}

    while not result["triggered"]:
        now_ist = datetime.now(config.IST)
        if now_ist.time() >= config.FALLBACK_CHECK_TIME or now_ist.time() >= config.MARKET_CLOSE:
            break

        ltp = broker.get_latest_futures_price(client, future)
        direction = strategy.detect_breakout(ltp, levels_data)
        if direction:
            result.update(triggered=True, direction=direction, futures_price=ltp)
            break

        time_module.sleep(config.REST_FALLBACK_POLL_SECONDS)

    return result


def _execute_entry(kite, direction: strategy.Direction, entry_reason: strategy.EntryReason,
                    futures_price_at_trigger: float, expiry, trading_day) -> None:
    spot_price = broker.get_nifty_spot_ltp(kite)

    if strategy.is_basis_anomalous(futures_price_at_trigger, spot_price):
        basis = strategy.compute_basis(futures_price_at_trigger, spot_price)
        logger.warning(
            "Futures/spot basis %.2f exceeds warning threshold (%.0f). Trading as specified "
            "unless BASIS_HARD_ABORT is set.", basis, config.BASIS_WARNING_THRESHOLD_POINTS,
        )
        if config.BASIS_HARD_ABORT:
            logger.critical("BASIS_HARD_ABORT enabled — aborting entry due to anomalous basis")
            execution.set_week_status(execution.week_key_for(trading_day), "NO_TRADE",
                                       reason="basis_hard_abort", basis=basis)
            return

    atm_strike = strategy.compute_atm_strike(spot_price)
    long_strike = strategy.compute_long_leg_strike(atm_strike, direction)
    option_type = strategy.option_type_for_direction(direction)

    sell_instrument = broker.resolve_option(kite, expiry, atm_strike, option_type)
    buy_instrument = broker.resolve_option(kite, expiry, long_strike, option_type)

    quantity = sell_instrument.lot_size * config.POSITION_LOTS

    logger.info(
        "ENTRY TRIGGER: reason=%s direction=%s futures_price=%.2f spot_price=%.2f "
        "atm_strike=%d long_strike=%d option_type=%s expiry=%s",
        entry_reason, direction, futures_price_at_trigger, spot_price,
        atm_strike, long_strike, option_type, expiry,
    )

    if config.BROKER == "jainam":
        # jainam.execution.enter_spread needs the resolved instrument
        # objects (for .instrument_token) -- Kite can quote/order directly
        # by tradingsymbol so zerodha.execution.enter_spread just takes
        # the strings. See jainam/execution.py's module docstring.
        fill_result = execution.enter_spread(kite, sell_instrument, buy_instrument, quantity, config.DRY_RUN)
    else:
        fill_result = execution.enter_spread(
            kite, sell_instrument.tradingsymbol, buy_instrument.tradingsymbol, quantity, config.DRY_RUN
        )

    week_key = execution.week_key_for(trading_day)

    if fill_result["status"] not in ("FILLED", "PARTIAL"):
        logger.error("Entry not completed (%s) — not marking week as traded", fill_result["status"])
        execution.set_week_status(week_key, "NO_TRADE", reason=fill_result["status"])
        return

    position = {
        "status": "OPEN" if fill_result["status"] == "FILLED" else "PARTIAL",
        "week_key": week_key,
        "direction": direction,
        "entry_reason": entry_reason,
        "entry_date": trading_day.isoformat(),
        "expiry_date": expiry.isoformat(),
        "futures_price_at_entry": futures_price_at_trigger,
        "spot_price_at_entry": spot_price,
        "quantity": quantity,
        "lot_size": sell_instrument.lot_size,
        "lots": config.POSITION_LOTS,
        "sell_leg": {"tradingsymbol": sell_instrument.tradingsymbol, "strike": atm_strike,
                     "fill_price": fill_result["sell_fill"]},
        "buy_leg": {"tradingsymbol": buy_instrument.tradingsymbol, "strike": long_strike,
                    "fill_price": fill_result["buy_fill"]},
    }

    if fill_result["status"] == "FILLED":
        entry_credit_per_share = fill_result["sell_fill"] - fill_result["buy_fill"]
        max_profit = strategy.compute_max_profit(
            fill_result["sell_fill"], fill_result["buy_fill"], sell_instrument.lot_size, config.POSITION_LOTS
        )
        position["entry_credit_per_share"] = entry_credit_per_share
        position["max_profit"] = max_profit
        logger.info("Entry filled. Credit/share=%.2f Max profit=%.2f", entry_credit_per_share, max_profit)
        execution.set_week_status(week_key, "TRADED", entry_reason=entry_reason, direction=direction)
    else:
        logger.critical("Entry PARTIAL (naked long) — see log above for details")
        execution.set_week_status(week_key, "TRADED", entry_reason=entry_reason,
                                   direction=direction, note="PARTIAL_FILL")

    execution.save_position(position)


def run_entry_monitor() -> int:
    now_ist = datetime.now(config.IST)
    today = now_ist.date()
    iso_weekday = now_ist.isoweekday()
    is_wednesday = iso_weekday == 3
    is_thursday = iso_weekday == 4

    if not (is_wednesday or is_thursday):
        logger.info("Today is not Wednesday or Thursday — nothing to do")
        return 0

    week_key = execution.week_key_for(today)
    week_already_resolved = execution.is_week_resolved(week_key)

    wednesday_date = today if is_wednesday else today - timedelta(days=1)
    thursday_date = wednesday_date + timedelta(days=1)

    try:
        wednesday_is_holiday = broker.is_trading_holiday(wednesday_date)
        thursday_is_holiday = broker.is_trading_holiday(thursday_date)
    except broker.HolidayLookupError:
        logger.critical(
            "Cannot determine NSE holiday status (no live source, cache, or override) — "
            "skipping today rather than guessing"
        )
        return 1

    action = strategy.resolve_trading_day_action(
        today, is_wednesday, is_thursday, wednesday_is_holiday, thursday_is_holiday, week_already_resolved
    )

    if action == strategy.WeekAction.ALREADY_RESOLVED:
        logger.info("Week %s already resolved (%s) — nothing to do", week_key, execution.get_week_status(week_key))
        return 0

    if action == strategy.WeekAction.WAIT_NOT_YET:
        execution.set_week_status(week_key, "SHIFTED_TO_THURSDAY", reason="Wednesday is an NSE holiday")
        logger.info("Wednesday %s is an NSE holiday — shifting this week's trade to Thursday %s",
                    wednesday_date, thursday_date)
        return 0

    if action == strategy.WeekAction.SKIP_TWO_DAY_HOLIDAY:
        execution.set_week_status(week_key, "SKIPPED_HOLIDAY_STRETCH",
                                   wednesday=wednesday_date.isoformat(), thursday=thursday_date.isoformat())
        logger.critical(
            "Both Wednesday %s AND Thursday %s are NSE holidays — this is not auto-resolved "
            "(see report). Skipping this week entirely; no trade will be attempted.",
            wednesday_date, thursday_date,
        )
        return 0

    trading_day = today  # action == TRADE_TODAY

    kite = broker.get_client()
    if kite is None:
        logger.error(
            "No valid %s session — cannot trade today. Run "
            "`python -m wednesday_nifty.zerodha.broker` (Kite) if config.BROKER is \"kite\".",
            config.BROKER,
        )
        return 1

    try:
        future = broker.resolve_current_month_future(kite, trading_day)
    except Exception:
        logger.exception("Failed to resolve current-month NIFTY future")
        return 1

    if strategy.is_rollover_proximity(future.expiry, trading_day):
        execution.set_week_status(week_key, "SKIPPED_ROLLOVER", future_expiry=future.expiry.isoformat())
        logger.critical(
            "Futures contract %s expires %s, within %d day(s) of trade date %s — rollover "
            "proximity edge case. Not guessing which contract to use; skipping this week. "
            "Please confirm manually.", future.tradingsymbol, future.expiry,
            config.ROLLOVER_PROXIMITY_DAYS, trading_day,
        )
        return 0

    try:
        candles = broker.fetch_futures_daily_candles(kite, future.instrument_token, trading_day)
        levels_data = strategy.compute_3day_levels(candles, trading_day)
        logger.info("3-Day levels for %s: High=%.2f Low=%.2f (from sessions %s)",
                    trading_day, levels_data.three_day_high, levels_data.three_day_low,
                    levels_data.trading_days_used)
    except Exception:
        logger.exception("Failed to compute 3-day levels")
        return 1

    try:
        expiry = broker.resolve_weekly_expiry(kite, trading_day)
    except Exception:
        logger.exception("Failed to resolve weekly option expiry")
        return 1

    _wait_until_market_open(datetime.now(config.IST))

    opening_price = broker.get_futures_opening_price(kite, future, trading_day)
    logger.info("Trading day %s opening futures price: %.2f", trading_day, opening_price)

    if config.BROKER == "jainam":
        ticker_result = _run_poll_loop(kite, future, levels_data)
    else:
        ticker_result = _run_ticker_loop(kite, future, levels_data)

    if ticker_result["triggered"]:
        _execute_entry(
            kite, ticker_result["direction"], strategy.EntryReason.BREAKOUT,
            ticker_result["futures_price"], expiry, trading_day,
        )
        return 0

    # No breakout by 2:35pm -- Entry Logic 3 fallback.
    current_price = broker.get_latest_futures_price(kite, future)
    direction = strategy.detect_fallback_direction(current_price, opening_price)
    logger.info("No breakout by %s — fallback check: open=%.2f current=%.2f -> %s",
                config.FALLBACK_CHECK_TIME, opening_price, current_price, direction)
    _execute_entry(kite, direction, strategy.EntryReason.FALLBACK_2_35, current_price, expiry, trading_day)
    return 0


# =============================================================================
# Exit monitor
# =============================================================================

def _within_market_hours(now_ist: datetime) -> bool:
    if now_ist.isoweekday() > 5:
        return False
    return config.MARKET_OPEN <= now_ist.time() <= config.MARKET_CLOSE


def run_exit_monitor() -> int:
    now_ist = datetime.now(config.IST)
    if not _within_market_hours(now_ist):
        return 0

    position = execution.get_open_position()
    if position is None:
        return 0

    kite = broker.get_client()
    if kite is None:
        logger.warning(
            "Open position exists but no valid %s session — skipping this check cycle, "
            "will retry next interval.", config.BROKER,
        )
        return 1

    expiry_date = date.fromisoformat(position["expiry_date"])
    has_credit_basis = position.get("entry_credit_per_share") is not None

    exit_reason = None

    if has_credit_basis:
        sell_symbol = position["sell_leg"]["tradingsymbol"]
        buy_symbol = position["buy_leg"]["tradingsymbol"]
        quote = kite.quote([f"NFO:{sell_symbol}", f"NFO:{buy_symbol}"])
        sell_ltp = quote[f"NFO:{sell_symbol}"]["last_price"]
        buy_ltp = quote[f"NFO:{buy_symbol}"]["last_price"]

        current_credit_per_share = strategy.compute_current_credit_value(sell_ltp, buy_ltp)
        entry_credit_per_share = position["entry_credit_per_share"]
        captured = strategy.captured_profit_fraction(entry_credit_per_share, current_credit_per_share)

        logger.info(
            "Mark-to-market: entry_credit=%.2f current_credit=%.2f captured=%.1f%%",
            entry_credit_per_share, current_credit_per_share, captured * 100,
        )

        if strategy.profit_target_hit(entry_credit_per_share, current_credit_per_share):
            exit_reason = "PROFIT_TARGET"

    if exit_reason is None and now_ist.date() == expiry_date and now_ist.time() >= config.EXPIRY_EXIT_TIME:
        exit_reason = "TIME_EXIT"

    if exit_reason is None:
        return 0

    logger.info("Exiting position: reason=%s", exit_reason)
    close_result = execution.exit_spread(kite, position, config.DRY_RUN)

    if close_result["status"] == "CLOSED":
        execution.update_position(
            status="CLOSED",
            close_reason=exit_reason,
            closed_date=now_ist.date().isoformat(),
            close_short_fill=close_result.get("close_short_fill"),
            close_long_fill=close_result.get("close_long_fill"),
        )
        logger.info("Position closed cleanly (%s)", exit_reason)
    else:
        execution.update_position(status="PARTIAL_EXIT", close_reason=exit_reason, **close_result)
        logger.critical("Position exit was PARTIAL — manual intervention required (see log above)")

    return 0


# =============================================================================
# CLI dispatch
# =============================================================================

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ("entry", "exit"):
        print("Usage: python -m wednesday_nifty.monitor {entry|exit}")
        return 2
    if argv[0] == "entry":
        return run_entry_monitor()
    return run_exit_monitor()


if __name__ == "__main__":
    sys.exit(main())
