"""
Covers the re-login-on-Invalid-Token retry added after the live
failure on 2026-10-28: run_entry.py crashed outright when the
market-data session died mid-run (XTS e-session-0007 "Invalid Token"),
with zero retry. These tests never touch the network -- a FakeSession
scripts a queue of canned responses in place of requests.Session.
"""

import broker.xts_client as xts


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict, ok: bool | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.ok = (200 <= status_code < 400) if ok is None else ok
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeSession:
    """Returns the next response from a scripted queue on every .request()
    call, regardless of method/url/etc -- enough for ordered scenarios."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


INVALID_TOKEN_BODY = {"type": "error", "code": "e-session-0007", "description": "Invalid Token"}
OK_QUOTE_BODY = {"type": "success", "result": {"ok": True}}
LOGIN_OK_BODY = {"type": "success", "result": {"token": "NEW_TOKEN"}}


# =============================================================================
# _request() -- the low-level retry
# =============================================================================

def test_request_without_relogin_raises_immediately_on_invalid_token():
    session = FakeSession([FakeResponse(400, INVALID_TOKEN_BODY, ok=False)])
    try:
        xts._request(session, "GET", "https://x", "/p", "OLD_TOKEN")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "e-session-0007" in str(exc)
    assert len(session.calls) == 1


def test_request_with_relogin_retries_once_and_succeeds():
    session = FakeSession([
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
        FakeResponse(200, OK_QUOTE_BODY),
    ])
    relogin_calls = []

    def relogin():
        relogin_calls.append(1)
        return "NEW_TOKEN"

    data = xts._request(session, "GET", "https://x", "/p", "OLD_TOKEN", relogin=relogin)
    assert data == OK_QUOTE_BODY
    assert len(relogin_calls) == 1
    assert len(session.calls) == 2
    assert session.calls[1]["headers"]["Authorization"] == "NEW_TOKEN"


def test_request_with_relogin_gives_up_after_one_retry_if_still_invalid():
    session = FakeSession([
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
    ])
    relogin_calls = []

    def relogin():
        relogin_calls.append(1)
        return "NEW_TOKEN"

    try:
        xts._request(session, "GET", "https://x", "/p", "OLD_TOKEN", relogin=relogin)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "e-session-0007" in str(exc)
    assert len(relogin_calls) == 1  # never retried a second time
    assert len(session.calls) == 2


def test_request_relogin_not_triggered_by_an_unrelated_error():
    session = FakeSession([FakeResponse(400, {"type": "error", "code": "e-other", "description": "nope"}, ok=False)])
    relogin_calls = []
    try:
        xts._request(session, "GET", "https://x", "/p", "OLD_TOKEN", relogin=lambda: relogin_calls.append(1))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "nope" in str(exc)
    assert relogin_calls == []
    assert len(session.calls) == 1


# =============================================================================
# XTSDataClient._market_get/_market_post -- wired to _relogin_market
# =============================================================================

def _client_with_session(responses: list[FakeResponse]) -> xts.XTSDataClient:
    client = xts.XTSDataClient(base_url="https://x", market_token="OLD_TOKEN", interactive_token=None)
    client._session = FakeSession(responses)
    client._market_api_key = "mkey"
    client._market_api_secret = "msecret"
    client._source = "WEBAPI"
    return client


def test_market_get_relogs_in_and_retries_on_invalid_token():
    client = _client_with_session([
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
        FakeResponse(200, LOGIN_OK_BODY),  # the re-login call itself
        FakeResponse(200, OK_QUOTE_BODY),  # the retried original call
    ])
    data = client._market_get("/some/path", {})
    assert data == OK_QUOTE_BODY
    assert client.market_token == "NEW_TOKEN"


# =============================================================================
# XTSDataClient._interactive_call -- its own separate retry loop
# =============================================================================

def _client_for_interactive(responses: list[FakeResponse]) -> xts.XTSDataClient:
    client = xts.XTSDataClient(base_url="https://x", market_token="MKT", interactive_token="OLD_INTERACTIVE")
    client._session = FakeSession(responses)
    client._interactive_api_key = "ikey"
    client._interactive_api_secret = "isecret"
    client._source = "WEBAPI"
    client.client_id = "C1"
    client.login_id = "C1A21"
    return client


def test_interactive_call_relogs_in_and_retries_on_invalid_token():
    client = _client_for_interactive([
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
        FakeResponse(200, LOGIN_OK_BODY),  # the re-login call itself
        FakeResponse(200, {"type": "success", "result": []}),
    ])
    net = client.get_login_net_quantity(12345)
    assert net == 0
    assert client.interactive_token == "NEW_TOKEN"


def _trade(login_id, side, qty, instrument_id=12345):
    return {"LoginID": login_id, "ExchangeSegment": "NSEFO", "ExchangeInstrumentID": instrument_id,
            "OrderSide": side, "LastTradedQuantity": qty}


def test_login_net_quantity_counts_only_our_logins_trades():
    client = _client_for_interactive([
        FakeResponse(200, {"type": "success", "result": [
            _trade("C1A21", "SELL", 65),
            _trade("C1A02", "SELL", 260),  # another login on the same client -- not ours
            _trade("C1A21", "BUY", 20),
            _trade("C1A21", "SELL", 99, instrument_id=999),  # different instrument
        ]}),
    ])
    assert client.get_login_net_quantity(12345) == -45


def test_login_net_quantity_refuses_without_login_id():
    client = _client_for_interactive([])
    client.login_id = None
    try:
        client.get_login_net_quantity(12345)
        assert False, "expected XTSOrderError"
    except xts.XTSOrderError as exc:
        assert "JAINAM_LOGIN_ID" in str(exc)


def test_interactive_call_only_relogs_in_once():
    client = _client_for_interactive([
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),
        FakeResponse(200, LOGIN_OK_BODY),
        FakeResponse(400, INVALID_TOKEN_BODY, ok=False),  # still invalid after relogin
    ])
    try:
        client.get_login_net_quantity(12345)
        assert False, "expected XTSOrderError"
    except xts.XTSOrderError as exc:
        assert "e-session-0007" in str(exc)


# =============================================================================
# Network blips (timeouts / dropped connections) on idempotent calls
# =============================================================================

def test_market_call_rides_out_a_read_timeout(monkeypatch):
    monkeypatch.setattr(xts._time_module, "sleep", lambda s: None)
    session = FakeSession([xts.requests.ReadTimeout("read timed out"), FakeResponse(200, OK_QUOTE_BODY)])
    data = xts._request(session, "POST", "https://x", "/quotes", token="T")
    assert data == OK_QUOTE_BODY
    assert len(session.calls) == 2


def test_market_call_gives_up_after_repeated_network_errors(monkeypatch):
    monkeypatch.setattr(xts._time_module, "sleep", lambda s: None)
    session = FakeSession([xts.requests.ConnectionError("down")] * (len(xts.NETWORK_RETRY_DELAYS_SECONDS) + 1))
    try:
        xts._request(session, "POST", "https://x", "/quotes", token="T")
        assert False, "expected ConnectionError"
    except xts.requests.ConnectionError:
        pass


def test_order_post_is_never_resent_on_network_error(monkeypatch):
    monkeypatch.setattr(xts._time_module, "sleep", lambda s: None)
    client = _client_for_interactive([xts.requests.ReadTimeout("lost response")])
    try:
        client._interactive_call("POST", "/interactive/orders", body={})
        assert False, "expected ReadTimeout"
    except xts.requests.ReadTimeout:
        pass
    assert len(client._session.calls) == 1
