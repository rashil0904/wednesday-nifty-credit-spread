from datetime import date

import pytest

from wednesday_nifty.jainam import broker, execution
from wednesday_nifty.jainam.client import NSE_FO_SEGMENT
from wednesday_nifty.tests.fake_xts import FakeXTSClient

SELL_TOKEN = 7001
BUY_TOKEN = 7002


@pytest.fixture
def client():
    c = FakeXTSClient()
    c.ltps[(NSE_FO_SEGMENT, SELL_TOKEN)] = 150.0
    c.ltps[(NSE_FO_SEGMENT, BUY_TOKEN)] = 80.0
    return c


def sell_instrument():
    return broker.OptionInstrument(
        tradingsymbol="NIFTY2591625000CE", instrument_token=SELL_TOKEN,
        expiry=date(2026, 9, 16), strike=25000, lot_size=75,
    )


def buy_instrument():
    return broker.OptionInstrument(
        tradingsymbol="NIFTY2591625200CE", instrument_token=BUY_TOKEN,
        expiry=date(2026, 9, 16), strike=25200, lot_size=75,
    )


# =============================================================================
# place_leg -- dry-run only
# =============================================================================

def test_place_leg_dry_run_returns_complete_at_ltp(client):
    result = execution.place_leg(client, SELL_TOKEN, "SELL", 75, dry_run=True)
    assert result == {"status": "COMPLETE", "average_price": 150.0, "order_id": "DRY_RUN"}


def test_place_leg_live_raises_not_implemented(client):
    with pytest.raises(NotImplementedError):
        execution.place_leg(client, SELL_TOKEN, "SELL", 75, dry_run=False)


def test_check_margin_always_passes(client):
    assert execution.check_margin(client, SELL_TOKEN, BUY_TOKEN, 75, dry_run=True) is True


# =============================================================================
# enter_spread
# =============================================================================

def test_enter_spread_buy_then_sell_fills_dry_run(client):
    result = execution.enter_spread(client, sell_instrument(), buy_instrument(), 75, dry_run=True)
    assert result == {"status": "FILLED", "buy_fill": 80.0, "sell_fill": 150.0}


def test_enter_spread_aborts_when_buy_leg_fails(client, monkeypatch):
    def fake_place_leg(client, instrument_token, transaction_type, quantity, dry_run=True):
        if transaction_type == "BUY":
            return {"status": "REJECTED", "average_price": None, "order_id": "X"}
        return {"status": "COMPLETE", "average_price": 150.0, "order_id": "X"}

    monkeypatch.setattr(execution, "place_leg", fake_place_leg)
    result = execution.enter_spread(client, sell_instrument(), buy_instrument(), 75, dry_run=True)
    assert result == {"status": "ABORTED_NO_FILL", "leg": "BUY"}


def test_enter_spread_partial_when_sell_leg_fails(client, monkeypatch):
    def fake_place_leg(client, instrument_token, transaction_type, quantity, dry_run=True):
        if transaction_type == "SELL":
            return {"status": "REJECTED", "average_price": None, "order_id": "X"}
        return {"status": "COMPLETE", "average_price": 80.0, "order_id": "X"}

    monkeypatch.setattr(execution, "place_leg", fake_place_leg)
    result = execution.enter_spread(client, sell_instrument(), buy_instrument(), 75, dry_run=True)
    assert result == {"status": "PARTIAL", "buy_fill": 80.0, "sell_fill": None}


# =============================================================================
# exit_spread -- re-resolves each leg's instrument_token from the saved position
# =============================================================================

@pytest.fixture
def open_position():
    return {
        "quantity": 75,
        "direction": "BEARISH",  # -> option_type CE, per strategy.option_type_for_direction
        "expiry_date": "2026-09-16",
        "sell_leg": {"tradingsymbol": "NIFTY2591625000CE", "strike": 25000, "fill_price": 150.0},
        "buy_leg": {"tradingsymbol": "NIFTY2591625200CE", "strike": 25200, "fill_price": 80.0},
    }


def test_exit_spread_closes_both_legs(client, open_position):
    client.options[(25000, "CE", date(2026, 9, 16))] = {
        "tradingsymbol": "NIFTY2591625000CE", "instrument_token": SELL_TOKEN, "lot_size": 75,
    }
    client.options[(25200, "CE", date(2026, 9, 16))] = {
        "tradingsymbol": "NIFTY2591625200CE", "instrument_token": BUY_TOKEN, "lot_size": 75,
    }

    result = execution.exit_spread(client, open_position, dry_run=True)
    assert result == {"status": "CLOSED", "close_short_fill": 150.0, "close_long_fill": 80.0}


def test_exit_spread_skips_short_leg_close_on_partial_entry(client, open_position):
    open_position["sell_leg"]["fill_price"] = None  # PARTIAL entry: short never filled
    client.options[(25200, "CE", date(2026, 9, 16))] = {
        "tradingsymbol": "NIFTY2591625200CE", "instrument_token": BUY_TOKEN, "lot_size": 75,
    }

    result = execution.exit_spread(client, open_position, dry_run=True)
    assert result == {"status": "CLOSED", "close_short_fill": None, "close_long_fill": 80.0}
