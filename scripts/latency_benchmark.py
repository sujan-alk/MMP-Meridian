#!/usr/bin/env python3
"""
scripts/latency_benchmark.py — CCXT vs C++ connector latency comparison.

Measures and compares latency for two key operations:
  1. fetch_ticker()                         × N calls  (default: 100)
  2. create_limit_order() + cancel_order()  × N pairs  (default: 50, dry-run only)

Two modes:
  --mock   Simulated connectors (no credentials or C++ .so needed).
           Uses realistic latency distributions based on the architecture:
             CCXT REST ticker  : ~800ms (HTTP round-trip)
             C++  WS  ticker   :   ~2ms (in-memory cache read)
             CCXT REST order   : ~1600ms (create + cancel, 2× HTTP)
             C++  WS  order    :  ~115ms (WS round-trip × 2)

  (real)   Uses live exchange connectors. Requires:
             - C++ .so built (cd exchange/cpp/build && cmake .. && make)
             - Exchange credentials in environment variables
             - Never places orders if LIVE_MODE=true (use --force-orders to override)

Usage examples:
  # Simulated benchmark (no setup required):
  python3 scripts/latency_benchmark.py --mock

  # Real benchmark on KuCoin, skip orders:
  python3 scripts/latency_benchmark.py --exchange kucoin --skip-orders

  # Real benchmark on Gate.io, include orders, save results:
  python3 scripts/latency_benchmark.py --exchange gate --output my-docs/phase2-benchmarks.md

  # Adjust call counts:
  python3 scripts/latency_benchmark.py --mock --ticker-n 200 --order-n 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Project root on path so this script works from any directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

N_TICKER_DEFAULT = 100
N_ORDER_DEFAULT  = 50
WARMUP_S         = 3.0    # seconds to wait for C++ WS to stabilise before benchmarking
ORDER_AMOUNT     = 100.0  # token amount for test orders (always far below market)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _pct(data: list[float], p: float) -> float:
    """Linear-interpolation percentile."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), int(k) + 1
    if hi >= len(s):
        return s[-1]
    return s[lo] + (k - lo) * (s[hi] - s[lo])


@dataclass
class BenchmarkResult:
    connector:    str
    operation:    str
    n_calls:      int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def mean(self)   -> float: return statistics.mean(self.latencies_ms)   if self.latencies_ms else 0.0
    @property
    def minimum(self)-> float: return min(self.latencies_ms)               if self.latencies_ms else 0.0
    @property
    def maximum(self)-> float: return max(self.latencies_ms)               if self.latencies_ms else 0.0
    @property
    def median(self) -> float: return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0
    @property
    def p95(self)    -> float: return _pct(self.latencies_ms, 95)
    @property
    def p99(self)    -> float: return _pct(self.latencies_ms, 99)
    @property
    def stdev(self)  -> float:
        return statistics.stdev(self.latencies_ms) if len(self.latencies_ms) > 1 else 0.0


# ---------------------------------------------------------------------------
# Mock connectors (simulated latency — no exchange dependency)
# ---------------------------------------------------------------------------

class _MockTicker:
    def __init__(self, mid: float):
        self.mid = mid
        self.bid = mid * 0.999
        self.ask = mid * 1.001
        self.last = mid
        self.timestamp = time.time()


class _MockOrder:
    def __init__(self, oid: str, side: str, price: float, amount: float):
        self.id         = oid
        self.exchange   = "mock"
        self.symbol     = "ALKIMI/USDT"
        self.side       = side
        self.price      = price
        self.amount     = amount
        self.amount_usd = price * amount
        self.status     = "open"
        self.timestamp  = time.time()


