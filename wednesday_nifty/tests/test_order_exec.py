from broker.order_exec import execute_order, limit_price_for


class FakeClient:
    """Scripted broker: each placed order gets the next scripted outcome."""

    def __init__(self, outcomes, ltp=100.0):
        self.outcomes = list(outcomes)  # (status, filled, avg) per placed order
        self.ltp = ltp
        self.placed = []  # (side, qty, price, tag)
        self.cancelled = []
        self.books = {}

    def get_option_ltp(self, _):
        if self.ltp is None:
            raise RuntimeError("no quote")
        return self.ltp

    def place_order(self, _, side, qty, price, tag):
        order_id = 1000 + len(self.placed)
        self.placed.append((side, qty, price, tag))
        status, filled, avg = self.outcomes.pop(0)
        self.books[order_id] = {
            "OrderStatus": status,
            "CumulativeQuantity": filled,
            "OrderAverageTradedPrice": str(avg),
            "CancelRejectReason": "RMS: insufficient margin" if status == "Rejected" else "",
        }
        return order_id

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        if self.books[order_id]["OrderStatus"] == "New":
            self.books[order_id]["OrderStatus"] = "Cancelled"

    def get_order(self, order_id):
        return self.books[order_id]


def run(client, side="BUY", qty=130, market=False):
    return execute_order(
        client, 1, side, qty, "T", allow_market_fallback=market,
        sleep=lambda _: None, attempt_timeout=5, log=lambda *_: None,
    )


def test_limit_price_rounds_to_tick_in_marketable_direction():
    assert limit_price_for("BUY", 100.0, 0.03) == 103.0
    assert limit_price_for("SELL", 100.0, 0.03) == 97.0
    assert limit_price_for("BUY", 3.0, 0.03) == 3.5  # rupee floor on the buffer
    assert limit_price_for("SELL", 0.3, 0.03) == 0.05  # never below one tick


def test_fills_first_attempt():
    c = FakeClient([("Filled", 130, 101.5)])
    r = run(c)
    assert r.complete and r.filled == 130 and r.avg_price == 101.5
    assert len(c.placed) == 1 and c.placed[0][2] == 103.0 and c.cancelled == []


def test_unfilled_order_is_cancelled_then_repriced_for_remainder_only():
    c = FakeClient([("New", 0, 0), ("Filled", 130, 104.0)])
    r = run(c)
    assert r.complete and c.cancelled == [1000]
    assert c.placed[1][1] == 130 and c.placed[1][2] > c.placed[0][2]  # more aggressive
    assert c.placed[0][3] != c.placed[1][3]  # unique tag per attempt


def test_partial_fill_then_remainder():
    c = FakeClient([("Cancelled", 65, 100.0), ("Filled", 65, 102.0)])
    r = run(c)
    assert r.complete and r.filled == 130 and r.avg_price == 101.0
    assert c.placed[1][1] == 65  # only the remainder is re-sent


def test_rejection_stops_immediately():
    c = FakeClient([("Rejected", 0, 0), ("Filled", 130, 100.0)])
    r = run(c)
    assert r.filled == 0 and "margin" in r.rejected_reason and len(c.placed) == 1


def test_entry_style_gives_up_without_market_order():
    c = FakeClient([("New", 0, 0)] * 3)
    r = run(c, market=False)
    assert r.filled == 0 and len(c.placed) == 3 and all(p[2] is not None for p in c.placed)


def test_exit_style_falls_back_to_market():
    c = FakeClient([("New", 0, 0)] * 3 + [("Filled", 130, 99.0)])
    r = run(c, side="SELL", market=True)
    assert r.complete and c.placed[-1][2] is None  # final order is Market


def test_no_quote_goes_straight_to_market_when_allowed():
    c = FakeClient([("Filled", 130, 99.0)], ltp=None)
    r = run(c, side="SELL", market=True)
    assert r.complete and c.placed[0][2] is None


def test_no_quote_and_no_market_allowed_places_nothing():
    c = FakeClient([], ltp=None)
    r = run(c, market=False)
    assert r.filled == 0 and c.placed == []
