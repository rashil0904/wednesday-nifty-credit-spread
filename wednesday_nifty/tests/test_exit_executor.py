from datetime import date, datetime, timedelta

import pytest

import config
import exit_executor
import position_store as ps
from strategy.breakout import EntryTrigger
from strategy.levels import ThreeDayLevels

LEVELS = ThreeDayLevels(high=25400.0, low=24950.0, sessions_used=(date(2026, 9, 11),))
EXPIRY = date(2026, 9, 22)
FUTURES_TOKEN = 9001
SHORT_TOKEN = 1
HEDGE_TOKEN = 2
FAR_FUTURE_EXIT_DATE = date(2026, 12, 29)  # never due in these tests unless explicitly testing time-exit


class FakeClient:
    def __init__(self):
        self.ltps: dict[int, float] = {}
        self.net_quantities: dict[int, int] = {}
        self.orders: list[dict] = []
        self.future = {"tradingsymbol": "NIFTY26SEPFUT", "instrument_token": FUTURES_TOKEN, "expiry": date(2026, 9, 29)}

    def get_ltp(self, segment, instrument_id):
        return self.ltps[instrument_id]

    def get_nifty_fut_instrument(self, as_of):
        return self.future

    def get_net_quantity(self, instrument_id):
        return self.net_quantities.get(instrument_id, 0)

    def place_order(self, instrument_id, side, qty, price, tag):
        order_id = 1000 + len(self.orders)
        self.orders.append({"id": order_id, "instrument_id": instrument_id, "side": side, "qty": qty})
        return order_id

    def cancel_order(self, order_id):
        pass

    def get_order(self, order_id):
        for o in self.orders:
            if o["id"] == order_id:
                return {"OrderStatus": "Filled", "CumulativeQuantity": o["qty"],
                        "OrderAverageTradedPrice": "100.0", "CancelRejectReason": ""}
        return None


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(ps, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    (tmp_path / "locks").mkdir()
    monkeypatch.setattr(config, "JAINAM_LIVE", False)  # dry run by default, matches .env


def legs():
    return [
        ps.StoredLeg(role="SHORT_ATM", option_type="PE", strike=25100, instrument_token=SHORT_TOKEN,
                     tradingsymbol="NIFTY...SHORT", quantity=75, avg_price=150.0),
        ps.StoredLeg(role="LONG_HEDGE", option_type="PE", strike=24900, instrument_token=HEDGE_TOKEN,
                     tradingsymbol="NIFTY...HEDGE", quantity=75, avg_price=80.0),
    ]


def open_high_breakout_position(*, legs_filled=True, exit_date=FAR_FUTURE_EXIT_DATE):
    ps.write_position(date(2026, 9, 16), EntryTrigger.HIGH_BREAKOUT, LEVELS, EXPIRY, exit_date,
                       FUTURES_TOKEN, legs(), entry_credit=70.0)
    if legs_filled:
        ps.update_position(legs_filled=True)


# =============================================================================
# No exit when nothing triggered
# =============================================================================

def test_no_exit_when_nothing_triggered():
    open_high_breakout_position()
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 25450.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}  # spread still worth full credit
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason is None
    assert ps.read_position()["status"] == "OPEN"


# =============================================================================
# Stop loss (HIGH_BREAKOUT)
# =============================================================================

def test_stop_loss_triggers_exit_in_dry_run():
    open_high_breakout_position()
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 25399.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}  # below sl_level=25400
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason == "STOP_LOSS"
    data = ps.read_position()
    assert data["status"] == "CLOSED"
    assert data["exit_reason"] == "STOP_LOSS"


def test_no_sl_check_before_both_legs_filled():
    open_high_breakout_position(legs_filled=False)
    client = FakeClient()
    # Futures deep below sl_level -- would fire if legs_filled were True.
    client.ltps = {FUTURES_TOKEN: 20000.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason is None
    assert ps.read_position()["status"] == "OPEN"


def test_low_breakout_position_never_stops_out_even_at_extreme_price():
    ps.write_position(date(2026, 9, 16), EntryTrigger.LOW_BREAKOUT, LEVELS, EXPIRY, FAR_FUTURE_EXIT_DATE,
                       FUTURES_TOKEN, legs(), entry_credit=70.0)
    ps.update_position(legs_filled=True)
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 1.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}  # sl_level is None for LOW_BREAKOUT
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason is None


def test_gap_down_open_below_sl_on_a_later_day_fires_on_first_tick():
    open_high_breakout_position()
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 25000.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}  # gap-down open
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason == "STOP_LOSS"


# =============================================================================
# Profit target
# =============================================================================

def test_profit_target_triggers_exit_when_sl_not_breached():
    open_high_breakout_position()
    client = FakeClient()
    # Futures still above sl_level (no SL), spread down to 10% of entry credit (90% captured).
    client.ltps = {FUTURES_TOKEN: 25450.0, SHORT_TOKEN: 10.0, HEDGE_TOKEN: 3.0}  # value=7.0, 90% of 70
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason == "PROFIT_TARGET"
    assert ps.read_position()["exit_reason"] == "PROFIT_TARGET"


# =============================================================================
# SL and profit target both true on the same check -> exactly one exit
# =============================================================================

def test_sl_and_profit_target_both_true_on_same_check_exactly_one_exit():
    open_high_breakout_position()
    client = FakeClient()
    # Futures below sl_level AND spread value already at the profit target.
    client.ltps = {FUTURES_TOKEN: 25000.0, SHORT_TOKEN: 10.0, HEDGE_TOKEN: 3.0}
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason == "STOP_LOSS"  # checked first
    data = ps.read_position()
    assert data["status"] == "CLOSED"
    assert data["exit_reason"] == "STOP_LOSS"  # not overwritten by a second exit attempt


# =============================================================================
# Time exit
# =============================================================================

def test_time_exit_fires_when_due(monkeypatch):
    past_exit_date = date(2020, 1, 1)
    open_high_breakout_position(exit_date=past_exit_date)
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 25450.0, SHORT_TOKEN: 150.0, HEDGE_TOKEN: 80.0}  # neither SL nor PT
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason == "TIME_EXIT"


# =============================================================================
# Rollover flag blocks the SL check without silently switching contracts
# =============================================================================

def test_rollover_flag_blocks_sl_check_but_allows_profit_target():
    open_high_breakout_position()
    client = FakeClient()
    client.future = {"tradingsymbol": "NIFTY26OCTFUT", "instrument_token": 9999, "expiry": date(2026, 10, 27)}
    # Futures would breach SL if checked, but the contract has rolled -- must NOT fire STOP_LOSS.
    client.ltps = {FUTURES_TOKEN: 20000.0, SHORT_TOKEN: 10.0, HEDGE_TOKEN: 3.0}
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    # Profit target condition is also met here and isn't blocked by the roll flag.
    assert reason == "PROFIT_TARGET"


def test_already_closed_position_short_circuits():
    open_high_breakout_position()
    ps.update_position(status="CLOSED")
    client = FakeClient()
    client.ltps = {FUTURES_TOKEN: 0.0, SHORT_TOKEN: 0.0, HEDGE_TOKEN: 0.0}
    reason = exit_executor.check_and_exit_if_triggered(client, None, ps.read_position())
    assert reason is None
