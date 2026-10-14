"""
Calls get_ltp() directly against the resolved NIFTY futures contract and
prints the result (or the full error body, now that xts_client.py's
_request() surfaces response.text instead of swallowing it behind
raise_for_status()).

Read-only: this exercises the same REST quotes call market_stream.py's
get_ltp_with_fallback() falls back to, and nothing else -- no orders are
possible from this script.

    python scripts/quote_smoke_test.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from broker.xts_client import NSE_FO_SEGMENT, XTSDataClient


def main() -> int:
    now = datetime.now(config.IST)
    print(f"=== QUOTE SMOKE TEST --- {now.isoformat()} ===\n")

    print("Logging into Jainam/XTS...")
    client = XTSDataClient.from_env()
    print(f"  market_token: {client.market_token[:20]}...")

    print("\nResolving current NIFTY futures contract...")
    future = client.get_nifty_fut_instrument(now.date())
    print(f"  {future['tradingsymbol']} (instrument_token={future['instrument_token']}, expiry={future['expiry']})")

    print(f"\nCalling get_ltp(NSE_FO_SEGMENT, {future['instrument_token']})...")
    try:
        ltp = client.get_ltp(NSE_FO_SEGMENT, future["instrument_token"])
        print(f"\n=== SUCCESS: LTP = {ltp} ===")
        return 0
    except Exception as exc:
        print(f"\n!! QUOTE CALL FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