class MockCCXTConnector:
    """Simulates CCXT REST latency. Each call is a full HTTP round-trip."""

    exchange_name = "mock_ccxt"
    symbol        = "ALKIMI/USDT"

    async def connect(self) -> None:
        await asyncio.sleep(0.22)  # load_markets HTTP call

    async def disconnect(self) -> None:
        pass

    async def fetch_ticker(self) -> _MockTicker:
        # REST GET — normally distributed around 800 ms
        await asyncio.sleep(max(0.05, random.gauss(0.800, 0.190)))
        return _MockTicker(0.10500 + random.gauss(0, 0.00012))

    async def create_limit_order(self, side: str, price: float, amount: float) -> _MockOrder:
        # REST POST — normally distributed around 900 ms
        await asyncio.sleep(max(0.10, random.gauss(0.900, 0.220)))
        return _MockOrder(f"ccxt-{int(time.time()*1000)}", side, price, amount)

    async def cancel_order(self, order_id: str) -> None:
        # REST DELETE — normally distributed around 700 ms
        await asyncio.sleep(max(0.05, random.gauss(0.700, 0.175)))


class MockCppConnector:
    """
    Simulates C++ WebSocket connector latency.

    fetch_ticker() reads from an in-memory cache maintained by the background
    WS thread — no network call at all. The only overhead is run_in_executor
    dispatching (~0.5 ms) + the C++ function call (~0.5 ms).

    Order placement does require a WS message round-trip to the exchange.
    """

    exchange_name = "mock_cpp"
    symbol        = "ALKIMI/USDT"

    async def connect(self) -> None:
        await asyncio.sleep(0.55)  # WS handshake + auth + subscribe + first ticker

    async def disconnect(self) -> None:
        pass

    async def fetch_ticker(self) -> _MockTicker:
        # In-memory read + thread-pool dispatch overhead: ~1.5 ms
        await asyncio.sleep(max(0.0004, random.gauss(0.0015, 0.0004)))
        return _MockTicker(0.10500 + random.gauss(0, 0.00006))

    async def create_limit_order(self, side: str, price: float, amount: float) -> _MockOrder:
        # WS send + exchange ACK round-trip: ~65 ms
        await asyncio.sleep(max(0.010, random.gauss(0.065, 0.016)))
        return _MockOrder(f"cpp-{int(time.time()*1000)}", side, price, amount)

    async def cancel_order(self, order_id: str) -> None:
        # WS cancel round-trip: ~50 ms
        await asyncio.sleep(max(0.008, random.gauss(0.050, 0.012)))


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

async def _timed(coro) -> tuple[Any, float]:
    """Await coro and return (result, elapsed_ms)."""
    t0 = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

async def run_ticker_benchmark(
    connector,
    label: str,
    n_calls: int,
    verbose: bool = True,
) -> BenchmarkResult:
    """Run fetch_ticker() n_calls times, record individual latencies."""
    result = BenchmarkResult(connector=label, operation="fetch_ticker", n_calls=n_calls)
    for i in range(n_calls):
        _, elapsed = await _timed(connector.fetch_ticker())
        result.latencies_ms.append(elapsed)
        if verbose and (i + 1) % 20 == 0:
            print(f"  [{label:<10}]  fetch_ticker  {i+1:>3}/{n_calls}  "
                  f"last={elapsed:>8.2f}ms  running_mean={result.mean:>8.2f}ms")
    return result


async def run_order_benchmark(
    connector,
    label: str,
    n_pairs: int,
    market_price: float,
    verbose: bool = True,
) -> BenchmarkResult:
    """
    Run create_limit_order() + cancel_order() n_pairs times.
    Orders are placed at 50% below the market price so they will never fill.
    The round-trip time for the pair is recorded as a single latency sample.
    """
    result = BenchmarkResult(connector=label, operation="order_roundtrip", n_calls=n_pairs)
    far_price = market_price * 0.50  # 50% below market — guaranteed not to fill
    for i in range(n_pairs):
        t0 = time.perf_counter()
        order = await connector.create_limit_order("buy", far_price, ORDER_AMOUNT)
        await connector.cancel_order(order.id)
        elapsed = (time.perf_counter() - t0) * 1000.0
        result.latencies_ms.append(elapsed)
        if verbose and (i + 1) % 10 == 0:
            print(f"  [{label:<10}]  order_cycle   {i+1:>3}/{n_pairs}  "
                  f"last={elapsed:>8.2f}ms  running_mean={result.mean:>8.2f}ms")
    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _fmt(ms: float) -> str:
    """Right-aligned ms value, width 9."""
    if ms >= 1000:
        return f"{ms:>8.1f}ms"
    elif ms >= 10:
        return f"{ms:>8.2f}ms"
    else:
        return f"{ms:>8.3f}ms"


