"""
scripts/gate_ws_raw.py  —  Raw Gate.io WebSocket dump (no C++ connector).

Connects directly to Gate.io's WebSocket using Python's websockets library
and prints every single frame exactly as Gate.io sends it — before any
parsing, before any C++ code touches it.

Use this to:
  - Verify Gate.io is actually sending ticker / order / balance updates
  - See the exact JSON structure the C++ connector has to parse
  - Check WS auth is working (private channels should NOT return an error)
  - Debug if a channel is silently failing or being blocked

Usage:
  export $(grep -v '^#' .env | grep -v '^ *$' | xargs)
  python3 scripts/gate_ws_raw.py

  # Watch only specific channels
  python3 scripts/gate_ws_raw.py --channels ticker
  python3 scripts/gate_ws_raw.py --channels ticker,orders,balances

  # Run for N seconds then exit
  python3 scripts/gate_ws_raw.py --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[ERROR] websockets library not installed.")
    print("  pip install websockets")
    sys.exit(1)

GATE_WS_URL = "wss://api.gateio.ws/ws/v4/"
SYMBOL      = "ALKIMI_USDT"

CHANNEL_COLOURS = {
    "spot.tickers":  "\033[96m",   # cyan
    "spot.orders":   "\033[93m",   # yellow
    "spot.balances": "\033[92m",   # green
    "spot.ping":     "\033[90m",   # grey
    "spot.pong":     "\033[90m",   # grey
}
RESET = "\033[0m"
RED   = "\033[91m"
BOLD  = "\033[1m"


def ws_sign(secret: str, channel: str, ts: int) -> str:
    """Gate.io per-channel WS auth: hex(HMAC-SHA512(secret, message))."""
    message = f"channel={channel}&event=subscribe&time={ts}"
    return hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha512
    ).hexdigest()


def fmt_time() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def pretty_json(raw: str) -> str:
    """Re-indent JSON for readable display. Falls back to raw if not valid JSON."""
    try:
        obj = json.loads(raw)
        return json.dumps(obj, indent=4)
    except Exception:
        return raw


async def run(channels: list[str], duration: float) -> None:
    api_key    = os.environ.get("GATE_API_KEY",    "")
    api_secret = os.environ.get("GATE_API_SECRET", "")

    if not api_key or not api_secret:
        print("[ERROR] GATE_API_KEY and GATE_API_SECRET must be set.")
        print("  export $(grep -v '^#' .env | grep -v '^ *$' | xargs)")
        sys.exit(1)

    print()
    print("═" * 72)
    print("  Gate.io Raw WebSocket Dump")
    print(f"  symbol: {SYMBOL}  |  channels: {', '.join(channels)}")
    print(f"  duration: {duration:.0f}s  |  url: {GATE_WS_URL}")
    print("═" * 72)
    print(f"\n  {fmt_time()}  Connecting …\n")

    deadline = time.time() + duration
    msg_count = 0

    async with websockets.connect(GATE_WS_URL) as ws:
        print(f"  {fmt_time()}  {BOLD}Connected{RESET}\n")

        ts = int(time.time())

        # ── subscribe ─────────────────────────────────────────────────────────
        for ch in channels:
            # Gate.io channel names: spot.tickers, spot.orders, spot.balances
            ch_full = f"spot.{ch}s" if ch == "ticker" else f"spot.{ch}"

            sub: dict = {
                "time":    ts,
                "channel": ch_full,
                "event":   "subscribe",
            }

            # Private channels need per-channel auth.
            if ch in ("orders", "balances"):
                sub["payload"] = [SYMBOL] if ch == "orders" else []
                sub["auth"] = {
                    "method": "api_key",
                    "KEY":    api_key,
                    "SIGN":   ws_sign(api_secret, ch_full, ts),
                }
            else:
                # Public ticker channel.
                sub["payload"] = [SYMBOL]

            await ws.send(json.dumps(sub))
            ch_full = f"spot.{ch}s" if ch == "ticker" else f"spot.{ch}"
            print(f"  {fmt_time()}  → Sent subscribe for {ch_full}")

        print()

        # ── receive loop ──────────────────────────────────────────────────────
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                # Send a ping to keep the connection alive.
                ping = {"time": int(time.time()), "channel": "spot.ping"}
                await ws.send(json.dumps(ping))
                continue

            msg_count += 1
            obj = json.loads(raw) if raw else {}

            channel = obj.get("channel", "unknown")
            event   = obj.get("event",   "")
            colour  = CHANNEL_COLOURS.get(channel, "\033[37m")

            # ── print header line ─────────────────────────────────────────────
            error = obj.get("error")
            status_tag = f"  {RED}ERROR: {error}{RESET}" if error else ""

            print(f"{colour}{'─'*72}{RESET}")
            print(f"{colour}  [{fmt_time()}]  #{msg_count:04d}  "
                  f"channel={channel}  event={event}{status_tag}{RESET}")
            print(f"{colour}{'─'*72}{RESET}")

            # ── print the parsed content in a human-readable way ──────────────
            if channel == "spot.tickers" and event == "update":
                result = obj.get("result", {})
                bid  = result.get("highest_bid", "?")
                ask  = result.get("lowest_ask",  "?")
                last = result.get("last",         "?")
                try:
                    spread_pct = (float(ask) - float(bid)) / float(last) * 100
                    spread_str = f"  spread= {float(ask)-float(bid):.8f}  ({spread_pct:.4f}%)"
                except Exception:
                    spread_str = ""
                print(f"  bid   = {bid}")
                print(f"  ask   = {ask}")
                print(f"  last  = {last}")
                print(f"  vol   = {result.get('base_volume', '?')}")
                print(spread_str)

            elif channel == "spot.orders" and event == "update":
                orders = obj.get("result", [])
                if isinstance(orders, list):
                    for o in orders:
                        print(f"  id={o.get('id','?')}  side={o.get('side','?')}  "
                              f"status={o.get('status','?')}  "
                              f"price={o.get('price','?')}  "
                              f"amount={o.get('amount','?')}")
                else:
                    print(f"  result: {orders}")

            elif channel == "spot.balances" and event == "update":
                result = obj.get("result", {})
                print(f"  currency  = {result.get('currency',  '?')}")
                print(f"  available = {result.get('available', '?')}")
                print(f"  total     = {result.get('total',     '?')}")

            elif event == "subscribe":
                # Subscription acknowledgement — check for errors.
                if error:
                    print(f"  {RED}Subscription FAILED for {channel}: {error}{RESET}")
                else:
                    print(f"  Subscription ACK for {channel} — OK")

            elif channel in ("spot.ping", "spot.pong"):
                print(f"  Heartbeat / pong — connection alive")

            else:
                # Anything else — print the full raw JSON so nothing is hidden.
                print(pretty_json(raw))

            print()

    print(f"\n  {fmt_time()}  Disconnected.  Total messages: {msg_count}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raw Gate.io WebSocket dump — shows every frame before any parsing."
    )
    parser.add_argument(
        "--channels",
        default="ticker,orders,balances",
        help="Comma-separated list of channels to subscribe to. "
             "Options: ticker, orders, balances (default: all three)"
    )
    parser.add_argument(
        "--duration", type=float, default=60,
        help="How long to listen in seconds (default: 60)"
    )
    args = parser.parse_args()

    channel_list = [c.strip() for c in args.channels.split(",") if c.strip()]
    valid = {"ticker", "orders", "balances"}
    unknown = set(channel_list) - valid
    if unknown:
        print(f"[ERROR] Unknown channels: {unknown}.  Valid: {valid}")
        sys.exit(1)

    asyncio.run(run(channel_list, args.duration))


if __name__ == "__main__":
    main()
