"""
XTS Socket.IO market-data stream, with REST fallback behind one
interface (`get_ltp_with_fallback`) -- strategy/script code never
touches python-socketio or decides which source is live.

CONFIRM BEFORE LIVE -- *no* code anywhere, including the
Nifty-option-BTST reference repo (REST-only), has ever connected to
Jainam's XTS WebSocket feed. This is new, unverified code.
scripts/socket_smoke_test.py tests it first, per explicit request,
before run_entry.py/run_monitor_exit.py ever depend on it.
  - Host/path: every verified REST market-data call on this account
    needed the `/apibinarymarketdata/` prefix instead of Symphony's
    documented `/apimarketdata/` (see xts_client.py's module
    docstring) -- so `config.JAINAM_SOCKET_PATH` defaults to the
    binary-prefixed path as the best guess, but
    scripts/socket_smoke_test.py can be pointed at either and will
    report which one (if either) actually connects.
  - Event names (`1512-json-full` for LTP, `1501-json-full` for
    touchline) and the query-string auth convention (token/userID/
    publishFormat/broadcastMode appended to the connect URL) are per
    Symphony's generic docs -- unverified against this deployment.
  - The subscribe REST call (`POST /instruments/subscription`) path is
    relative to the SAME prefix as the rest of the market-data REST
    calls (`/apibinarymarketdata/instruments/subscription`) by analogy
    with every other verified endpoint in xts_client.py -- also
    unverified until the smoke test subscribes something and a tick
    actually arrives.
"""

import json
import logging
import threading
import time as time_module
from datetime import datetime
from urllib.parse import urlencode

import socketio

import config
from broker.xts_client import XTSDataClient

logger = logging.getLogger(__name__)

LTP_MESSAGE_CODE = 1512
TOUCHLINE_MESSAGE_CODE = 1501


class MarketStream:
    def __init__(self, client: XTSDataClient, on_tick=None):
        """on_tick, if given: Callable[[int, float, datetime], None],
        called (exchange_instrument_id, ltp, exchange_timestamp) for
        every accepted tick -- in addition to the internal cache update."""
        self.client = client
        self.on_tick = on_tick
        self._sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self._cache: dict[int, tuple[float, datetime]] = {}
        self._cache_lock = threading.Lock()
        self._subscribed: set[tuple[int, int]] = set()  # (exchangeSegment, exchangeInstrumentID)
        self._connected = False
        self._rest_fallback_mode = False
        self._stop_requested = False
        self._reconnect_thread: threading.Thread | None = None
        self._register_handlers()

    # --- connection -------------------------------------------------------

    def connect(self, path: str | None = None) -> None:
        """Raises on failure -- callers decide whether to fall back to
        pure REST polling for the whole session (see
        get_ltp_with_fallback, which does this automatically once
        is_connected() is False and reconnection has given up)."""
        path = path or config.JAINAM_SOCKET_PATH
        query = urlencode({
            "token": self.client.market_token,
            "userID": self.client.client_id or "",
            "publishFormat": "JSON",
            "broadcastMode": "Full",
        })
        url = f"{self.client.base_url}?{query}"
        logger.info("Connecting to market-data socket: %s (socketio_path=%s)", self.client.base_url, path)
        self._sio.connect(url, socketio_path=path.lstrip("/"), transports=["websocket"], wait_timeout=10)
        self._connected = True
        self._rest_fallback_mode = False

    def disconnect(self) -> None:
        self._stop_requested = True
        if self._subscribed:
            try:
                self.unsubscribe(list(self._subscribed))
            except Exception:
                logger.exception("Error unsubscribing during disconnect")
        try:
            self._sio.disconnect()
        except Exception:
            logger.exception("Error disconnecting socket")
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def is_rest_fallback_mode(self) -> bool:
        return self._rest_fallback_mode

    # --- subscriptions ------------------------------------------------------

    def subscribe(self, instruments: list[tuple[int, int]], message_code: int = LTP_MESSAGE_CODE) -> None:
        """instruments: list of (exchangeSegment, exchangeInstrumentID)."""
        body = {
            "instruments": [
                {"exchangeSegment": seg, "exchangeInstrumentID": iid} for seg, iid in instruments
            ],
            "xtsMessageCode": message_code,
        }
        self.client._market_post("/apibinarymarketdata/instruments/subscription", body)
        self._subscribed.update(instruments)

    def unsubscribe(self, instruments: list[tuple[int, int]]) -> None:
        body = {
            "instruments": [
                {"exchangeSegment": seg, "exchangeInstrumentID": iid} for seg, iid in instruments
            ],
            "xtsMessageCode": LTP_MESSAGE_CODE,
        }
        try:
            self.client._market_post("/apibinarymarketdata/instruments/subscription/unsubscribe", body)
        finally:
            self._subscribed.difference_update(instruments)

    # --- tick cache ---------------------------------------------------------

    def get_cached(self, exchange_instrument_id: int) -> tuple[float, datetime] | None:
        with self._cache_lock:
            return self._cache.get(exchange_instrument_id)

    def is_stale(self, exchange_instrument_id: int, max_age_seconds: float) -> bool:
        cached = self.get_cached(exchange_instrument_id)
        if cached is None:
            return True
        _, ts = cached
        age = (datetime.now(ts.tzinfo) - ts).total_seconds()
        return age > max_age_seconds

    # --- internals -----------------------------------------------------------

    def _register_handlers(self) -> None:
        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on(f"{LTP_MESSAGE_CODE}-json-full", self._on_tick_message)
        self._sio.on(f"{TOUCHLINE_MESSAGE_CODE}-json-full", self._on_tick_message)

    def _on_connect(self) -> None:
        logger.info("Market-data socket connected")
        self._connected = True

    def _on_disconnect(self) -> None:
        logger.warning("Market-data socket disconnected")
        self._connected = False
        if self._stop_requested:
            return
        self._start_reconnect_loop()

    def _on_tick_message(self, data) -> None:
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                logger.warning("Unparseable tick payload: %r", data[:200])
                return
        ltp = data.get("LastTradedPrice")
        instrument_id = data.get("ExchangeInstrumentID")
        if ltp is None or instrument_id is None:
            return
        ltp = float(ltp)
        if ltp <= 0:
            return

        ts = _parse_tick_timestamp(data.get("LastTradedTime"))
        if ts is None or not (config.MARKET_OPEN <= ts.time() <= config.MARKET_CLOSE):
            return

        with self._cache_lock:
            self._cache[int(instrument_id)] = (ltp, ts)
        if self.on_tick:
            self.on_tick(int(instrument_id), ltp, ts)

    def _start_reconnect_loop(self) -> None:
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_with_backoff, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_with_backoff(self) -> None:
        attempt = 0
        while not self._stop_requested and attempt < config.RECONNECT_MAX_ATTEMPTS:
            attempt += 1
            backoff = min(2 ** attempt, 60)
            logger.warning("Reconnect attempt %d/%d in %ds", attempt, config.RECONNECT_MAX_ATTEMPTS, backoff)
            time_module.sleep(backoff)
            try:
                self.connect()
                if self._subscribed:
                    self.subscribe(list(self._subscribed))
                logger.info("Reconnected and re-subscribed %d instrument(s)", len(self._subscribed))
                return
            except Exception:
                logger.exception("Reconnect attempt %d failed", attempt)

        if not self._stop_requested:
            self._rest_fallback_mode = True
            logger.critical(
                "Market-data socket failed to reconnect after %d attempts -- "
                "switching to REST polling for the rest of this session.",
                config.RECONNECT_MAX_ATTEMPTS,
            )