def _speedup(a: float, b: float) -> str:
    """Speedup ratio string (a / b)."""
    if b <= 0:
        return "    N/A  "
    r = a / b
    return f"{r:>7.1f}x "


def _divider(width: int = 80) -> str:
    return "  " + "─" * width


def format_results_table(
    op_label: str,
    ccxt: BenchmarkResult,
    cpp: BenchmarkResult,
) -> str:
    """Return a formatted comparison table for one operation."""
    cols = ["Connector", "Mean", "Min", "p50", "p95", "p99", "Max", "Stdev"]
    hdr_fmt = f"  {'Connector':<12}  {'Mean':>9}  {'Min':>9}  {'p50':>9}  {'p95':>9}  {'p99':>9}  {'Max':>9}  {'Stdev':>9}"
    div = _divider(85)

    def _row(r: BenchmarkResult) -> str:
        return (
            f"  {r.connector:<12}  {_fmt(r.mean):>9}  {_fmt(r.minimum):>9}  "
            f"{_fmt(r.median):>9}  {_fmt(r.p95):>9}  {_fmt(r.p99):>9}  "
            f"{_fmt(r.maximum):>9}  {_fmt(r.stdev):>9}"
        )

    def _speedup_row() -> str:
        return (
            f"  {'Speedup':<12}  "
            f"{_speedup(ccxt.mean,    cpp.mean):>9}  "
            f"{_speedup(ccxt.minimum, cpp.minimum):>9}  "
            f"{_speedup(ccxt.median,  cpp.median):>9}  "
            f"{_speedup(ccxt.p95,     cpp.p95):>9}  "
            f"{_speedup(ccxt.p99,     cpp.p99):>9}  "
            f"{_speedup(ccxt.maximum, cpp.maximum):>9}  "
            f"{'':>9}"
        )

    return "\n".join([
        f"\n  {op_label}",
        div,
        hdr_fmt,
        div,
        _row(ccxt),
        _row(cpp),
        div,
        _speedup_row(),
        div,
    ])


def format_target_checks(
    ticker_cpp: BenchmarkResult,
    order_cpp: BenchmarkResult | None,
) -> str:
    """Return a formatted target-metrics pass/fail section."""
    lines = ["\n  Target metrics check:"]

    checks = [
        (ticker_cpp.mean,   3.0,   "C++ fetch_ticker mean",  "< 3ms  (cache + thread-pool)"),
        (ticker_cpp.p99,    5.0,   "C++ fetch_ticker p99",   "< 5ms  (cache + thread-pool)"),
    ]
    if order_cpp is not None:
        checks += [
            (order_cpp.median, 150.0, "C++ order roundtrip p50", "< 150ms (create+cancel WS)"),
            (order_cpp.p99,    300.0, "C++ order roundtrip p99", "< 300ms (create+cancel WS)"),
        ]

    for actual, target, label, note in checks:
        ok = actual <= target
        mark = "✓" if ok else "✗"
        lines.append(f"    {mark}  {label:<30}  {note:<30}  actual: {_fmt(actual).strip()}")

    return "\n".join(lines)


def format_header(exchange: str, symbol: str, mode: str, ts: str) -> str:
    width = 82
    title    = "ALKIMI MM Bot — Connector Latency Benchmark"
    subtitle = f"exchange: {exchange}  |  symbol: {symbol}  |  mode: {mode}  |  {ts}"
    return (
        f"\n  {'═' * width}\n"
        f"  {title:^{width}}\n"
        f"  {subtitle:^{width}}\n"
        f"  {'═' * width}"
    )


