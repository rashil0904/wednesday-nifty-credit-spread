# Wednesday 3-Day Breakout Credit Spread

Live NIFTY weekly options strategy, trading through Jainam Broking
(Symphony XTS API). WebSocket market data with REST fallback. Dry run
by default.

## Strategy summary

- **Entry day**: Wednesday, or Thursday if Wednesday is an NSE holiday
  (`config.NSE_HOLIDAYS`). If both are holidays, the week is skipped
  and flagged, never auto-pushed to Friday.
- **Reference levels**: 3D_HIGH/3D_LOW from the 3 most recent completed
  NIFTY futures daily sessions, computed once before 09:15.
- **Entry triggers** (`strategy.breakout.EntryTrigger`), first breach
  wins, evaluated every tick 09:15-14:35:
  - `LOW_BREAKOUT` (futures < 3D_LOW) -> Call Credit Spread
  - `HIGH_BREAKOUT` (futures > 3D_HIGH) -> Put Credit Spread
  - If nothing breaches by 14:35, compare the 14:35 price to the 09:15
    open: `TIME_1435_RED` (below) -> Call Credit Spread,
    `TIME_1435_GREEN` (above) -> Put Credit Spread, exactly equal -> no trade.
- **Strikes**: ATM = spot snapped to the live strike interval; hedge =
  ATM +/- `HEDGE_OFFSET_POINTS` (200), same option type.
- **Expiry**: nearest *listed* weekly expiry on/after the Tuesday
  following entry, resolved live from the instrument master (so an
  NSE holiday shift in the listed expiry is never missed).
- **Exits**, whichever fires first:
  - Profit target: 90% of entry credit captured (every day, every tick).
  - **Stop loss — `HIGH_BREAKOUT` entries only**: SL level = the
    3D_HIGH that was breached, fixed at entry forever. Exits the whole
    spread the instant NIFTY futures trade below it, intraday or as a
    gap-down open on a later day. No stop loss exists for any other
    trigger — do not add one.
  - Time exit: the Tuesday (or holiday-shifted earlier trading day) at
    `config.EXIT_TIME` (15:15), unconditional.
- Order sequencing: entry BUYs the hedge leg first, then SELLs the
  short leg (margin, and never leaves a naked short on a partial
  entry). Exit BUYs back the short leg first, then SELLs the hedge leg.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # already done here -- real Jainam creds are in .env
.venv/bin/python -m pytest -v
```

1. **Run the socket smoke test first**, before trusting the WebSocket
   path for anything else:
   ```
   .venv/bin/python scripts/socket_smoke_test.py
   # if it fails, try the other candidate path:
   .venv/bin/python scripts/socket_smoke_test.py --path /apimarketdata/socket.io
   ```
2. `JAINAM_CLIENT_ID` is already set in `.env` (not required for dry
   run; unconfirmed against a real order/position call yet).
3. `JAINAM_LIVE=false` (the default) runs everything against real
   market data and logs exactly what it would trade, placing nothing.
   Flip to `true` only after reviewing every item below.

## Running

```
.venv/bin/python scripts/run_entry.py          # Wednesday/Thursday mornings
.venv/bin/python scripts/run_monitor_exit.py   # every later trading day while a position is open
```

Both are meant to be started once each relevant morning and left
running through market close (they stream/poll internally), not
re-invoked every few minutes.

### Cron (IST)

```cron
# Entry: Wed/Thu mornings, well before 09:15 -- the script itself
# waits for 09:15 and decides whether today is the resolved entry day.
10 9 * * 3,4 cd /path/to/wednesday_nifty && .venv/bin/python scripts/run_entry.py >> logs/cron.log 2>&1

# Exit monitor: every weekday morning. No-ops immediately if there's
# no open position, or if today is the entry day (run_entry.py already
# covers that day's monitoring).
10 9 * * 1-5 cd /path/to/wednesday_nifty && .venv/bin/python scripts/run_monitor_exit.py >> logs/cron.log 2>&1
```

## Open items — depends on the live API, not guessed

1. **WebSocket host/path/event names** — no code anywhere (including
   the reference repo, which is REST-only) has ever connected to
   Jainam's XTS socket feed. `config.JAINAM_SOCKET_PATH` defaults to
   `/apibinarymarketdata/socket.io` (best guess, by analogy with every
   verified REST market-data call needing that prefix) but
   `scripts/socket_smoke_test.py --path /apimarketdata/socket.io` can
   try the documented alternative. Event names (`1512-json-full`) and
   the subscribe-request shape are unverified until a real tick
   arrives.
2. **Candle timestamp convention** — carried forward as verified from
   the reference repo (IST wall-clock encoded as a pseudo-epoch,
   `:59`-stamped) against this same Jainam account. Worth a spot-check
   against a known value once live.
3. **Futures rollover near expiry** — `config.ROLLOVER_PROXIMITY_DAYS`
   (2) skips the week entirely rather than guess which contract to
   read. Confirm this threshold.
4. **Futures rollover while a HIGH_BREAKOUT stop loss is active** — if
   the near-month future rolls while a position with an active SL is
   open, `exit_executor.py` FLAGS this loudly and skips the SL check
   that cycle rather than silently comparing the new contract's price
   against the old SL level. Needs a decision on how (or whether) to
   remap the SL to the new contract.
5. **Combo-order support** — not used. Entry/exit both place sequential
   single-leg orders, matching the reference.
6. **Margin** — no pre-trade margin check exists anywhere (matching the
   reference's own scope). Not added here either.
7. **`JAINAM_CLIENT_ID`** — set to `SNM3358` in `.env`. Not yet
   confirmed against a real order/position call; required for any order
   placement or net-position check, dry run doesn't need it.
8. **Interactive API credential rejection risk** — the reference
   repo's author had their own Interactive key/secret rejected on this
   same Jainam account. Our `.env` has different Interactive
   credentials — untested until the first real login attempt.
9. **NSEFO option/futures LTP after market hours** — the reference saw
   an empty quote list for NSEFO instruments after-hours (NSECM/VIX
   worked fine same session). Unclear if this is a closed-market
   caching gap or an entitlement issue; needs a during-market-hours
   check.
10. **`compressionValue="D"` for daily OHLC candles** (used for the
    3-day high/low) is an unverified guess by analogy with the
    documented `"60"` (1-minute) value — confirm against a real
    response.

## Project layout

- `config.py` — every tunable constant.
- `strategy/` — pure logic, no I/O: `levels.py` (3-day H/L),
  `breakout.py` (entry triggers), `strikes.py`, `calendar.py`
  (entry-day holiday shift, expiry/exit-date calc), `plan.py` (leg
  assembly), `exit_rules.py` (profit target, time exit),
  `stop_loss.py` (HIGH_BREAKOUT-only SL).
- `broker/` — `xts_client.py` (REST), `order_exec.py` (fill tracking),
  `market_stream.py` (Socket.IO + REST fallback behind one interface).
- `exit_executor.py` — the shared "check every exit condition, close if
  one fires" procedure used by both scripts.
- `position_store.py` — JSON position state + the file locks guarding
  against double entry/exit.
- `scripts/run_entry.py`, `scripts/run_monitor_exit.py`,
  `scripts/socket_smoke_test.py`.
- `tests/` — one file per module above, plus `test_entry_flow.py` and
  `test_exit_executor.py` for cross-module interaction cases.
