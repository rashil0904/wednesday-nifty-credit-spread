"""A minimal fake XTSDataClient covering only what jainam/broker.py and
jainam/execution.py actually call. No network access."""


class FakeXTSClient:
    def __init__(self):
        self.futures = {}  # as_of (date) -> dict(tradingsymbol, instrument_token, expiry)
        self.option_expiries = []  # list[date]
        self.options = {}  # (strike, option_type, expiry) -> dict(tradingsymbol, instrument_token, lot_size)
        self.spot_ltp = None
        self.ltps = {}  # (exchange_segment, exchange_instrument_id) -> float
        self.daily_candles = {}  # exchange_instrument_id -> list[Candle]
        self.candles_at = {}  # (exchange_segment, exchange_instrument_id, date, time) -> Candle

    def get_nifty_fut_instrument(self, as_of):
        return self.futures[as_of]

    def list_nifty_option_expiries(self, as_of):
        candidates = sorted(e for e in self.option_expiries if e >= as_of)
        if not candidates:
            raise LookupError(f"No option expiry >= {as_of}")
        return candidates

    def resolve_option_instrument(self, strike, option_type, expiry):
        return self.options[(strike, option_type, expiry)]

    def get_nifty_spot_ltp(self):
        return self.spot_ltp

    def get_ltp(self, exchange_segment, exchange_instrument_id):
        return self.ltps[(exchange_segment, exchange_instrument_id)]

    def get_daily_candles(self, exchange_segment, exchange_instrument_id, from_date, to_date):
        return self.daily_candles.get(exchange_instrument_id, [])

    def get_candle_at(self, exchange_segment, exchange_instrument_id, target_date, target_time):
        return self.candles_at[(exchange_segment, exchange_instrument_id, target_date, target_time)]
