"""A minimal fake Kite client covering only what order_execution.py /
levels.py / instruments.py actually call. No network access."""


class FakeKite:
    VARIETY_REGULAR = "regular"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"

    def __init__(self):
        self.access_token = "fake-token"
        self.quotes = {}
        self.ltps = {}
        self.historical = {}
        self.instrument_rows = []
        self.margins_available = 10_000_000
        self.margin_required = 10_000
        self.order_log = []
        self.fill_map = {}  # tradingsymbol -> "COMPLETE" | "REJECTED"
        self._order_seq = 0

    def instruments(self, exchange):
        return self.instrument_rows

    def historical_data(self, instrument_token, from_date, to_date, interval):
        return self.historical.get(instrument_token, [])

    def quote(self, keys):
        return {k: self.quotes[k] for k in keys}

    def ltp(self, keys):
        return {k: {"last_price": self.ltps[k]} for k in keys}

    def order_margins(self, basket):
        return [{"total": self.margin_required / len(basket)} for _ in basket]

    def margins(self):
        return {"equity": {"available": {"live_balance": self.margins_available}}}

    def place_order(self, **kwargs):
        self._order_seq += 1
        order_id = f"ORDER{self._order_seq}"
        symbol = kwargs["tradingsymbol"]
        status = self.fill_map.get(symbol, "COMPLETE")
        price = kwargs.get("price")
        if price is None:
            price = self.ltps.get(f"NFO:{symbol}", 0)
        self.order_log.append({"order_id": order_id, "status": status, "price": price, **kwargs})
        return order_id

    def order_history(self, order_id):
        for order in self.order_log:
            if order["order_id"] == order_id:
                return [{"status": order["status"], "average_price": order["price"]}]
        return [{"status": "REJECTED", "average_price": None}]

    def cancel_order(self, variety, order_id):
        pass
