from datetime import datetime, timedelta

import config
from broker.market_stream import MarketStream, get_ltp_with_fallback


class FakeXTSClient:
    def __init__(self):
        self.base_url = "https://example.test"
        self.market_token = "tok"
        self.client_id = "C1"
        self.rest_ltp = 100.0
        self.rest_calls = 0

    def get_ltp(self, segment, instrument_id):
        self.rest_calls += 1
        return self.rest_ltp


def make_stream(connected=True, rest_fallback=False):
    stream = MarketStream(FakeXTSClient())
    stream._connected = connected
    stream._rest_fallback_mode = rest_fallback
    return stream


def feed_tick(stream, instrument_id, ltp, ts):
    with stream._cache_lock:
        stream._cache[instrument_id] = (ltp, ts)


# =============================================================================
# Tick cache / staleness guard
# =============================================================================

def test_get_cached_returns_none_before_any_tick():
    stream = make_stream()
    assert stream.get_cached(12345) is None


def test_fresh_tick_is_used_directly():
    stream = make_stream()
    now = datetime.now(config.IST)
    feed_tick(stream, 12345, 25100.0, now)
    client = stream.client
    ltp = get_ltp_with_fallback(stream, client, 2, 12345, stale_seconds=10)
    assert ltp == 25100.0
    assert client.rest_calls == 0  # never fell back


def test_stale_tick_falls_back_to_rest():
    stream = make_stream()
    old = datetime.now(config.IST) - timedelta(seconds=30)
    feed_tick(stream, 12345, 25100.0, old)
    client = stream.client
    ltp = get_ltp_with_fallback(stream, client, 2, 12345, stale_seconds=10)
    assert ltp == client.rest_ltp
    assert client.rest_calls == 1


def test_missing_tick_falls_back_to_rest():
    stream = make_stream()
    client = stream.client
    ltp = get_ltp_with_fallback(stream, client, 2, 99999, stale_seconds=10)
    assert ltp == client.rest_ltp
    assert client.rest_calls == 1


def test_disconnected_stream_falls_back_to_rest_even_with_fresh_tick():
    stream = make_stream(connected=False)
    feed_tick(stream, 12345, 25100.0, datetime.now(config.IST))
    client = stream.client
    ltp = get_ltp_with_fallback(stream, client, 2, 12345, stale_seconds=10)
    assert ltp == client.rest_ltp
    assert client.rest_calls == 1


def test_rest_fallback_mode_always_uses_rest_even_with_fresh_tick():
    stream = make_stream(rest_fallback=True)
    feed_tick(stream, 12345, 25100.0, datetime.now(config.IST))
    client = stream.client
    ltp = get_ltp_with_fallback(stream, client, 2, 12345, stale_seconds=10)
    assert ltp == client.rest_ltp
    assert client.rest_calls == 1


def test_no_stream_at_all_uses_rest():
    client = FakeXTSClient()
    ltp = get_ltp_with_fallback(None, client, 2, 12345, stale_seconds=10)
    assert ltp == client.rest_ltp
    assert client.rest_calls == 1


def test_is_stale_reflects_cache_age():
    stream = make_stream()
    assert stream.is_stale(12345, max_age_seconds=10)  # never ticked -> stale
    feed_tick(stream, 12345, 25100.0, datetime.now(config.IST))
    assert not stream.is_stale(12345, max_age_seconds=10)
    feed_tick(stream, 12345, 25100.0, datetime.now(config.IST) - timedelta(seconds=20))
    assert stream.is_stale(12345, max_age_seconds=10)


# =============================================================================
# Tick message handling (ignoring bad ticks)
# =============================================================================

def test_tick_message_ignores_non_positive_ltp():
    stream = make_stream()
    stream._on_tick_message({"ExchangeInstrumentID": 1, "LastTradedPrice": 0, "LastTradedTime": "1760000000"})
    assert stream.get_cached(1) is None


def test_tick_message_ignores_missing_fields():
    stream = make_stream()
    stream._on_tick_message({"ExchangeInstrumentID": 1})
    assert stream.get_cached(1) is None


def test_tick_message_accepts_valid_tick_within_market_hours():
    stream = make_stream()
    # 2026-09-16 10:00:00 IST as a Unix epoch second.
    ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=config.IST)
    epoch = int(ts.timestamp())
    stream._on_tick_message({"ExchangeInstrumentID": 1, "LastTradedPrice": 25100.5, "LastTradedTime": str(epoch)})
    cached = stream.get_cached(1)
    assert cached is not None
    assert cached[0] == 25100.5


def test_tick_message_ignores_ticks_outside_market_hours():
    stream = make_stream()
    ts = datetime(2026, 9, 16, 20, 0, 0, tzinfo=config.IST)  # 8pm, well after close
    epoch = int(ts.timestamp())
    stream._on_tick_message({"ExchangeInstrumentID": 1, "LastTradedPrice": 25100.5, "LastTradedTime": str(epoch)})
    assert stream.get_cached(1) is None


# =============================================================================
# Reconnect-exhausted -> REST fallback mode
# =============================================================================

def test_reconnect_exhaustion_switches_to_rest_fallback_mode(monkeypatch):
    stream = make_stream(connected=False)
    monkeypatch.setattr(config, "RECONNECT_MAX_ATTEMPTS", 2)

    def always_fails(path=None):
        raise ConnectionError("still down")

    monkeypatch.setattr(stream, "connect", always_fails)
    monkeypatch.setattr("broker.market_stream.time_module.sleep", lambda _: None)

    stream._reconnect_with_backoff()
    assert stream.is_rest_fallback_mode()
