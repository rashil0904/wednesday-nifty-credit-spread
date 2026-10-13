"""
Entry-day script: Wednesday (or Thursday if Wednesday is an NSE
holiday). Computes the 3-day levels, streams NIFTY futures from 09:15,
enters on the first breakout or the 14:35 red/green fallback, then
keeps monitoring for the rest of the trading day (profit target always;
stop loss only for a HIGH_BREAKOUT entry -- see strategy/stop_loss.py).

Guarded against a double entry three ways: a per-day file lock
(position_store.acquire_entry_lock), a position-file check (today
already resolved), and -- once live -- a broker net-position check
before placing any order.

DRY RUN by default (JAINAM_LIVE=false in .env): streams real market
data and logs exactly what it would do; places no orders.

    python scripts/run_entry.py
"""

import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import position_store as ps
from broker.market_stream import MarketStream, get_ltp_with_fallback
from broker.order_exec import execute_order
from broker.xts_client import NSE_FO_SEGMENT, XTSDataClient
from exit_executor import check_and_exit_if_triggered
from strategy.breakout import check_breakout, fallback_trigger
from strategy.calendar import WeekAction, following_tuesday, resolve_entry_day_action, resolve_exit_date
from strategy.levels import compute_3day_levels
from strategy.plan import hedge_direction_sign, option_type_for_trigger
from strategy.strikes import calculate_atm_strike, calculate_hedge_strike


def log(msg: str) -> None:
    print(f"[{datetime.now(config.IST).isoformat()}] {msg}")


def main() -> int:
    now = datetime.now(config.IST)
    today = now.date()
    is_wed = today.weekday() == 2
    is_thu = today.weekday() == 3

    if not (is_wed or is_thu):
        log("Today is not Wednesday or Thursday -- nothing to do.")
        return 0

    wednesday_date = today if is_wed else today - timedelta(days=1)
    thursday_date = wednesday_date + timedelta(days=1)
    wed_holiday = wednesday_date in config.NSE_HOLIDAYS
    thu_holiday = thursday_date in config.NSE_HOLIDAYS

    existing = ps.read_position()
    week_already_resolved = bool(existing and existing.get("entry_date") == today.isoformat())

    action = resolve_entry_day_action(today, is_wed, is_thu, wed_holiday, thu_holiday, week_already_resolved)

    if action is WeekAction.ALREADY_RESOLVED:
        log("Today is already resolved -- nothing to do.")
        return 0
    if action is WeekAction.WAIT_NOT_YET:
        log("Wednesday is an NSE holiday -- waiting for Thursday.")
        return 0
    if action is WeekAction.SKIP_HOLIDAY_STRETCH:
        ps.write_no_trade(today, "Both Wednesday and Thursday are NSE holidays")
        log("Both Wednesday and Thursday are holidays -- skipping this week entirely.")
        return 0

    # action is TRADE_TODAY
    try:
        with ps.acquire_entry_lock(today):
            return _run_entry_day(now, today)
    except ps.LockHeldError:
        log("Entry lock already held (another run in progress or a stuck prior run) -- exiting.")
        return 1


