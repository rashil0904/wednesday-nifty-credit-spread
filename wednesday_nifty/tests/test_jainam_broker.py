from datetime import date, time

import pytest

from wednesday_nifty import config
from wednesday_nifty.jainam import broker
from wednesday_nifty.jainam.client import NSE_FO_SEGMENT, Candle
from wednesday_nifty.zerodha import broker as zerodha_broker
from wednesday_nifty.tests.fake_xts import FakeXTSClient


# =============================================================================
# Session handling
# =============================================================================

def test_get_client_returns_none_when_config_missing(monkeypatch):
    monkeypatch.setattr(config, "JAINAM_BASE_URL", "")
    assert broker.get_client() is None


def test_get_client_returns_none_when_login_raises(monkeypatch):
    monkeypatch.setattr(config, "JAINAM_BASE_URL", "https://example.com")
    monkeypatch.setattr(config, "JAINAM_MARKET_API_KEY", "k")
    monkeypatch.setattr(config, "JAINAM_MARKET_API_SECRET", "s")
    monkeypatch.setattr(config, "JAINAM_INTERACTIVE_API_KEY", "k2")
    monkeypatch.setattr(config, "JAINAM_INTERACTIVE_API_SECRET", "s2")

    def boom(*args, **kwargs):
        raise RuntimeError("login failed")

    monkeypatch.setattr(broker.XTSDataClient, "from_env", boom)
    assert broker.get_client() is None


# =============================================================================
# Instrument resolution
# =============================================================================

def test_resolve_current_month_future_wraps_client_result():
    client = FakeXTSClient()
    client.futures[date(2026, 9, 16)] = {
        "tradingsymbol": "NIFTY26SEPFUT", "instrument_token": 5001, "expiry": date(2026, 9, 29),
    }
    future = broker.resolve_current_month_future(client, date(2026, 9, 16))
    assert future.tradingsymbol == "NIFTY26SEPFUT"
    assert future.instrument_token == 5001
    assert future.expiry == date(2026, 9, 29)


def test_resolve_weekly_expiry_never_assumes_a_weekday():
    client = FakeXTSClient()
    client.option_expiries = [date(2026, 9, 16), date(2026, 9, 23)]
    expiry = broker.resolve_weekly_expiry(client, trade_date=date(2026, 9, 16))
    assert expiry == date(2026, 9, 16)


def test_resolve_option_wraps_client_result():
    client = FakeXTSClient()
    client.options[(25000, "CE", date(2026, 9, 16))] = {
        "tradingsymbol": "NIFTY2591625000CE", "instrument_token": 7001, "lot_size": 75,
    }
    opt = broker.resolve_option(client, expiry=date(2026, 9, 16), strike=25000, option_type="CE")
    assert opt.tradingsymbol == "NIFTY2591625000CE"
    assert opt.instrument_token == 7001
    assert opt.lot_size == 75
    assert opt.strike == 25000
    assert opt.expiry == date(2026, 9, 16)


# =============================================================================
# Quotes / candles
# =============================================================================

def test_get_nifty_spot_ltp_delegates_to_client():
    client = FakeXTSClient()
    client.spot_ltp = 25123.45
    assert broker.get_nifty_spot_ltp(client) == 25123.45


def test_fetch_futures_daily_candles_returns_kite_shaped_dicts():
    client = FakeXTSClient()
    client.daily_candles[5001] = [
        Candle(timestamp=date(2026, 9, 15), open=25100, high=25300, low=25050, close=25200),
    ]
    result = broker.fetch_futures_daily_candles(client, 5001, trading_day=date(2026, 9, 16))
    assert result == [{"date": date(2026, 9, 15), "open": 25100, "high": 25300, "low": 25050, "close": 25200}]


def test_get_futures_opening_price_reads_0915_candle_open():
    client = FakeXTSClient()
    future = broker.FuturesContract(tradingsymbol="NIFTY26SEPFUT", instrument_token=5001, expiry=None)
    client.candles_at[(NSE_FO_SEGMENT, 5001, date(2026, 9, 16), time(9, 15))] = Candle(
        timestamp=None, open=25050, high=25100, low=25000, close=25080,
    )
    assert broker.get_futures_opening_price(client, future, date(2026, 9, 16)) == 25050


def test_get_latest_futures_price_reads_ltp():
    client = FakeXTSClient()
    future = broker.FuturesContract(tradingsymbol="NIFTY26SEPFUT", instrument_token=5001, expiry=None)
    client.ltps[(NSE_FO_SEGMENT, 5001)] = 25123.45
    assert broker.get_latest_futures_price(client, future) == 25123.45


# =============================================================================
# Holiday lookup is re-exported from zerodha.broker, not reimplemented
# =============================================================================

def test_holiday_lookup_is_reexported_from_zerodha_broker():
    assert broker.is_trading_holiday is zerodha_broker.is_trading_holiday
    assert broker.HolidayLookupError is zerodha_broker.HolidayLookupError