def format_full_report(
    exchange: str,
    symbol: str,
    mode: str,
    ts: str,
    ticker_ccxt: BenchmarkResult,
    ticker_cpp:  BenchmarkResult,
    order_ccxt:  BenchmarkResult | None,
    order_cpp:   BenchmarkResult | None,
) -> str:
    """Assemble the full benchmark report as a single string."""
    parts = [format_header(exchange, symbol, mode, ts)]

    parts.append(format_results_table(
        f"fetch_ticker()  ×  {ticker_ccxt.n_calls} calls",
        ticker_ccxt, ticker_cpp,
    ))

    if order_ccxt is not None and order_cpp is not None:
        parts.append(format_results_table(
            f"create_limit_order() + cancel_order()  ×  {order_ccxt.n_calls} pairs",
            order_ccxt, order_cpp,
        ))

    parts.append(format_target_checks(ticker_cpp, order_cpp))
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Real connector setup
# ---------------------------------------------------------------------------

def _get_creds(exchange: str) -> dict:
    """Read exchange credentials from environment variables."""
    mapping = {
        "kucoin": {
            "api_key":    os.environ.get("KUCOIN_API_KEY", ""),
            "api_secret": os.environ.get("KUCOIN_API_SECRET", ""),
            "passphrase": os.environ.get("KUCOIN_PASSPHRASE", ""),
        },
        "gate": {
            "api_key":    os.environ.get("GATE_API_KEY", ""),
            "api_secret": os.environ.get("GATE_API_SECRET", ""),
        },
        "mexc": {
            "api_key":    os.environ.get("MEXC_API_KEY", ""),
            "api_secret": os.environ.get("MEXC_API_SECRET", ""),
        },
        "kraken": {
            "api_key":    os.environ.get("KRAKEN_API_KEY", ""),
            "api_secret": os.environ.get("KRAKEN_API_SECRET", ""),
        },
    }
    creds = mapping.get(exchange)
    if creds is None:
        raise ValueError(f"Unknown exchange: {exchange!r}")
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing credentials for {exchange}: {missing}. "
            f"Set the corresponding environment variables, or use --mock."
        )
    return creds


def _build_real_connectors(exchange: str, symbol: str, creds: dict):
    """Build one CCXT and one C++ connector for the given exchange."""
    from exchange.factory import create_connector
    ccxt_conn = create_connector(exchange, symbol, creds, use_cpp=False)
    cpp_conn  = create_connector(exchange, symbol, creds, use_cpp=True)
    return ccxt_conn, cpp_conn


# ---------------------------------------------------------------------------
# Main benchmark orchestration
# ---------------------------------------------------------------------------

