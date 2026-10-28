"""
Keeps a long-running poll loop alive through transient failures.

2026-10-28: one 10s read timeout on a quotes call (12:17) propagated
out of run_entry.py's breakout loop and killed the whole entry day --
the 14:35 fallback never ran. A failed poll must cost one iteration,
not the day. Each failure is logged; Telegram hears about the first
one, then every ALERT_EVERY_SECONDS while it keeps failing, then once
on recovery.
"""

from datetime import datetime

import config
import notify

ALERT_EVERY_SECONDS = 300


class PollGuard:
    def __init__(self, name: str, log=print, send=None, now=lambda: datetime.now(config.IST)):
        self.name = name
        self._log = log
        self._send = send or (lambda msg, log: notify.send(msg, log=log))
        self._now = now
        self.failures = 0
        self._last_alert = None

    def failed(self, exc: Exception) -> None:
        self.failures += 1
        self._log(f"!! {self.name}: poll failed ({self.failures} in a row): {type(exc).__name__}: {exc}")
        now = self._now()
        if self._last_alert is None or (now - self._last_alert).total_seconds() >= ALERT_EVERY_SECONDS:
            self._last_alert = now
            self._send(f"[WARN] {self.name}: poll failing ({self.failures} in a row), still running and "
                       f"retrying. Last error: {type(exc).__name__}: {str(exc)[:300]}", log=self._log)

    def succeeded(self) -> None:
        if self.failures:
            self._log(f"{self.name}: recovered after {self.failures} failed poll(s).")
            self._send(f"[OK] {self.name}: recovered after {self.failures} failed poll(s).", log=self._log)
        self.failures = 0
        self._last_alert = None