def _run_entry_day(now: datetime, today: date) -> int:
    client = XTSDataClient.from_env()
    log(f"Logged in. client_id={client.client_id or '(not set -- dry run only)'}")

    future = client.get_nifty_fut_instrument(today)
    log(f"Near-month future: {future['tradingsymbol']} (expiry {future['expiry']})")
    if (future["expiry"] - today).days <= config.ROLLOVER_PROXIMITY_DAYS:
        ps.write_no_trade(today, f"Rollover proximity: future expires {future['expiry']}")
        log(f"Future expires {future['expiry']}, within {config.ROLLOVER_PROXIMITY_DAYS} day(s) -- "
            "skipping this week rather than guessing which contract to use.")
        return 0

    to_date = today - timedelta(days=1)
    from_date = to_date - timedelta(days=14)
    candles = client.get_daily_candles(NSE_FO_SEGMENT, future["instrument_token"], from_date, to_date)
    daily = [{"date": c.timestamp.date(), "high": c.high, "low": c.low} for c in candles]
    try:
        levels = compute_3day_levels(daily, today)
    except ValueError as exc:
        ps.write_no_trade(today, str(exc))
        log(f"Could not compute 3-day levels: {exc}")
        return 1
    log(f"3D_HIGH={levels.high} 3D_LOW={levels.low} sessions={levels.sessions_used}")

    _wait_until(config.ENTRY_DECISION_START)
    opening_price = _get_opening_price_with_retry(client, future["instrument_token"], today)
    log(f"09:15 futures open: {opening_price}")

    stream = None
    if config.USE_WEBSOCKET:
        stream = MarketStream(client)
        try:
            stream.connect()
            stream.subscribe([(NSE_FO_SEGMENT, future["instrument_token"])])
            log("Market-data socket connected and subscribed.")
        except Exception as exc:
            log(f"Socket connect/subscribe failed ({exc!r}) -- using REST polling for today.")
            stream = None

    trigger = None
    while True:
        now_ist = datetime.now(config.IST)
        if now_ist.time() >= config.FALLBACK_TRIGGER_TIME:
            break
        ltp = get_ltp_with_fallback(stream, client, NSE_FO_SEGMENT, future["instrument_token"], log=log)
        trigger = check_breakout(ltp, levels)
        if trigger:
            log(f"BREAKOUT: {trigger.value} at futures_ltp={ltp}")
            break
        time_module.sleep(config.POLL_INTERVAL_SECONDS)

    if trigger is None:
        price_1435 = get_ltp_with_fallback(stream, client, NSE_FO_SEGMENT, future["instrument_token"], log=log)
        trigger = fallback_trigger(price_1435, opening_price)
        if trigger is None:
            ps.write_no_trade(today, f"14:35 fallback: price {price_1435} exactly equal to 09:15 open {opening_price}")
            log("14:35 fallback: price exactly equal to the open -- no trade.")
            if stream:
                stream.disconnect()
            return 0
        log(f"14:35 fallback: {trigger.value} (price={price_1435}, open={opening_price})")

    exit_code = _enter_and_monitor(client, stream, today, future, levels, trigger)
    if stream:
        stream.disconnect()
    return exit_code


