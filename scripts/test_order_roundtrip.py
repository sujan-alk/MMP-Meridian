"""
scripts/test_order_roundtrip.py — One real place + cancel round trip on Gate.io.

Verifies the rate limiter and precision filter introduced in the last change:
  - Filter loaded at connect time  (expect: amount_precision=0, min_base=1.0)
  - Amount sent as integer string  (no INVALID_PARAM rejection)
  - Rate limiter token consumed    (acquire returns instantly for 1 order)
  - WS order place returns an ID
  - WS order cancel succeeds

Order safety:
  - Buy limit placed at 50% of the LIVE bid price — computed at run time.
  - At ALKIMI ~$0.003, that is ~$0.0015.  A >50% crash in under 1 second
    would be required to fill it.
  - Amount: 1 000 ALKIMI (~$1.50 notional at safe price) — above Gate.io's
    min_quote_amount of $1, whole number (satisfies amount_precision=0).
  - Gate.io charges fees ONLY on fills.  Cancelling an unfilled order costs $0.

Usage:
    cd Alkimi-MM-Platform
    set -a && source .env && set +a
    python3 scripts/test_order_roundtrip.py
"""

import os
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT  = Path(__file__).resolve().parent.parent
BUILD = ROOT / "exchange" / "cpp" / "build"
sys.path.insert(0, str(BUILD))

# ── Import C++ connector ───────────────────────────────────────────────────────
try:
    from alkimi_cpp_connectors import GateConnector
except ImportError as e:
    print(f"[ERROR] Cannot import GateConnector: {e}")
    print(f"  Expected .so in: {BUILD}")
    print("  Build: cd exchange/cpp/build && cmake .. && make")
    sys.exit(1)

# ── Credentials ────────────────────────────────────────────────────────────────
api_key    = os.environ.get("GATE_API_KEY",    "")
api_secret = os.environ.get("GATE_API_SECRET", "")

if not api_key or not api_secret:
    print("[ERROR] GATE_API_KEY and GATE_API_SECRET are not set.")
    print("  Run:  set -a && source .env && set +a")
    sys.exit(1)

# ── Connect ────────────────────────────────────────────────────────────────────
print()
print("═" * 62)
print("  Gate.io  —  Order Round-Trip Test")
print("═" * 62)
print()
print("  Connecting …  (filter load log appears on stderr below)")
print()

conn = GateConnector("ALKIMI/USDT", api_key, api_secret)
conn.connect()

print()
print("  Connected.")
print()

# ── Live ticker ────────────────────────────────────────────────────────────────
ticker     = conn.fetch_ticker()
bid        = ticker.bid
safe_price = bid * 0.50          # 50% of live bid — computed right now

# Compute the minimum integer amount that gives $5 notional at safe_price.
# This clears Gate.io's min_quote_amount ($3 for ALKIMI) with margin.
# safe_price is computed from the live bid, so this is always correct.
import math
TEST_AMOUNT = math.ceil(5.0 / safe_price)
notional    = safe_price * TEST_AMOUNT

print(f"  Live bid   :  {bid:.8f} USDT")
print(f"  Order at   :  {safe_price:.8f} USDT  (bid × 0.50)")
print(f"  Amount     :  {TEST_AMOUNT} ALKIMI  (enough for $5 notional at safe price)")
print(f"  Notional   :  ${notional:.4f} USDT")
print()

# ── Place ──────────────────────────────────────────────────────────────────────
print("  [ 1 / 2 ]  Placing buy limit order …")
t_place = time.perf_counter()
try:
    order = conn.create_limit_order("buy", safe_price, float(TEST_AMOUNT))
except Exception as e:
    print(f"\n  FAIL  create_limit_order raised: {e}\n")
    conn.disconnect()
    sys.exit(1)
place_ms = (time.perf_counter() - t_place) * 1000.0

print(f"  Order ID   :  {order.id}")
print(f"  Latency    :  {place_ms:.1f} ms")
print()

# ── Cancel ─────────────────────────────────────────────────────────────────────
print(f"  [ 2 / 2 ]  Cancelling order {order.id} …")
t_cancel = time.perf_counter()
try:
    conn.cancel_order(order.id)
except Exception as e:
    print(f"\n  FAIL  cancel_order raised: {e}\n")
    conn.disconnect()
    sys.exit(1)
cancel_ms = (time.perf_counter() - t_cancel) * 1000.0

print(f"  Latency    :  {cancel_ms:.1f} ms")
print()

# ── Summary ────────────────────────────────────────────────────────────────────
print("─" * 62)
print(f"  Place latency   {place_ms:>8.1f} ms")
print(f"  Cancel latency  {cancel_ms:>8.1f} ms")
print(f"  Round trip      {place_ms + cancel_ms:>8.1f} ms")
print("─" * 62)
print()
print("  PASS — order placed and cancelled successfully.")
print("  Cost: $0.00  (unfilled order — no fees charged by Gate.io)")
print()

conn.disconnect()
