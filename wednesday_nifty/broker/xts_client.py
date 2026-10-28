"""
Jainam (XTS-based) market-data + order-execution layer.

Jainam white-labels Symphony Fintech's XTS Connect API. This talks to
the documented REST endpoints directly with `requests`. Adapted from
(not imported from) the user's Nifty-option-BTST reference repo's
jainam/broker/xts_client.py -- rewritten fresh for this project's own
shapes, carrying forward the facts that repo verified live against this
same Jainam account (base URL https://smpd.jainam.in:3643,
2026-09-30/10-01):

VERIFIED:
  - Market Data login is `POST /apibinarymarketdata/auth/login`, NOT
    `/apimarketdata/auth/login` (404s on this account -- the generic
    reference SDK's route table is wrong for this deployment).
  - `futureSymbol`/`optionsymbol` responses are a list-of-one
    (`{"result": [{...}]}`), not a bare object.
  - OHLC `dataReponse` is comma-separated pipe-delimited rows
    (`timestamp|O|H|L|C|volume|OI`). The timestamp is IST wall-clock time
    encoded AS IF it were a UTC epoch -- reading it with `tz=timezone.utc`
    and relabeling `tzinfo=IST` recovers the correct IST clock time
    directly, no further offset needed. Each row is stamped at `:59`
    seconds (end of its minute), not `:00` -- matching must be done by
    (date, hour, minute), ignoring seconds.
  - `resolve_nifty_option_grid`'s strikePrice endpoint works live despite
    not appearing in the reference SDK's route table (only in the prose
    docs).

STILL UNVERIFIED / KNOWN RISKS for THIS project's credentials (flagged,
not assumed):
  - Interactive API: the reference author's own Interactive key/secret
    were rejected ("Entered invalid credentials") on this same account.
    Our `.env` has different Interactive credentials -- untested until
    the first real login attempt.
  - NSEFO (futures/options) LTP returned an empty `listQuotes` when the
    reference tested after market hours; VIX (NSECM) worked fine
    after-hours in the same session. Unclear if this is a closed-market
    caching gap or a segment-entitlement issue -- needs a during-market-
    hours check.
  - `JAINAM_CLIENT_ID` is required for order placement and net-position
    checks but is not yet in this project's .env.
"""

import time as _time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import config

IST = ZoneInfo("Asia/Kolkata")

NSE_CM_SEGMENT = 1  # equity cash + indices
NSE_FO_SEGMENT = 2  # futures & options


class InstrumentNotFoundError(Exception):
    """Raised when a required instrument can't be resolved. Never guess a symbol."""


class XTSOrderError(Exception):
    """Raised for any failed Interactive (orders/positions) call."""


class CandleNotFoundError(Exception):
    """Raised when the expected candle isn't present in the OHLC response."""


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


def _fmt_expiry(d: date) -> str:
    """XTS instrument-lookup endpoints take expiry as DDMonYYYY, e.g. 30Jan2025."""
    return d.strftime("%d%b%Y")


def _fmt_ohlc_time(dt: datetime) -> str:
    """OHLC start/end time format per docs, e.g. 'Jan 27 2025 090000'."""
    return dt.strftime("%b %d %Y %H%M%S")


def _parse_ohlc_rows(raw: str) -> list[Candle]:
    """Parses the comma-separated, pipe-delimited OHLC blob. See module
    docstring for the verified timestamp convention. Returns
    chronologically-ascending Candle objects."""
    rows = []
    for row in raw.split(","):
        fields = row.split("|")
        if len(fields) < 5 or not fields[0]:
            continue
        row_dt = datetime.fromtimestamp(int(fields[0]), tz=timezone.utc).replace(tzinfo=IST)
        rows.append(Candle(
            timestamp=row_dt, open=float(fields[1]), high=float(fields[2]),
            low=float(fields[3]), close=float(fields[4]),
        ))
    rows.sort(key=lambda c: c.timestamp)
    return rows