def _enter_and_monitor(client, stream, today, future, levels, trigger) -> int:
    following_tue = following_tuesday(today, config.EXPIRY_WEEKDAY)
    expiry = min(client.list_nifty_option_expiries(following_tue))
    exit_date = resolve_exit_date(expiry, config.NSE_HOLIDAYS)
    log(f"Expiry: {expiry}  Exit date: {exit_date}")

    strike_interval, lot_size = client.resolve_nifty_option_grid(expiry)
    spot = client.get_nifty_spot_ltp()
    atm_strike = calculate_atm_strike(spot, strike_interval)
    hedge_strike = calculate_hedge_strike(
        atm_strike, config.HEDGE_OFFSET_POINTS, strike_interval,
        direction_sign=hedge_direction_sign(trigger),
    )
    option_type = option_type_for_trigger(trigger).value
    log(f"Spot={spot} ATM={atm_strike} Hedge={hedge_strike} ({option_type})")

    short_instr = client.resolve_option_instrument(atm_strike, option_type, expiry)
    hedge_instr = client.resolve_option_instrument(hedge_strike, option_type, expiry)
    quantity = config.LOTS * lot_size

    # Entry sequencing: BUY the hedge leg first (for margin), then SELL the short leg.
    if not config.JAINAM_LIVE:
        hedge_fill_price = client.get_ltp(NSE_FO_SEGMENT, hedge_instr["instrument_token"])
        short_fill_price = client.get_ltp(NSE_FO_SEGMENT, short_instr["instrument_token"])
        log(f"[DRY_RUN] WOULD BUY {quantity} {hedge_instr['tradingsymbol']} near {hedge_fill_price}")
        log(f"[DRY_RUN] WOULD SELL {quantity} {short_instr['tradingsymbol']} near {short_fill_price}")
        legs_filled_ok = True
    else:
        net_hedge = client.get_net_quantity(hedge_instr["instrument_token"])
        net_short = client.get_net_quantity(short_instr["instrument_token"])
        if net_hedge != 0 or net_short != 0:
            log(f"!! Pre-existing position on these instruments (hedge net={net_hedge}, short net={net_short}) "
                "-- refusing to enter, review manually.")
            ps.write_no_trade(today, "Pre-existing non-zero net position on resolved instruments", mode="live")
            return 1

        tag = f"E{today.strftime('%Y%m%d')}"
        buy = execute_order(client, hedge_instr["instrument_token"], "BUY", quantity, tag + "H", allow_market_fallback=False)
        if buy.filled == 0:
            log(f"!! Hedge BUY did not fill ({buy.rejected_reason or 'no fill'}) -- nothing traded.")
            ps.write_no_trade(today, f"Hedge BUY did not fill: {buy.rejected_reason or 'no fill'}", mode="live")
            return 1
        hedge_fill_price = buy.avg_price
        log(f"Hedge filled: {buy.filled} @ {buy.avg_price}")

        sell = execute_order(client, short_instr["instrument_token"], "SELL", quantity, tag + "S", allow_market_fallback=False)
        short_fill_price = sell.avg_price if sell.filled else None
        legs_filled_ok = sell.filled == quantity
        if not legs_filled_ok:
            log(f"!! SHORT leg did not fully fill ({sell.filled}/{quantity}) -- "
                "naked/partial long position. MANUAL REVIEW REQUIRED.")

    entry_credit = (short_fill_price - hedge_fill_price) if short_fill_price is not None else 0.0

    legs = [
        ps.StoredLeg(role="SHORT_ATM", option_type=option_type, strike=atm_strike,
                     instrument_token=short_instr["instrument_token"], tradingsymbol=short_instr["tradingsymbol"],
                     quantity=quantity, avg_price=short_fill_price or 0.0),
        ps.StoredLeg(role="LONG_HEDGE", option_type=option_type, strike=hedge_strike,
                     instrument_token=hedge_instr["instrument_token"], tradingsymbol=hedge_instr["tradingsymbol"],
                     quantity=quantity, avg_price=hedge_fill_price or 0.0),
    ]
    ps.write_position(
        today, trigger, levels, expiry, exit_date, future["instrument_token"], legs, entry_credit,
        mode="live" if config.JAINAM_LIVE else "dry",
    )
    if legs_filled_ok:
        ps.update_position(legs_filled=True)
    log(f"ENTRY {'' if config.JAINAM_LIVE else '(DRY RUN) '}complete: trigger={trigger.value} "
        f"entry_credit={entry_credit:.2f}")

    if not legs_filled_ok:
        return 1

    if stream:
        try:
            stream.subscribe([
                (NSE_FO_SEGMENT, short_instr["instrument_token"]),
                (NSE_FO_SEGMENT, hedge_instr["instrument_token"]),
            ])
        except Exception as exc:
            log(f"Could not subscribe option legs ({exc!r}) -- profit-target/SL checks will use REST.")

    log("Monitoring for the rest of the trading day (profit target"
        + (" + stop loss" if trigger.value == "HIGH_BREAKOUT" else "") + ")...")
    while True:
        now_ist = datetime.now(config.IST)
        if now_ist.time() >= config.MARKET_CLOSE:
            log("Market closed -- ending today's monitoring. run_monitor_exit.py takes over on later days.")
            return 0
        position = ps.read_position()
        if position is None or position.get("status") != "OPEN":
            log("Position no longer OPEN -- stopping monitoring.")
            return 0
        exit_reason = check_and_exit_if_triggered(client, stream, position, log=log)
        if exit_reason:
            log(f"Exited same-day: {exit_reason}")
            return 0
        time_module.sleep(config.POLL_INTERVAL_SECONDS)


def _wait_until(target_time) -> None:
    now = datetime.now(config.IST)
    if now.time() >= target_time:
        return
    target_dt = now.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)
    wait_seconds = (target_dt - now).total_seconds()
    log(f"Waiting {wait_seconds:.0f}s until {target_time}")
    time_module.sleep(max(0, wait_seconds))


def _get_opening_price_with_retry(client, instrument_token: int, today: date, retries: int = 5, delay: float = 5.0) -> float:
    """The 09:15 candle isn't available from the broker until that
    minute has actually completed -- retry briefly rather than fail
    immediately if we're called right at 09:15:00."""
    last_exc = None
    for attempt in range(retries):
        try:
            return client.get_candle_at(NSE_FO_SEGMENT, instrument_token, today, config.ENTRY_DECISION_START).open
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time_module.sleep(delay)
    raise RuntimeError(f"Could not fetch 09:15 opening candle after {retries} retries") from last_exc


if __name__ == "__main__":
    sys.exit(main())
