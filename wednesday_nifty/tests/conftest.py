import pytest

import notify


@pytest.fixture(autouse=True)
def _disable_real_telegram_calls(request, monkeypatch):
    """Tests must never hit the real Telegram API, no matter what's in
    .env -- every module calls notify.send(...) (attribute lookup at
    call time), so patching the module-level function here covers every
    caller. test_notify.py tests notify.send itself, so it's exempt --
    those tests already inject their own fake `post`/token and never
    touch the network either."""
    if "test_notify" in request.node.nodeid:
        return
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: False)
