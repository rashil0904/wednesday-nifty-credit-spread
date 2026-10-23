"""
Telegram notifications for every money-moving event and anything else
worth knowing about without watching the log: entries, exits, order
failures, skipped weeks, and crashes.

Best-effort only -- a Telegram outage must never block or crash the
trading flow. send() catches everything itself and only logs locally on
failure; callers never need to wrap it in their own try/except.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see
.env.example for how to get them). If either is unset, send() logs and
no-ops -- notifications are a nice-to-have, not a trading precondition.
"""

import requests

import config


def send(message: str, *, token: str | None = None, chat_id: str | None = None,
         post=requests.post, log=print) -> bool:
    token = config.TELEGRAM_BOT_TOKEN if token is None else token
    chat_id = config.TELEGRAM_CHAT_ID if chat_id is None else chat_id
    if not token or not chat_id:
        log("[notify] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set in .env -- skipping notification.")
        return False
    try:
        resp = post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 -- must never crash the caller
        log(f"[notify] Telegram send failed: {exc!r}")
        return False
