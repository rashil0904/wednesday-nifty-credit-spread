from datetime import date

import pytest

from wednesday_nifty import config, execution
from wednesday_nifty.tests.fake_kite import FakeKite

SELL_SYM = "NIFTY25091825000CE"
BUY_SYM = "NIFTY25091825200CE"


@pytest.fixture(autouse=True)
def isolated_state_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_STATE_FILE", tmp_path / "weekly_state.json")
    monkeypatch.setattr(config, "POSITIONS_FILE", tmp_path / "positions.json")


@pytest.fixture
def kite():
    k = FakeKite()
    k.quotes[f"NFO:{SELL_SYM}"] = {"last_price": 82.35, "depth": {"buy": [], "sell": []}}
    k.quotes[f"NFO:{BUY_SYM}"] = {"last_price": 24.10, "depth": {"buy": [], "sell": []}}
    k.ltps[f"NFO:{SELL_SYM}"] = 82.35
    k.ltps[f"NFO:{BUY_SYM}"] = 24.10
    return k


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(config, "LEG_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "LEG_RETRY_COUNT", 2)


# =============================================================================
# State: week key / one-trade-per-Wednesday guard
# =============================================================================

def test_week_key_maps_wednesday_and_thursday_to_same_key():
    wed = date(2026, 9, 16)
    thu = date(2026, 9, 17)
    assert execution.week_key_for(wed) == execution.week_key_for(thu) == wed.isoformat()


def test_week_key_differs_across_weeks():
    assert execution.week_key_for(date(2026, 9, 16)) != execution.week_key_for(date(2026, 9, 23))


def test_one_trade_per_wednesday_guard_persists_across_reload():
    week_key = execution.week_key_for(date(2026, 9, 16))
    assert not execution.is_week_resolved(week_key)

    execution.set_week_status(week_key, "TRADED", direction="BULLISH")

    # Simulate a process restart: nothing is held in memory, everything is
    # re-read from disk on the next call.
    assert execution.is_week_resolved(week_key)
    assert execution.get_week_status(week_key) == "TRADED"


def test_shifted_to_thursday_is_not_treated_as_resolved():
    week_key = execution.week_key_for(date(2026, 9, 16))
    execution.set_week_status(week_key, "SHIFTED_TO_THURSDAY", reason="Wednesday is an NSE holiday")
    assert not execution.is_week_resolved(week_key)


# =============================================================================
# State: position round trip
# =============================================================================

def test_position_round_trip():
    assert execution.get_open_position() is None
    execution.save_position({"status": "OPEN", "direction": "BEARISH"})
    pos = execution.get_open_position()
    assert pos["status"] == "OPEN"

    execution.update_position(status="CLOSED", close_reason="PROFIT_TARGET")
    assert execution.get_open_position() is None
    assert execution.load_position()["close_reason"] == "PROFIT_TARGET"


# =============================================================================
# Order execution: dry-run, leg sequencing, failure handling, margin
# =============================================================================

def test_dry_run_never_places_real_orders(kite):
    result = execution.enter_spread(kite, SELL_SYM, BUY_SYM, quantity=75, dry_run=True)
    assert result["status"] == "FILLED"
    assert kite.order_log == []


def test_entry_buy_leg_fails_aborts_with_no_position(kite):
    kite.fill_map[BUY_SYM] = "REJECTED"
    result = execution.enter_spread(kite, SELL_SYM, BUY_SYM, quantity=75, dry_run=False)
    assert result["status"] == "ABORTED_NO_FILL"
    assert result["leg"] == "BUY"
    assert all(o["tradingsymbol"] != SELL_SYM for o in kite.order_log)


def test_entry_sell_leg_fails_after_buy_fills_is_partial_naked_long(kite):
    kite.fill_map[SELL_SYM] = "REJECTED"
    result = execution.enter_spread(kite, SELL_SYM, BUY_SYM, quantity=75, dry_run=False)
    assert result["status"] == "PARTIAL"
    assert result["buy_fill"] is not None
    assert result["sell_fill"] is None
    # Buy leg only ever attempted once (it succeeded); sell leg retried LEG_RETRY_COUNT times,
    # each attempt placing LIMIT then MARKET since both are rejected under the fake.
    buy_orders = [o for o in kite.order_log if o["tradingsymbol"] == BUY_SYM]
    sell_orders = [o for o in kite.order_log if o["tradingsymbol"] == SELL_SYM]
    assert len(buy_orders) == 1
    assert len(sell_orders) == config.LEG_RETRY_COUNT * 2


def test_margin_check_blocks_entry_when_insufficient(kite):
    kite.margins_available = 100
    kite.margin_required = 50_000
    result = execution.enter_spread(kite, SELL_SYM, BUY_SYM, quantity=75, dry_run=False)
    assert result["status"] == "ABORTED_MARGIN"
    assert kite.order_log == []


def test_exit_closes_both_legs_short_first(kite):
    position = {
        "quantity": 75,
        "sell_leg": {"tradingsymbol": SELL_SYM, "fill_price": 82.35},
        "buy_leg": {"tradingsymbol": BUY_SYM, "fill_price": 24.10},
    }
    result = execution.exit_spread(kite, position, dry_run=False)
    assert result["status"] == "CLOSED"
    # First order placed overall must be the BUY-to-close on the short leg.
    assert kite.order_log[0]["tradingsymbol"] == SELL_SYM
    assert kite.order_log[0]["transaction_type"] == "BUY"


def test_exit_of_partial_position_skips_short_leg_close(kite):
    position = {
        "quantity": 75,
        "sell_leg": {"tradingsymbol": SELL_SYM, "fill_price": None},  # never filled at entry
        "buy_leg": {"tradingsymbol": BUY_SYM, "fill_price": 24.10},
    }
    result = execution.exit_spread(kite, position, dry_run=False)
    assert result["status"] == "CLOSED"
    assert result["close_short_fill"] is None
    assert all(o["tradingsymbol"] != SELL_SYM for o in kite.order_log)
    assert any(o["tradingsymbol"] == BUY_SYM and o["transaction_type"] == "SELL" for o in kite.order_log)


def test_exit_short_leg_close_failure_reports_partial_exit(kite):
    kite.fill_map[SELL_SYM] = "REJECTED"
    position = {
        "quantity": 75,
        "sell_leg": {"tradingsymbol": SELL_SYM, "fill_price": 82.35},
        "buy_leg": {"tradingsymbol": BUY_SYM, "fill_price": 24.10},
    }
    result = execution.exit_spread(kite, position, dry_run=False)
    assert result["status"] == "PARTIAL_EXIT"
    assert result["short_closed"] is False
    # Long leg close must never be attempted while the short is still open.
    assert all(o["tradingsymbol"] != BUY_SYM for o in kite.order_log)
