"""
scripts/diagnose_gate.py  —  Live diagnostic for the Gate.io C++ connector.

What this shows:
  - That the C++ connector actually connects and authenticates
  - Every ticker update (bid / ask / mid / last) as it arrives from the WS cache
  - Current balance (USDT + ALKIMI) from the atomic double buffer
  - Current open orders from the SPSC queue
  - Latency of each fetch_ticker() call (should be sub-millisecond)
  - Whether the WebSocket is staying alive (ticker timestamp should advance)

Usage:
  export $(grep -v '^#' .env | grep -v '^ *$' | xargs)
  python3 scripts/diagnose_gate.py

  # Run for a specific duration (seconds), default 120
  python3 scripts/diagnose_gate.py --duration 60

  # Print ticker every N seconds, default 3
  python3 scripts/diagnose_gate.py --interval 3
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "exchange" / "cpp" / "build"
sys.path.insert(0, str(BUILD))

# ── load C++ connector ────────────────────────────────────────────────────────
try:
    from alkimi_cpp_connectors import GateConnector
except ImportError as e:
    print(f"[ERROR] Could not import C++ connector: {e}")
    print(f"  Make sure you have built the connector:")
    print(f"    cmake --build {BUILD}")
    sys.exit(1)


def fmt_time(ts: float) -> str:
    """Convert a Unix timestamp (seconds) to a readable local time string."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def fmt_price(p: float) -> str:
    return f"{p:.8f}"


def print_separator(char: str = "─", width: int = 68) -> None:
    print(char * width)


def run(duration: float, interval: float) -> None:
    # ── credentials ──────────────────────────────────────────────────────────
    api_key    = os.environ.get("GATE_API_KEY",    "")
    api_secret = os.environ.get("GATE_API_SECRET", "")

    if not api_key or not api_secret:
        print("[ERROR] GATE_API_KEY and GATE_API_SECRET must be set in the environment.")
        print("  export $(grep -v '^#' .env | grep -v '^ *$' | xargs)")
        sys.exit(1)

    print()
    print_separator("═")
    print("  Gate.io C++ Connector  —  Live Diagnostic")
    print(f"  symbol: ALKIMI/USDT  |  duration: {duration:.0f}s  |  interval: {interval:.1f}s")
    print_separator("═")

    # ── connect ───────────────────────────────────────────────────────────────
    print("\n  Connecting C++ connector to Gate.io …", end="", flush=True)
    t0 = time.perf_counter()
    try:
        conn = GateConnector("ALKIMI/USDT", api_key, api_secret)
        conn.connect()
    except Exception as e:
        print(f"\n  [ERROR] connect() failed: {e}")
        sys.exit(1)

    connect_ms = (time.perf_counter() - t0) * 1000
    print(f"  connected in {connect_ms:.0f} ms\n")

    # ── poll loop ─────────────────────────────────────────────────────────────
    deadline    = time.time() + duration
    tick        = 0
    prev_ts     = 0.0
    stale_count = 0

    try:
        while time.time() < deadline:
            tick += 1
            now_wall = time.time()

            # ── fetch_ticker ──────────────────────────────────────────────────
            t_start = time.perf_counter()
            try:
                ticker = conn.fetch_ticker()
                fetch_us = (time.perf_counter() - t_start) * 1_000_000  # microseconds
            except Exception as e:
                print(f"  [TICK {tick:04d}] fetch_ticker ERROR: {e}")
                time.sleep(interval)
                continue

            # Detect whether the WebSocket is actually delivering new data
            is_stale = (ticker.timestamp == prev_ts)
            if is_stale:
                stale_count += 1
                stale_label = f"  ⚠ STALE ({stale_count} consecutive)"
            else:
                stale_count = 0
                stale_label = ""

            prev_ts = ticker.timestamp

            # ── fetch_balance ─────────────────────────────────────────────────
            try:
                balance = conn.fetch_balance()
                bal_str = (f"USDT {balance.usd:.4f}  |  "
                           f"ALKIMI {balance.token:.2f}")
            except Exception as e:
                bal_str = f"ERROR: {e}"

            # ── fetch_open_orders ─────────────────────────────────────────────
            try:
                orders = conn.fetch_open_orders()
                if orders:
                    order_lines = []
                    for o in orders:
                        order_lines.append(
                            f"    id={o.id[:12]}…  {o.side:4s}  "
                            f"price={fmt_price(o.price)}  "
                            f"amount={o.amount:.2f}  "
                            f"status={o.status}"
                        )
                    orders_str = "\n" + "\n".join(order_lines)
                else:
                    orders_str = "none"
            except Exception as e:
                orders_str = f"ERROR: {e}"

            # ── print ─────────────────────────────────────────────────────────
            print_separator()
            print(f"  Tick #{tick:04d}  |  wall {fmt_time(now_wall)}  |  "
                  f"fetch_ticker took {fetch_us:.1f} µs{stale_label}")
            print_separator()
            print(f"  Ticker (from WS cache via seqlock)")
            print(f"    bid   = {fmt_price(ticker.bid)}")
            print(f"    ask   = {fmt_price(ticker.ask)}")
            print(f"    last  = {fmt_price(ticker.last)}")
            print(f"    mid   = {fmt_price(ticker.mid)}")
            print(f"    spread= {fmt_price(ticker.ask - ticker.bid)}"
                  f"  ({((ticker.ask - ticker.bid) / ticker.mid * 100):.4f}%)"
                  if ticker.mid > 0 else "")
            print(f"    WS ts = {fmt_time(ticker.timestamp)}"
                  f"  (age: {(now_wall - ticker.timestamp)*1000:.1f} ms)")
            print()
            print(f"  Balance (from atomic double buffer)")
            print(f"    {bal_str}")
            print()
            print(f"  Open orders (from SPSC queue → consumer map)")
            print(f"    {orders_str}")
            print()

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n  Interrupted.")
    finally:
        print("\n  Disconnecting …")
        try:
            conn.disconnect()
        except Exception:
            pass
        print("  Done.")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live diagnostic for the Gate.io C++ connector."
    )
    parser.add_argument("--duration", type=float, default=120,
                        help="How long to run in seconds (default: 120)")
    parser.add_argument("--interval", type=float, default=3,
                        help="Seconds between each print (default: 3)")
    args = parser.parse_args()
    run(args.duration, args.interval)


if __name__ == "__main__":
    main()
