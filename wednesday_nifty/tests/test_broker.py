import json
from datetime import date, datetime, timedelta

import pytest

from wednesday_nifty import broker, config
from wednesday_nifty.tests.fake_kite import FakeKite


# =============================================================================
# Session handling
# =============================================================================

@pytest.fixture(autouse=True)
def isolated_session_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "session.json")


def test_load_cached_token_returns_none_when_no_file():
    assert broker.load_cached_token() is None


def test_save_and_load_cached_token_round_trips_for_today():
    broker.save_session("abc123")
    assert broker.load_cached_token() == "abc123"


def test_stale_cached_token_from_a_previous_day_is_rejected():
    config.SESSION_FILE.write_text(json.dumps({"access_token": "old-token", "date": "2020-01-01"}))
    assert broker.load_cached_token() is None


def test_get_kite_client_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "KITE_API_KEY", "")
    assert broker.get_kite_client() is None


def test_get_kite_client_returns_none_without_valid_session(monkeypatch):
    monkeypatch.setattr(config, "KITE_API_KEY", "some-key")
    assert broker.get_kite_client() is None  # no session cached


# =============================================================================
# Instrument resolution
# =============================================================================

@pytest.fixture
def kite_with_instruments():
    k = FakeKite()
    k.instrument_rows = [
        {"name": "NIFTY", "segment": "NFO-FUT", "tradingsymbol": "NIFTY25SEPFUT",
         "instrument_token": 1001, "expiry": "2026-09-30", "strike": 0,
         "instrument_type": "FUT", "lot_size": 75},
        {"name": "NIFTY", "segment": "NFO-FUT", "tradingsymbol": "NIFTY25OCTFUT",
         "instrument_token": 1002, "expiry": "2026-10-28", "strike": 0,
         "instrument_type": "FUT", "lot_size": 75},
        {"name": "NIFTY", "segment": "NFO-OPT", "tradingsymbol": "NIFTY2591625000CE",
         "instrument_token": 2001, "expiry": "2026-09-16", "strike": 25000,
         "instrument_type": "CE", "lot_size": 75},
        {"name": "NIFTY", "segment": "NFO-OPT", "tradingsymbol": "NIFTY2592325000CE",
         "instrument_token": 2002, "expiry": "2026-09-23", "strike": 25000,
         "instrument_type": "CE", "lot_size": 75},
    ]
    return k


@pytest.fixture(autouse=True)
def isolated_instrument_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTRUMENTS_CACHE_DIR", tmp_path)


def test_resolve_current_month_future_picks_nearest_expiry(kite_with_instruments):
    future = broker.resolve_current_month_future(kite_with_instruments, today=date(2026, 9, 16))
    assert future.tradingsymbol == "NIFTY25SEPFUT"
    assert future.expiry == date(2026, 9, 30)


def test_resolve_weekly_expiry_never_assumes_a_weekday(kite_with_instruments):
    expiry = broker.resolve_weekly_expiry(kite_with_instruments, trade_date=date(2026, 9, 16))
    assert expiry == date(2026, 9, 16)  # observed live from the dump, not hardcoded


def test_resolve_option_finds_matching_instrument(kite_with_instruments):
    opt = broker.resolve_option(kite_with_instruments, expiry=date(2026, 9, 16), strike=25000, option_type="CE")
    assert opt.tradingsymbol == "NIFTY2591625000CE"
    assert opt.lot_size == 75


def test_resolve_option_raises_when_not_found(kite_with_instruments):
    with pytest.raises(LookupError):
        broker.resolve_option(kite_with_instruments, expiry=date(2026, 9, 16), strike=99999, option_type="CE")


# =============================================================================
# NSE holiday lookup
# =============================================================================

@pytest.fixture(autouse=True)
def isolated_holiday_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOLIDAY_CACHE_FILE", tmp_path / "holiday_cache.json")
    monkeypatch.setattr(config, "HOLIDAY_OVERRIDE_FILE", tmp_path / "holiday_override.json")


def test_is_trading_holiday_uses_live_fetch(monkeypatch):
    mocked_holiday = date(2026, 10, 2)  # e.g. Gandhi Jayanti, mocked here rather than hardcoded live
    monkeypatch.setattr(broker, "_fetch_live_holidays", lambda: {mocked_holiday})

    assert broker.is_trading_holiday(mocked_holiday)
    assert not broker.is_trading_holiday(date(2026, 10, 1))


def test_falls_back_to_cache_when_live_fetch_fails(monkeypatch):
    cached_holiday = date(2026, 11, 5)
    config.HOLIDAY_CACHE_FILE.write_text(json.dumps({
        "fetched_at": "2026-01-01T00:00:00",
        "dates": [cached_holiday.isoformat()],
    }))

    def boom():
        raise ConnectionError("NSE unreachable")

    monkeypatch.setattr(broker, "_fetch_live_holidays", boom)

    assert broker.is_trading_holiday(cached_holiday)


def test_raises_when_no_live_no_cache_no_override(monkeypatch):
    def boom():
        raise ConnectionError("NSE unreachable")

    monkeypatch.setattr(broker, "_fetch_live_holidays", boom)

    with pytest.raises(broker.HolidayLookupError):
        broker.is_trading_holiday(date(2026, 10, 2))


def test_override_file_is_unioned_with_live_result(monkeypatch):
    monkeypatch.setattr(broker, "_fetch_live_holidays", lambda: {date(2026, 10, 2)})
    override_date = date(2026, 12, 25)
    config.HOLIDAY_OVERRIDE_FILE.write_text(json.dumps({"dates": [override_date.isoformat()]}))

    assert broker.is_trading_holiday(override_date)
    assert broker.is_trading_holiday(date(2026, 10, 2))


# =============================================================================
# Futures daily-candle fetch (thin wrapper)
# =============================================================================

def test_fetch_futures_daily_candles_queries_a_window_ending_before_trading_day():
    k = FakeKite()
    k.historical[1001] = [{"date": datetime(2026, 9, 15), "high": 25300, "low": 25050}]
    result = broker.fetch_futures_daily_candles(k, 1001, trading_day=date(2026, 9, 16))
    assert result == k.historical[1001]
