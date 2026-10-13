"""
Order execution with fill tracking: place, wait, cancel, reprice.
Adapted from (not imported from) the Nifty-option-BTST reference's
jainam/broker/order_exec.py -- the retry/fill-tracking logic is generic
and reused near-verbatim.

Takes any client exposing get_ltp(segment, id) / place_order /
cancel_order / get_order (XTSDataClient, or a fake in tests). Never
trusts that an order filled -- it reads the order book for the real
cumulative quantity, and always cancels an unfilled order before sending
the next one so it can never double up.
"""

import math
import time
from dataclasses import dataclass, field

TICK = 0.05  # NSE index-option tick size
TERMINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "Expired"}

# Limit price = last price moved this fraction in the aggressive direction
# (up to buy, down to sell). A limit that crosses the book fills at the
# best available price, so this behaves like a protected market order.
DEFAULT_BUFFERS = (0.03, 0.06, 0.10)
MIN_BUFFER_RUPEES = 0.5


@dataclass
class FillResult:
    requested: int
    filled: int = 0
    avg_price: float = 0.0
    rejected_reason: str | None = None
    order_ids: list[int] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.filled >= self.requested


def limit_price_for(side: str, ltp: float, buffer_pct: float) -> float:
    """Aggressive limit price, snapped to the tick in the direction that keeps it marketable."""
    buffer = max(ltp * buffer_pct, MIN_BUFFER_RUPEES)
    if side == "BUY":
        return round(math.ceil(round((ltp + buffer) / TICK, 6)) * TICK, 2)
    return max(TICK, round(math.floor(round((ltp - buffer) / TICK, 6)) * TICK, 2))


def execute_order(
    client,
    instrument_id: int,
    side: str,
    quantity: int,
    tag: str,
    *,
    allow_market_fallback: bool,
    buffers: tuple[float, ...] = DEFAULT_BUFFERS,
    poll_seconds: float = 2.5,
    attempt_timeout: float = 20.0,
    sleep=time.sleep,
    log=print,
) -> FillResult:
    result = FillResult(requested=quantity)
    traded_value = 0.0
    attempts = len(buffers) + (1 if allow_market_fallback else 0)

    for i in range(attempts):
        remaining = quantity - result.filled
        if remaining <= 0:
            break

        use_market = i >= len(buffers)
        price: float | None = None
        if not use_market:
            try:
                price = limit_price_for(side, client.get_option_ltp(instrument_id), buffers[i])
            except Exception as exc:  # noqa: BLE001 -- no quote: go to market if allowed, else stop
                log(f"    no live quote for {instrument_id} ({exc})")
                if not allow_market_fallback:
                    break
                use_market = True

        label = "MARKET" if use_market else f"LIMIT {price}"
        log(f"    attempt {i + 1}: {side} {remaining} @ {label}")
        order_id = client.place_order(instrument_id, side, remaining, None if use_market else price, f"{tag}{i}")
        result.order_ids.append(order_id)

        order = _wait_terminal(client, order_id, poll_seconds, attempt_timeout, sleep)
        if order is None or order["OrderStatus"] not in TERMINAL_STATUSES:
            log(f"    order {order_id} not filled in {attempt_timeout:.0f}s -> cancelling")
            try:
                client.cancel_order(order_id)
            except Exception as exc:  # noqa: BLE001 -- it may have filled/cancelled meanwhile
                log(f"    cancel call failed ({exc}); re-reading order")
            order = _wait_terminal(client, order_id, poll_seconds, 15.0, sleep)
            if order is None or order["OrderStatus"] not in TERMINAL_STATUSES:
                result.rejected_reason = f"order {order_id} state unknown after cancel -- STOPPED, check the order book"
                log(f"    !! {result.rejected_reason}")
                break

        filled = int(order.get("CumulativeQuantity") or 0)
        if filled:
            traded_value += filled * float(order.get("OrderAverageTradedPrice") or 0)
            result.filled += filled
        log(f"    order {order_id}: {order['OrderStatus']}, filled {filled}")

        if order["OrderStatus"] == "Rejected":
            result.rejected_reason = order.get("CancelRejectReason") or "rejected"
            break

    result.avg_price = round(traded_value / result.filled, 2) if result.filled else 0.0
    return result


def _wait_terminal(client, order_id: int, poll_seconds: float, timeout: float, sleep) -> dict | None:
    waited = 0.0
    order = None
    while True:
        order = client.get_order(order_id) or order
        if order is not None and order["OrderStatus"] in TERMINAL_STATUSES:
            return order
        if waited >= timeout:
            return order
        sleep(poll_seconds)
        waited += poll_seconds
