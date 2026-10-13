"""
Connects to Jainam's XTS market-data WebSocket, subscribes NIFTY
futures, prints the first 20 ticks as they arrive, then disconnects.

Run this FIRST, before trusting broker/market_stream.py for anything
else -- the socket host/path/event names are unverified (see
market_stream.py's module docstring).

    python scripts/socket_smoke_test.py
    python scripts/socket_smoke_test.py --path /apimarketdata/socket.io   # try the other candidate

Exit code 0 if 20 ticks were received, 1 otherwise (connect failure,
subscribe failure, or timeout waiting for ticks).
"""

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from broker.market_stream import LTP_MESSAGE_CODE, MarketStream
from broker.xts_client import NSE_FO_SEGMENT, XTSDataClient

TICK_COUNT_TARGET = 20
WAIT_TIMEOUT_SECONDS = 120


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=None, help="Override config.JAINAM_SOCKET_PATH for this run")
    args = parser.parse_args()

    now = datetime.now(config.IST)
    print(f"=== SOCKET SMOKE TEST --- {now.isoformat()} ===\n")

    print("Logging into Jainam/XTS...")
    client = XTSDataClient.from_env()
    print(f"  market_token: {client.market_token[:20]}...")
    print(f"  client_id: {client.client_id or '(not set)'}")

    print("\nResolving current NIFTY futures contract...")
    future = client.get_nifty_fut_instrument(now.date())
    print(f"  {future['tradingsymbol']} (instrument_token={future['instrument_token']}, expiry={future['expiry']})")

    received = []
    done = threading.Event()

    def on_tick(instrument_id, ltp, ts):
        received.append((instrument_id, ltp, ts))
        print(f"  tick {len(received):2d}: instrument={instrument_id} ltp={ltp} ts={ts.isoformat()}")
        if len(received) >= TICK_COUNT_TARGET:
            done.set()

    stream = MarketStream(client, on_tick=on_tick)
    path = args.path or config.JAINAM_SOCKET_PATH
    print(f"\nConnecting to socket at path {path!r}...")
    try:
        stream.connect(path=path)
    except Exception as exc:
        print(f"\n!! CONNECT FAILED: {exc!r}")
        print("Try --path with the other candidate (see market_stream.py's module docstring).")
        return 1
    print("  connected.")

    print(f"\nSubscribing NIFTY futures ({future['instrument_token']}) for LTP...")
    try:
        stream.subscribe([(NSE_FO_SEGMENT, future["instrument_token"])], LTP_MESSAGE_CODE)
    except Exception as exc:
        print(f"\n!! SUBSCRIBE FAILED: {exc!r}")
        stream.disconnect()
        return 1
    print("  subscribed.")

    print(f"\nWaiting for {TICK_COUNT_TARGET} ticks (timeout {WAIT_TIMEOUT_SECONDS}s)...\n")
    got_all = done.wait(timeout=WAIT_TIMEOUT_SECONDS)
    stream.disconnect()

    if not got_all:
        print(f"\n!! TIMED OUT after {len(received)}/{TICK_COUNT_TARGET} ticks.")
        print("If market is closed, that may be why -- otherwise the event name or")
        print("subscribe shape is likely wrong (see market_stream.py's module docstring).")
        return 1

    print(f"\n=== SUCCESS: received {len(received)} ticks. Report this path/shape as confirmed. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