async def run_benchmark(args: argparse.Namespace) -> str:
    """Run the full benchmark, return the report as a string."""
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "SIMULATED (--mock)" if args.mock else "LIVE"

    # ── Build connectors ────────────────────────────────────────────────────
    if args.mock:
        ccxt_conn = MockCCXTConnector()
        cpp_conn  = MockCppConnector()
        exchange  = "mock"
        symbol    = "ALKIMI/USDT"
    else:
        creds     = _get_creds(args.exchange)
        ccxt_conn, cpp_conn = _build_real_connectors(args.exchange, args.symbol, creds)
        exchange  = args.exchange
        symbol    = args.symbol

    print(format_header(exchange, symbol, mode, ts))
    print(f"\n  Connecting…", flush=True)

    # ── Connect both connectors ─────────────────────────────────────────────
    await asyncio.gather(ccxt_conn.connect(), cpp_conn.connect())
    print(f"  Connected. ", flush=True)

    if not args.mock and WARMUP_S > 0:
        print(f"  Warming up C++ WS ({WARMUP_S:.0f}s)…", flush=True)
        await asyncio.sleep(WARMUP_S)

    # ── Ticker benchmark ────────────────────────────────────────────────────
    print(f"\n  ── fetch_ticker × {args.ticker_n} calls ──")
    print(f"  Running CCXT…", flush=True)
    ticker_ccxt = await run_ticker_benchmark(ccxt_conn, "CCXT", args.ticker_n)

    print(f"  Running C++…", flush=True)
    ticker_cpp  = await run_ticker_benchmark(cpp_conn,  "C++",  args.ticker_n)

    # ── Order benchmark ─────────────────────────────────────────────────────
    order_ccxt: BenchmarkResult | None = None
    order_cpp:  BenchmarkResult | None = None

    run_orders = not args.skip_orders
    if run_orders and not args.mock:
        live_mode = os.environ.get("LIVE_MODE", "").lower() == "true"
        if live_mode and not args.force_orders:
            print("\n  ⚠  LIVE_MODE=true — skipping order benchmark for safety.")
            print("     Pass --force-orders to run order benchmark in live mode.")
            run_orders = False

    if run_orders:
        # Use a recent ticker mid to set the far-from-market price
        ref_ticker = await ccxt_conn.fetch_ticker()
        market_mid = ref_ticker.mid

        print(f"\n  ── create+cancel order × {args.order_n} pairs ──")
        print(f"  Market mid: {market_mid:.6f}  |  Order price: {market_mid*0.5:.6f} (50% below market)")
        print(f"  Running CCXT…", flush=True)
        order_ccxt = await run_order_benchmark(ccxt_conn, "CCXT", args.order_n, market_mid)

        print(f"  Running C++…", flush=True)
        order_cpp  = await run_order_benchmark(cpp_conn,  "C++",  args.order_n, market_mid)

    # ── Disconnect ──────────────────────────────────────────────────────────
    await asyncio.gather(
        ccxt_conn.disconnect(), cpp_conn.disconnect(),
        return_exceptions=True,
    )

    return format_full_report(
        exchange, symbol, mode, ts,
        ticker_ccxt, ticker_cpp,
        order_ccxt,  order_cpp,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Benchmark CCXT vs C++ connector latency.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument(
        "--mock", action="store_true",
        help="Use simulated connectors (no credentials or C++ .so needed)",
    )
    p.add_argument(
        "--exchange", default="kucoin",
        choices=["kucoin", "gate", "mexc", "kraken"],
        help="Exchange to benchmark (default: kucoin)",
    )
    p.add_argument(
        "--symbol", default="ALKIMI/USDT",
        help="Trading symbol (default: ALKIMI/USDT)",
    )
    p.add_argument(
        "--ticker-n", type=int, default=N_TICKER_DEFAULT, metavar="N",
        help=f"Number of fetch_ticker() calls (default: {N_TICKER_DEFAULT})",
    )
    p.add_argument(
        "--order-n", type=int, default=N_ORDER_DEFAULT, metavar="N",
        help=f"Number of create+cancel order pairs (default: {N_ORDER_DEFAULT})",
    )
    p.add_argument(
        "--skip-orders", action="store_true",
        help="Skip the order placement benchmark",
    )
    p.add_argument(
        "--force-orders", action="store_true",
        help="Run order benchmark even when LIVE_MODE=true (use with caution)",
    )
    p.add_argument(
        "--output", metavar="FILE",
        help="Write report to this file in addition to stdout",
    )
    p.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for mock mode (ensures reproducible output)",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    try:
        report = asyncio.run(run_benchmark(args))
    except KeyboardInterrupt:
        print("\n  Benchmark interrupted.")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"\n  Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(report)

    if args.output:
        # Wrap in a markdown code block if writing to a .md file
        if args.output.endswith(".md"):
            content = (
                "## Benchmark Results\n\n"
                "Generated by `scripts/latency_benchmark.py`.\n\n"
                "```\n" + report + "\n```\n"
            )
        else:
            content = report
        with open(args.output, "a") as fh:
            fh.write(content)
        print(f"\n  Results appended to: {args.output}")


if __name__ == "__main__":
    main()
