from datetime import datetime

import pytest

from wednesday_nifty import broker, config, execution, monitor
from wednesday_nifty.tests.fake_kite import FakeKite


# =============================================================================
# CLI dispatch
# =============================================================================

def test_cli_rejects_invalid_or_missing_args():
    assert monitor.main([]) == 2
    assert monitor.main(["bogus"]) == 2


def test_cli_dispatches_to_entry_monitor(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "run_entry_monitor", lambda: calls.append("entry") or 0)
    assert monitor.main(["entry"]) == 0
    assert calls == ["entry"]


def test_cli_dispatches_to_exit_monitor(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "run_exit_monitor", lambda: calls.append("exit") or 0)
    assert monitor.main(["exit"]) == 0
    assert calls == ["exit"]


# =============================================================================
# Market-hours gate (exit monitor's cheap no-op path)
# =============================================================================

def test_within_market_hours_true_during_session():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=config.IST)  # Wednesday noon
    assert monitor._within_market_hours(now)


def test_within_market_hours_false_before_open():
    now = datetime(2026, 9, 16, 9, 0, tzinfo=config.IST)
    assert not monitor._within_market_hours(now)


def test_within_market_hours_false_after_close():
    now = datetime(2026, 9, 16, 16, 0, tzinfo=config.IST)
    assert not monitor._within_market_hours(now)


def test_within_market_hours_false_on_weekend():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=config.IST)  # Saturday
    assert not monitor._within_market_hours(now)


# =============================================================================
# Exit monitor no-ops cheaply (never touches the broker) when there's
# nothing to do
# =============================================================================

def test_exit_monitor_noops_outside_market_hours(monkeypatch):
    monkeypatch.setattr(monitor, "_within_market_hours", lambda now: False)

    def fail_if_called():
        raise AssertionError("get_kite_client should not be called outside market hours")

    monkeypatch.setattr(broker, "get_kite_client", fail_if_called)
    assert monitor.run_exit_monitor() == 0


def test_exit_monitor_noops_when_no_open_position(monkeypatch):
    monkeypatch.setattr(monitor, "_within_market_hours", lambda now: True)
    monkeypatch.setattr(execution, "get_open_position", lambda: None)

    def fail_if_called():
        raise AssertionError("get_kite_client should not be called when there's no open position")

    monkeypatch.setattr(broker, "get_kite_client", fail_if_called)
    assert monitor.run_exit_monitor() == 0


# =============================================================================
# Thin quote wrappers
# =============================================================================

def test_get_futures_opening_price_reads_ohlc_open():
    k = FakeKite()
    future = broker.FuturesContract(tradingsymbol="NIFTY25SEPFUT", instrument_token=1001,
                                     expiry=None)
    k.quotes["NFO:NIFTY25SEPFUT"] = {"last_price": 25100, "ohlc": {"open": 25050}}
    assert monitor._get_futures_opening_price(k, future) == 25050


def test_get_latest_futures_price_reads_ltp():
    k = FakeKite()
    future = broker.FuturesContract(tradingsymbol="NIFTY25SEPFUT", instrument_token=1001,
                                     expiry=None)
    k.ltps["NFO:NIFTY25SEPFUT"] = 25123.45
    assert monitor._get_latest_futures_price(k, future) == 25123.45
