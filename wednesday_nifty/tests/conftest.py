import sys

import pytest

import notify

sys.path.insert(0, "/root/shared_jainam")
import session_cache as _session_cache  # noqa: E402


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


@pytest.fixture(autouse=True)
def _isolate_shared_jainam_session_cache(tmp_path, monkeypatch):
    """Tests must never read or write the REAL shared session cache at
    /root/shared_jainam/.jainam_session.json -- that file is live
    production state shared across 3 separate trading projects on this
    box. Without this, one test's fake relogin can write a token that a
    LATER test (using the same placeholder base_url) silently adopts
    instead of going through its own mocked response queue -- exactly
    the cross-test pollution that broke test_interactive_call_only_relogs_in_once
    on 2026-10-28."""
    monkeypatch.setattr(_session_cache, "SESSION_FILE", str(tmp_path / "session.json"))
    monkeypatch.setattr(_session_cache, "_LOCK_FILE", str(tmp_path / "session.json.lock"))
