import pytest

from results import trade_history as th


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(th, "HISTORY_PATH", tmp_path / "trade_history.json")


def _legs(short_exit=100.0, hedge_exit=40.0):
    return {
        "SHORT_ATM": {"role": "SHORT_ATM", "quantity": 260, "avg_price": 160.0, "exit_price": short_exit},
        "LONG_HEDGE": {"role": "LONG_HEDGE", "quantity": 260, "avg_price": 80.0, "exit_price": hedge_exit},
    }


def test_gross_pnl_profit_when_short_falls_and_hedge_falls_less():
    # Short sold 160 bought back 100 -> +60/unit. Hedge bought 80 sold 40 -> -40/unit.
    pnl = th.gross_pnl(_legs(short_exit=100.0, hedge_exit=40.0))
    assert pnl == (160.0 - 100.0) * 260 + (40.0 - 80.0) * 260


def test_gross_pnl_none_when_a_leg_has_no_exit_price():
    legs = _legs()
    del legs["LONG_HEDGE"]["exit_price"]
    assert th.gross_pnl(legs) is None


def test_gross_pnl_none_when_a_leg_is_missing():
    assert th.gross_pnl({"SHORT_ATM": _legs()["SHORT_ATM"]}) is None


def test_append_closed_trade_round_trips_and_computes_pnl():
    position = {
        "entry_date": "2026-11-18",
        "trigger": "TIME_1435_RED",
        "expiry": "2026-11-23",
        "exit_reason": "TIME_EXIT",
        "entry_credit": 79.1,
        "closed_date": "2026-11-23",
        "legs": [
            {"role": "SHORT_ATM", "option_type": "CE", "quantity": 260, "avg_price": 163.5,
             "exit_price": 136.7, "exit_quantity": 260, "exit_timestamp": "2026-11-23T15:15:07+05:30"},
            {"role": "LONG_HEDGE", "option_type": "CE", "quantity": 260, "avg_price": 84.4,
             "exit_price": 4.5, "exit_quantity": 260, "exit_timestamp": "2026-11-23T15:15:07+05:30"},
        ],
    }
    record = th.append_closed_trade(position)

    assert record["entry_date"] == "2026-11-18"
    assert record["exit_reason"] == "TIME_EXIT"
    assert record["exit_date"] == "2026-11-23"
    assert record["gross_pnl"] == pytest.approx((163.5 - 136.7) * 260 + (4.5 - 84.4) * 260)

    history = th.load_trade_history()
    assert len(history) == 1
    assert history[0] == record


def test_append_closed_trade_appends_not_overwrites():
    position = {
        "entry_date": "2026-11-04", "trigger": "HIGH_BREAKOUT", "expiry": "2026-11-09",
        "exit_reason": "STOP_LOSS", "entry_credit": 80.1, "closed_date": "2026-11-05",
        "legs": [
            {"role": "SHORT_ATM", "option_type": "PE", "quantity": 260, "avg_price": 200.24, "exit_price": 220.0},
            {"role": "LONG_HEDGE", "option_type": "PE", "quantity": 260, "avg_price": 120.14, "exit_price": 110.0},
        ],
    }
    th.append_closed_trade(position)
    th.append_closed_trade({**position, "entry_date": "2026-11-11"})
    assert len(th.load_trade_history()) == 2


def test_load_trade_history_returns_empty_list_when_no_file():
    assert th.load_trade_history() == []
