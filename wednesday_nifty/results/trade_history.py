"""
Append-only local record of every CLOSED live wednesday_nifty trade.

positions.json (position_store.py) only ever holds the CURRENT position --
it's overwritten on every entry and gitignored as local run state (see its
own docstring). Something has to accumulate the full history once a trade
closes, or there is no PnL to report at all. This file is that something:
one record appended per fully-closed trade, with gross_pnl() computed here
so there's a single source of truth for the P&L formula. Tracked in git,
not gitignored: this is a permanent ledger, not transient run state like
positions.json.

Modeled on nifty-option-btst's results/trade_history.py, adapted for this
strategy's two legs (SHORT_ATM / LONG_HEDGE, a credit spread rather than a
ratio backspread) and its exit_reason field (PROFIT_TARGET / STOP_LOSS /
TIME_EXIT / MANUAL) -- unlike BTST's single unconditional exit path, this
strategy actually has that distinction, so it's kept here rather than
dropped.

trade_history.json lives alongside this file, at results/trade_history.json.
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HISTORY_PATH = Path(__file__).resolve().parent / "trade_history.json"


def gross_pnl(legs: dict[str, dict]) -> float | None:
    short_leg = legs.get("SHORT_ATM")
    hedge_leg = legs.get("LONG_HEDGE")
    if not short_leg or not hedge_leg:
        return None
    if short_leg.get("exit_price") is None or hedge_leg.get("exit_price") is None:
        return None
    # Short leg: sold at entry, bought back at exit -> profit = entry - exit.
    # Hedge leg: bought at entry, sold back at exit -> profit = exit - entry.
    short_pnl = (short_leg["avg_price"] - short_leg["exit_price"]) * short_leg["quantity"]
    hedge_pnl = (hedge_leg["exit_price"] - hedge_leg["avg_price"]) * hedge_leg["quantity"]
    return round(short_pnl + hedge_pnl, 2)


def append_closed_trade(position: dict) -> dict:
    """position is the full positions.json dict once every leg has closed
    (status == "CLOSED", every leg carries exit_price/exit_timestamp --
    see position_store.record_leg_exit). Returns the record it wrote, so
    the caller can reuse it without re-reading the file."""
    legs = {leg["role"]: leg for leg in position["legs"]}
    exit_dates = [leg["exit_timestamp"][:10] for leg in legs.values() if leg.get("exit_timestamp")]

    record = {
        "entry_date": position["entry_date"],
        "trigger": position.get("trigger"),
        "option_type": next((leg.get("option_type") for leg in legs.values()), None),
        "expiry": position.get("expiry"),
        "legs": legs,
        "exit_date": max(exit_dates) if exit_dates else position.get("closed_date"),
        "exit_reason": position.get("exit_reason"),
        "entry_credit": position.get("entry_credit"),
        "gross_pnl": gross_pnl(legs),
        "archived_at": datetime.now(IST).isoformat(),
    }

    HISTORY_PATH.parent.mkdir(exist_ok=True)
    history = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(record)

    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=2) + "\n")
    tmp.replace(HISTORY_PATH)
    return record


def load_trade_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
