import notify


class _FakeResponse:
    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc


def test_send_skips_and_logs_when_token_missing():
    logged = []
    ok = notify.send("hello", token="", chat_id="123", log=logged.append)
    assert ok is False
    assert "skipping notification" in logged[0]


def test_send_skips_and_logs_when_chat_id_missing():
    logged = []
    ok = notify.send("hello", token="abc", chat_id="", log=logged.append)
    assert ok is False
    assert "skipping notification" in logged[0]


def test_send_posts_correct_url_and_payload_on_success():
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _FakeResponse()

    ok = notify.send("hello world", token="TOKEN123", chat_id="CHAT456", post=fake_post)
    assert ok is True
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert payload == {"chat_id": "CHAT456", "text": "hello world"}
    assert timeout == 10


def test_send_returns_false_and_logs_when_post_raises():
    def fake_post(url, json, timeout):
        raise ConnectionError("network down")

    logged = []
    ok = notify.send("hello", token="t", chat_id="c", post=fake_post, log=logged.append)
    assert ok is False
    assert "Telegram send failed" in logged[0]


def test_send_returns_false_and_logs_when_response_raises_for_status():
    def fake_post(url, json, timeout):
        return _FakeResponse(raise_exc=RuntimeError("400 Bad Request"))

    logged = []
    ok = notify.send("hello", token="t", chat_id="c", post=fake_post, log=logged.append)
    assert ok is False
    assert "Telegram send failed" in logged[0]