def _parse_tick_timestamp(raw) -> datetime | None:
    """XTS touchline/LTP ticks carry an exchange timestamp whose exact
    format is unverified for this deployment (seconds-epoch vs
    milliseconds vs ISO string all appear in different Symphony docs
    examples) -- this accepts the common shapes and returns None rather
    than guess on an unrecognized one, which the staleness guard then
    treats as "no usable tick"."""
    if raw is None:
        return None
    try:
        value = int(raw)
        if value > 10**12:  # looks like milliseconds
            value //= 1000
        return datetime.fromtimestamp(value, tz=config.IST)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw)).replace(tzinfo=config.IST)
    except ValueError:
        return None


def get_ltp_with_fallback(
    stream: MarketStream | None,
    client: XTSDataClient,
    exchange_segment: int,
    exchange_instrument_id: int,
    stale_seconds: float = None,
    log=logger.warning,
) -> float:
    """The single interface strategy/script code calls for a live
    price: tries the stream cache first, falls back to a REST get_ltp
    call if the stream is absent, disconnected, in REST-fallback mode,
    or the cached tick is stale."""
    stale_seconds = config.STALE_TICK_SECONDS if stale_seconds is None else stale_seconds

    if stream is not None and stream.is_connected() and not stream.is_rest_fallback_mode():
        cached = stream.get_cached(exchange_instrument_id)
        if cached is not None:
            ltp, ts = cached
            age = (datetime.now(ts.tzinfo) - ts).total_seconds()
            if age <= stale_seconds:
                return ltp
            log(f"Stale tick for instrument {exchange_instrument_id} ({age:.1f}s old) -- falling back to REST")
        else:
            log(f"No cached tick yet for instrument {exchange_instrument_id} -- falling back to REST")
    return client.get_ltp(exchange_segment, exchange_instrument_id)