class XTSDataClient:
    def __init__(self, base_url: str, market_token: str, interactive_token: str | None):
        self.base_url = base_url.rstrip("/")
        self.market_token = market_token
        self.interactive_token = interactive_token
        self._interactive_error: str | None = None
        self.client_id: str | None = None
        self._session = requests.Session()
        self._index_list_cache: dict[int, dict[str, int]] = {}
        # Credentials needed to re-login if a token is rejected mid-run
        # (see _request's relogin param) -- set by from_env(), None if
        # constructed directly (e.g. in tests that never trigger a retry).
        self._market_api_key: str | None = None
        self._market_api_secret: str | None = None
        self._interactive_api_key: str | None = None
        self._interactive_api_secret: str | None = None
        self._source: str | None = None

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "XTSDataClient":
        """Logs into Market Data (required) and, if Interactive
        credentials are present, Interactive too. A failed or missing
        Interactive login does NOT raise here -- it's deferred until a
        method that needs it is called, so market-data-only usage (dry
        run) isn't blocked by it."""
        if env_path:
            load_dotenv(env_path)

        base_url = config.JAINAM_BASE_URL
        market_api_key = config.JAINAM_MARKET_API_KEY
        market_api_secret = config.JAINAM_MARKET_API_SECRET
        interactive_api_key = config.JAINAM_INTERACTIVE_API_KEY
        interactive_api_secret = config.JAINAM_INTERACTIVE_API_SECRET
        source = config.JAINAM_SOURCE

        missing = [
            name for name, val in {
                "JAINAM_BASE_URL": base_url,
                "JAINAM_MARKET_API_KEY": market_api_key,
                "JAINAM_MARKET_API_SECRET": market_api_secret,
            }.items() if not val
        ]
        if missing:
            raise RuntimeError(f"Missing in .env: {', '.join(missing)}")

        session = requests.Session()
        market_token = _login(
            session, base_url, "/apibinarymarketdata/auth/login", market_api_key, market_api_secret, source
        )

        interactive_token = None
        interactive_error = None
        if interactive_api_key and interactive_api_secret:
            try:
                interactive_token = _login(
                    session, base_url, "/interactive/user/session",
                    interactive_api_key, interactive_api_secret, source,
                )
            except Exception as exc:  # noqa: BLE001 -- deliberately deferred, see docstring
                interactive_error = str(exc)
        else:
            interactive_error = "JAINAM_INTERACTIVE_API_KEY/SECRET not set in .env"

        client = cls(base_url=base_url, market_token=market_token, interactive_token=interactive_token)
        client._session = session
        client._interactive_error = interactive_error
        client.client_id = config.JAINAM_CLIENT_ID or None
        client._market_api_key = market_api_key
        client._market_api_secret = market_api_secret
        client._interactive_api_key = interactive_api_key
        client._interactive_api_secret = interactive_api_secret
        client._source = source
        return client

    # --- low-level request helpers ----------------------------------------

    def _relogin_market(self) -> str:
        self.market_token = _login(
            self._session, self.base_url, "/apibinarymarketdata/auth/login",
            self._market_api_key, self._market_api_secret, self._source,
        )
        return self.market_token

    def _relogin_interactive(self) -> None:
        self.interactive_token = _login(
            self._session, self.base_url, "/interactive/user/session",
            self._interactive_api_key, self._interactive_api_secret, self._source,
        )

    def _market_get(self, path: str, params: dict) -> dict:
        return _request(self._session, "GET", self.base_url, path, self.market_token,
                         params=params, relogin=self._relogin_market)

    def _market_post(self, path: str, body: dict) -> dict:
        return _request(self._session, "POST", self.base_url, path, self.market_token,
                         json_body=body, relogin=self._relogin_market)

    # --- quotes / spot ------------------------------------------------------

    def _index_list(self, exchange_segment: int) -> dict[str, int]:
        """Name -> exchangeInstrumentID for all indices in a segment, e.g. 'NIFTY 50' -> 26000."""
        if exchange_segment not in self._index_list_cache:
            data = self._market_get("/apibinarymarketdata/instruments/indexlist", {"exchangeSegment": exchange_segment})
            mapping = {}
            for entry in data["result"]["indexList"]:
                name, _, instrument_id = entry.rpartition("_")
                mapping[name] = int(instrument_id)
            self._index_list_cache[exchange_segment] = mapping
        return self._index_list_cache[exchange_segment]

    def get_ltp(self, exchange_segment: int, exchange_instrument_id: int) -> float:
        data = self._market_post(
            "/apibinarymarketdata/instruments/quotes",
            {
                "instruments": [
                    {"exchangeSegment": exchange_segment, "exchangeInstrumentID": exchange_instrument_id}
                ],
                "xtsMessageCode": 1501,  # TouchLineEvent -- includes LastTradedPrice
                "publishFormat": "JSON",
            },
        )
        quotes = data["result"]["listQuotes"]
        if not quotes:
            raise InstrumentNotFoundError(
                f"No quote returned for segment={exchange_segment} instrumentID={exchange_instrument_id}"
            )
        import json as _json

        quote = quotes[0]
        if isinstance(quote, str):  # docs show this double-JSON-encoded sometimes
            quote = _json.loads(quote)
        return float(quote["LastTradedPrice"])

    def get_nifty_spot_ltp(self) -> float:
        indices = self._index_list(NSE_CM_SEGMENT)
        if "NIFTY 50" not in indices:
            raise InstrumentNotFoundError("NIFTY 50 not found in NSECM index list")
        return self.get_ltp(NSE_CM_SEGMENT, indices["NIFTY 50"])

    def get_option_ltp(self, exchange_instrument_id: int) -> float:
        """Convenience wrapper: every option/future leg this strategy
        trades is NSEFO, so order_exec.py only needs the instrument id."""
        return self.get_ltp(NSE_FO_SEGMENT, exchange_instrument_id)

    # --- instrument resolution --------------------------------------------

    def get_nifty_fut_instrument(self, as_of: date) -> dict:
        """Resolve the current (nearest-unexpired) NIFTY futures contract as of the given date."""
        data = self._market_get(
            "/apibinarymarketdata/instruments/instrument/expiryDate",
            {"exchangeSegment": NSE_FO_SEGMENT, "series": "FUTIDX", "symbol": "NIFTY"},
        )
        expiries = [datetime.fromisoformat(e).date() for e in data["result"]]
        candidates = sorted(e for e in expiries if e >= as_of)
        if not candidates:
            raise InstrumentNotFoundError(f"No NIFTY future found with expiry >= {as_of}")
        nearest_expiry = candidates[0]

        data = self._market_get(
            "/apibinarymarketdata/instruments/instrument/futureSymbol",
            {
                "exchangeSegment": NSE_FO_SEGMENT, "series": "FUTIDX", "symbol": "NIFTY",
                "expiryDate": _fmt_expiry(nearest_expiry),
            },
        )
        results = data["result"]
        if not results:
            raise InstrumentNotFoundError(f"No NIFTY future found for expiry {nearest_expiry}")
        result = results[0]
        return {
            "tradingsymbol": result["Description"],
            "instrument_token": result["ExchangeInstrumentID"],
            "expiry": nearest_expiry,
        }

    def list_nifty_option_expiries(self, as_of: date) -> list[date]:
        """All live NIFTY weekly-option expiries >= as_of, from the live
        instrument master -- never assumes a weekday, and automatically
        reflects any NSE holiday shift since these are the real listed
        dates, not a calendar computation."""
        data = self._market_get(
            "/apibinarymarketdata/instruments/instrument/expiryDate",
            {"exchangeSegment": NSE_FO_SEGMENT, "series": "OPTIDX", "symbol": "NIFTY"},
        )
        expiries = [datetime.fromisoformat(e).date() for e in data["result"]]
        candidates = sorted(e for e in expiries if e >= as_of)
        if not candidates:
            raise InstrumentNotFoundError(f"No NIFTY option expiry found with expiry >= {as_of}")
        return candidates

    def resolve_nifty_option_grid(self, expiry: date) -> tuple[float, int]:
        """Return (strike_interval, lot_size) for NIFTY options at the
        given expiry, via the strikePrice endpoint (works live despite
        not being in the reference SDK's route table)."""
        data = self._market_get(
            "/apibinarymarketdata/instruments/instrument/strikePrice",
            {
                "exchangeSegment": NSE_FO_SEGMENT, "series": "OPTIDX", "symbol": "NIFTY",
                "expiryDate": _fmt_expiry(expiry), "optionType": "CE",
            },
        )
        strikes = sorted({float(s) for s in data["result"]})
        if len(strikes) < 2:
            raise InstrumentNotFoundError(
                f"Fewer than 2 strikes listed for NIFTY expiry {expiry}; cannot derive interval"
            )
        diffs = [round(b - a, 2) for a, b in zip(strikes, strikes[1:])]
        strike_interval = min(diffs)

        sample = self.resolve_option_instrument(strikes[0], "CE", expiry)
        lot_size = sample["lot_size"]
        return strike_interval, lot_size

    def resolve_option_instrument(self, strike: float, option_type: str, expiry: date) -> dict:
        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")
        data = self._market_get(
            "/apibinarymarketdata/instruments/instrument/optionsymbol",
            {
                "exchangeSegment": NSE_FO_SEGMENT, "series": "OPTIDX", "symbol": "NIFTY",
                "expiryDate": _fmt_expiry(expiry), "optionType": option_type, "strikePrice": strike,
            },
        )
        results = data.get("result")
        if not results:
            raise InstrumentNotFoundError(f"No NIFTY {option_type} found for strike={strike} expiry={expiry}")
        result = results[0]
        return {
            "tradingsymbol": result["Description"],
            "instrument_token": result["ExchangeInstrumentID"],
            "lot_size": result["LotSize"],
        }

    # --- candles ---------------------------------------------------------

    def get_candle_at(self, exchange_segment: int, exchange_instrument_id: int, target_date: date, target_time: time) -> Candle:
        """Fetch the 1-minute candle covering target_date + target_time.
        See module docstring for the verified timestamp convention."""
        start_dt = datetime.combine(target_date, time(9, 0))
        end_dt = datetime.combine(target_date, time(15, 30))
        data = self._market_get(
            "/apibinarymarketdata/instruments/ohlc",
            {
                "exchangeSegment": exchange_segment, "exchangeInstrumentID": exchange_instrument_id,
                "startTime": _fmt_ohlc_time(start_dt), "endTime": _fmt_ohlc_time(end_dt),
                "compressionValue": "60",
            },
        )
        for candle in _parse_ohlc_rows(data["result"]["dataReponse"]):
            if (candle.timestamp.date(), candle.timestamp.hour, candle.timestamp.minute) == \
               (target_date, target_time.hour, target_time.minute):
                return candle
        raise CandleNotFoundError(f"No candle found for {target_date} {target_time} for instrument {exchange_instrument_id}")

    def get_daily_candles(self, exchange_segment: int, exchange_instrument_id: int,
                           from_date: date, to_date: date) -> list[Candle]:
        """Daily OHLC candles in [from_date, to_date], chronologically
        ascending -- used for the 3-day high/low. `compressionValue="D"`
        is an unverified guess by analogy with the documented "60"
        (1-minute) value; confirm against a real response before
        trusting it live (see README open items)."""
        start_dt = datetime.combine(from_date, time(0, 0))
        end_dt = datetime.combine(to_date, time(23, 59))
        data = self._market_get(
            "/apibinarymarketdata/instruments/ohlc",
            {
                "exchangeSegment": exchange_segment, "exchangeInstrumentID": exchange_instrument_id,
                "startTime": _fmt_ohlc_time(start_dt), "endTime": _fmt_ohlc_time(end_dt),
                "compressionValue": "D",
            },
        )
        return _parse_ohlc_rows(data["result"]["dataReponse"])

    # --- live orders (Interactive API) --------------------------------------
    # These place REAL orders. Only called when config.JAINAM_LIVE is True.

    def _interactive_call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        """Interactive request that keeps Jainam's error body, rides out
        429 rate limits, and re-logs in ONCE if the token is rejected
        (e-session-0007) -- same live failure mode as _request's
        relogin, just on the Interactive (orders/positions) session."""
        if not self.interactive_token:
            raise XTSOrderError(f"Interactive API session not available: {self._interactive_error}")
        params = dict(params or {})
        if self.client_id:
            params.setdefault("clientID", self.client_id)
        relogin_used = False
        for attempt in range(5):
            response = self._session.request(
                method, self.base_url + path,
                headers={"Content-Type": "application/json", "Authorization": self.interactive_token},
                params=params or None, json=body, timeout=15,
            )
            if response.status_code == 429:
                _time_module.sleep(2 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                raise XTSOrderError(f"{method} {path}: HTTP {response.status_code} {response.text[:200]}")
            if response.status_code >= 400 or data.get("type") == "error":
                if not relogin_used and data.get("code") == "e-session-0007":
                    relogin_used = True
                    self._relogin_interactive()
                    continue
                raise XTSOrderError(f"{method} {path}: {data.get('code')} {data.get('description')}")
            return data
        raise XTSOrderError(f"{method} {path}: still rate limited (429) after retries")

    def place_order(self, exchange_instrument_id: int, side: str, quantity: int,
                     limit_price: float | None, tag: str) -> int:
        """Place an NRML NSEFO order. limit_price=None sends a Market order. Returns AppOrderID."""
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if not self.client_id:
            raise XTSOrderError("JAINAM_CLIENT_ID is not set -- refusing to place an order")
        body = {
            "exchangeSegment": "NSEFO",
            "exchangeInstrumentID": exchange_instrument_id,
            "productType": "NRML",
            "orderType": "Market" if limit_price is None else "Limit",
            "orderSide": side,
            "timeInForce": "DAY",
            "disclosedQuantity": 0,
            "orderQuantity": quantity,
            "limitPrice": 0 if limit_price is None else limit_price,
            "stopPrice": 0,
            "orderUniqueIdentifier": tag,
            "clientID": self.client_id,
        }
        try:
            data = self._interactive_call("POST", "/interactive/orders", body=body)
        except requests.RequestException:
            # Timeout/connection drop AFTER sending: the order may exist. Look it
            # up by tag before anyone retries, otherwise we could double up.
            _time_module.sleep(2)
            found = [o for o in self.get_order_book() if o.get("OrderUniqueIdentifier") == tag]
            if found:
                return int(found[-1]["AppOrderID"])
            raise
        return int(data["result"]["AppOrderID"])

    def cancel_order(self, app_order_id: int) -> None:
        self._interactive_call("DELETE", "/interactive/orders", params={"appOrderID": app_order_id})

    def get_order_book(self) -> list[dict]:
        try:
            data = self._interactive_call("GET", "/interactive/orders")
        except XTSOrderError as exc:
            if "Data Not Available" in str(exc):
                return []
            raise
        return data.get("result") or []

    def get_order(self, app_order_id: int) -> dict | None:
        for order in self.get_order_book():
            if int(order["AppOrderID"]) == int(app_order_id):
                return order
        return None

    def get_net_quantity(self, exchange_instrument_id: int) -> int:
        """Net position quantity for one NSEFO instrument on this client (+long / -short)."""
        try:
            data = self._interactive_call("GET", "/interactive/portfolio/positions", params={"dayOrNet": "NetWise"})
        except XTSOrderError as exc:
            if "Data Not Available" in str(exc):
                return 0
            raise
        total = 0
        for row in (data.get("result") or {}).get("positionList", []):
            if int(row["ExchangeInstrumentId"]) == int(exchange_instrument_id) and row["ExchangeSegment"] == "NSEFO":
                total += int(float(row["Quantity"]))
        return total


def _login(session: requests.Session, base_url: str, path: str, app_key: str, secret_key: str, source: str) -> str:
    data = _request(
        session, "POST", base_url, path, token=None,
        json_body={"appKey": app_key, "secretKey": secret_key, "source": source},
    )
    try:
        return data["result"]["token"]
    except KeyError as exc:
        raise RuntimeError(f"Login to {path} did not return a token: {data}") from exc


def _is_invalid_token_error(text: str) -> bool:
    return "e-session-0007" in text


def _request(session: requests.Session, method: str, base_url: str, path: str, token: str | None,
             params: dict | None = None, json_body: dict | None = None,
             relogin: Callable[[], str] | None = None) -> dict:
    """relogin, if given, is called ONCE if the token is rejected
    (e-session-0007 'Invalid Token') -- it must log in again and return
    the new token, which is retried immediately. This is the live
    failure mode seen 2026-10-28: the market-data session died mid-run
    (most likely another process logging in with the same API key --
    see README open items), killing run_entry.py outright with no
    retry. Never passed for the initial login call itself (nothing to
    re-login from yet)."""
    for attempt in range(2):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        response = session.request(
            method, base_url.rstrip("/") + path, headers=headers, params=params, json=json_body, timeout=10
        )
        if not response.ok:
            if relogin is not None and attempt == 0 and _is_invalid_token_error(response.text):
                token = relogin()
                continue
            raise RuntimeError(
                f"XTS API HTTP {response.status_code} on {method} {path}: {response.text[:500]}"
            )
        data = response.json()
        if data.get("type") == "error":
            if relogin is not None and attempt == 0 and data.get("code") == "e-session-0007":
                token = relogin()
                continue
            raise RuntimeError(f"XTS API error on {path}: {data.get('description')}")
        return data
