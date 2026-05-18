"""
scripts/ab_paper_trade.py — A/B Paper Trading CLI

Runs ABComparator on one or more exchanges concurrently, comparing CCXT vs C++
connectors in parallel on the same order grid.  Supply --mock for a fully
offline simulation (no credentials or C++ .so required).

Usage examples
──────────────
  # Mock: all 4 exchanges concurrently (10-second smoke test):
  python3 scripts/ab_paper_trade.py --mock --exchanges all --duration 10 --tick-interval 2

  # Mock: specific exchanges only:
  python3 scripts/ab_paper_trade.py --mock --exchanges kucoin,gate --duration 600

  # Real: all 4 exchanges (24h test, read-only mainnet keys):
  export KUCOIN_API_KEY=... KUCOIN_API_SECRET=... KUCOIN_PASSPHRASE=...
  export GATE_API_KEY=...   GATE_API_SECRET=...
  export MEXC_API_KEY=...   MEXC_API_SECRET=...
  export KRAKEN_API_KEY=... KRAKEN_API_SECRET=...
  python3 scripts/ab_paper_trade.py --exchanges all --duration 86400 \\
      --output my-docs/ab_results_24h.md

  # Real: just the two high-weight exchanges:
  python3 scripts/ab_paper_trade.py --exchanges kucoin,gate --duration 86400
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path so this runs as a standalone script
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.schema import DepthConfig, SpreadConfig, VolatilityConfig
from core.ab_comparator import ABComparator
from exchange.base import Balance, BaseConnector, Candle, Fill, Order, Ticker
from utils.logging import get_logger

log = get_logger("ab_paper_trade")

# ---------------------------------------------------------------------------
# Supported exchanges
# ---------------------------------------------------------------------------

ALL_EXCHANGES = ["kucoin", "gate", "mexc", "kraken"]

_CRED_KEYS: dict[str, list[str]] = {
    "kucoin": ["KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_PASSPHRASE"],
    "gate":   ["GATE_API_KEY",   "GATE_API_SECRET"],
    "mexc":   ["MEXC_API_KEY",   "MEXC_API_SECRET"],
    "kraken": ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"],
}

_QUOTE_CURRENCY: dict[str, str] = {
    "kucoin": "USDT",
    "gate":   "USDT",
    "mexc":   "USDT",
    "kraken": "USD",
}

# Slightly different starting prices per exchange to simulate cross-exchange
# price divergence realistically in mock mode.
_MOCK_INITIAL_PRICE: dict[str, float] = {
    "kucoin": 0.10500,
    "gate":   0.10502,
    "mexc":   0.10498,
    "kraken": 0.10501,
}


def _get_creds(exchange: str) -> dict | None:
    """Read API credentials from env vars. Returns None if any are missing."""
    keys = _CRED_KEYS.get(exchange, [])
    if not keys:
        return None
    env_vals = {k: os.environ.get(k, "") for k in keys}
    if not all(env_vals.values()):
        return None
    prefix = exchange.upper()
    creds: dict = {
        "api_key":    env_vals.get(f"{prefix}_API_KEY", ""),
        "api_secret": env_vals.get(f"{prefix}_API_SECRET", ""),
    }
    pp_key = f"{prefix}_PASSPHRASE"
    if pp_key in env_vals:
        creds["passphrase"] = env_vals[pp_key]
    return creds


# ---------------------------------------------------------------------------
# Mock price engine  (one per exchange — simulates cross-exchange divergence)
# ---------------------------------------------------------------------------

class _MockPriceEngine:
    """
    Geometric Brownian Motion price simulator.
    Each exchange gets its own instance so prices drift independently,
    reflecting realistic cross-exchange price divergence.
    """

    def __init__(
        self,
        seed: int | None = None,
        initial_price: float = 0.1050,
    ) -> None:
        self._rng = random.Random(seed)
        self._mid = initial_price

    def advance(self, dt_s: float = 30.0, vol_ann_pct: float = 80.0) -> float:
        sigma_tick = (vol_ann_pct / 100.0) * math.sqrt(dt_s / (252.0 * 86400.0))
        self._mid *= math.exp(self._rng.gauss(0.0, sigma_tick))
        return self._mid

    @property
    def mid(self) -> float:
        return self._mid

    def make_ticker(self, spread_pct: float = 0.001) -> Ticker:
        m = self._mid
        return Ticker(
            bid=m * (1.0 - spread_pct),
            ask=m * (1.0 + spread_pct),
            mid=m,
            last=m,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# Mock connectors
# ---------------------------------------------------------------------------

class MockCCXTConnector(BaseConnector):
    """
    Simulates a CCXT REST connector:
      Ticker latency : Gaussian ~800 ms ± 190 ms (min 100 ms)
      Failure rate   : ~5 %  (REST timeouts / rate-limits)
      Disconnect prob: ~1.8 % per tick  (~1 disconnect every 55 ticks)
    """

    _TICKER_MEAN_S   = 0.800
    _TICKER_STDEV_S  = 0.190
    _TICKER_MIN_S    = 0.100
    _FAILURE_RATE    = 0.050
    _DISCONNECT_PROB = 0.018

    def __init__(
        self, exchange: str, price_engine: _MockPriceEngine, rng: random.Random
    ) -> None:
        super().__init__(exchange_name=exchange, symbol="ALKIMI/USDT")
        self._price = price_engine
        self._rng   = rng

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def fetch_ticker(self) -> Ticker:
        if self._rng.random() < self._DISCONNECT_PROB:
            raise ConnectionError(f"MockCCXT[{self.exchange_name}]: simulated disconnect")
        latency = max(self._TICKER_MIN_S,
                      self._rng.gauss(self._TICKER_MEAN_S, self._TICKER_STDEV_S))
        await asyncio.sleep(latency)
        if self._rng.random() < self._FAILURE_RATE:
            raise TimeoutError(f"MockCCXT[{self.exchange_name}]: simulated timeout")
        return self._price.make_ticker()

    async def fetch_candles(self, timeframe: str = "1m", limit: int = 15) -> list[Candle]:
        return []

    async def fetch_balance(self) -> Balance:
        return Balance(usd=10_000.0, token=100_000.0)

    async def create_limit_order(self, side: str, price: float, amount: float) -> Order:
        await asyncio.sleep(max(0.10, self._rng.gauss(0.900, 0.220)))
        return Order(
            id=f"mock-ccxt-{self.exchange_name}-{int(time.time() * 1000)}",
            exchange=self.exchange_name, symbol="ALKIMI/USDT",
            side=side, price=price, amount=amount,
            amount_usd=price * amount, status="open", timestamp=time.time(),
        )

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.sleep(max(0.05, self._rng.gauss(0.700, 0.150)))

    async def cancel_all_orders(self) -> None:
        pass

    async def fetch_open_orders(self) -> list[Order]:
        return []

    async def fetch_fills(self, since_ts: float | None = None, limit: int = 100) -> list[Fill]:
        return []


class MockCppConnector(BaseConnector):
    """
    Simulates a C++ WebSocket connector:
      Ticker latency : Gaussian ~1.5 ms ± 0.4 ms (reads in-memory WS cache)
      Failure rate   : ~0.5 %  (rare mutex errors)
      Disconnect prob: ~0.3 % per tick  (~10× more stable than CCXT)
    """

    _TICKER_MEAN_S   = 0.0015
    _TICKER_STDEV_S  = 0.0004
    _TICKER_MIN_S    = 0.0004
    _FAILURE_RATE    = 0.005
    _DISCONNECT_PROB = 0.003

    def __init__(
        self, exchange: str, price_engine: _MockPriceEngine, rng: random.Random
    ) -> None:
        super().__init__(exchange_name=exchange, symbol="ALKIMI/USDT")
        self._price = price_engine
        self._rng   = rng

    async def connect(self) -> None:
        await asyncio.sleep(0.02)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def fetch_ticker(self) -> Ticker:
        if self._rng.random() < self._DISCONNECT_PROB:
            raise ConnectionError(f"MockCpp[{self.exchange_name}]: simulated WS reconnect")
        latency = max(self._TICKER_MIN_S,
                      self._rng.gauss(self._TICKER_MEAN_S, self._TICKER_STDEV_S))
        await asyncio.sleep(latency)
        if self._rng.random() < self._FAILURE_RATE:
            raise RuntimeError(f"MockCpp[{self.exchange_name}]: simulated mutex timeout")
        return self._price.make_ticker()

    async def fetch_candles(self, timeframe: str = "1m", limit: int = 15) -> list[Candle]:
        return []

    async def fetch_balance(self) -> Balance:
        return Balance(usd=10_000.0, token=100_000.0)

    async def create_limit_order(self, side: str, price: float, amount: float) -> Order:
        await asyncio.sleep(max(0.010, self._rng.gauss(0.065, 0.016)))
        return Order(
            id=f"mock-cpp-{self.exchange_name}-{int(time.time() * 1000)}",
            exchange=self.exchange_name, symbol="ALKIMI/USDT",
            side=side, price=price, amount=amount,
            amount_usd=price * amount, status="open", timestamp=time.time(),
        )

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.sleep(max(0.005, self._rng.gauss(0.020, 0.005)))

    async def cancel_all_orders(self) -> None:
        pass

    async def fetch_open_orders(self) -> list[Order]:
        return []

    async def fetch_fills(self, since_ts: float | None = None, limit: int = 100) -> list[Fill]:
        return []


# ---------------------------------------------------------------------------
# Price advance task  (one per exchange)
# ---------------------------------------------------------------------------

async def _price_advance_loop(
    engine: _MockPriceEngine,
    tick_s: float,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(tick_s)
        engine.advance(dt_s=tick_s)


# ---------------------------------------------------------------------------
# Per-exchange runner
# ---------------------------------------------------------------------------

async def _run_exchange(
    exchange: str,
    ccxt_conn: BaseConnector,
    cpp_conn: BaseConnector,
    symbol: str,
    spread_cfg: SpreadConfig,
    depth_cfg: DepthConfig,
    vol_cfg: VolatilityConfig,
    duration_s: float,
    tick_interval_s: float,
    report_interval_s: float,
    price_engine: _MockPriceEngine | None,   # None in real mode
) -> ABComparator:
    """Connect, run, and return the finished ABComparator for one exchange."""
    comparator = ABComparator(
        ccxt_conn, cpp_conn, exchange, symbol,
        spread_cfg, depth_cfg, vol_cfg,
        tick_interval_s=tick_interval_s,
    )

    # In mock mode, run the price advance loop as a background task so it
    # can be cancelled cleanly when the comparator finishes — not gathered.
    advance_task: asyncio.Task | None = None
    if price_engine is not None:
        stop_event = asyncio.Event()
        advance_task = asyncio.create_task(
            _price_advance_loop(price_engine, tick_interval_s, stop_event)
        )

    try:
        await comparator.start(duration_s=duration_s, report_interval_s=report_interval_s)
    finally:
        if advance_task is not None:
            advance_task.cancel()
            try:
                await advance_task
            except asyncio.CancelledError:
                pass

    return comparator


# ---------------------------------------------------------------------------
# Multi-exchange summary
# ---------------------------------------------------------------------------

def _format_summary(
    comparators: dict[str, ABComparator],
    elapsed_s: float,
) -> str:
    from datetime import timedelta

    def _human(s: float) -> str:
        h, r = divmod(int(s), 3600)
        m, sec = divmod(r, 60)
        return f"{h}h {m:02d}m {sec:02d}s"

    width = 90
    lines: list[str] = [
        f"\n  {'═' * width}",
        f"  {'MULTI-EXCHANGE A/B SUMMARY':^{width}}",
        f"  {'duration: ' + _human(elapsed_s):^{width}}",
        f"  {'═' * width}",
    ]

    # Header row
    col = 16
    hdr = f"  {'Exchange':<10}  {'Uptime':>{col}}  {'Reconnects':>{col}}  {'Ticker OK':>{col}}  {'C++ mean lat':>{col}}  {'Fill rate':>{col}}  {'Verdict':>{col}}"
    lines += [hdr, "  " + "─" * (width - 2)]

    all_pass = True
    for exch, comp in comparators.items():
        v = comp.verdict()
        passed = all(v.values())
        all_pass = all_pass and passed

        c = comp.ccxt_metrics
        p = comp.cpp_metrics

        uptime_str   = f"{p.uptime_pct:.1f}% / {c.uptime_pct:.1f}%"
        recon_str    = f"{p.reconnection_count} / {c.reconnection_count}"
        success_str  = f"{p.ticker_success_rate_pct:.1f}% / {c.ticker_success_rate_pct:.1f}%"
        lat_str      = f"{p.ticker_latency_mean_ms:.1f}ms"
        fill_str     = f"{p.fill_rate_pct:.3f}% / {c.fill_rate_pct:.3f}%"
        verdict_str  = "PASS ✓" if passed else "FAIL ✗"

        lines.append(
            f"  {exch:<10}  {uptime_str:>{col}}  {recon_str:>{col}}  "
            f"{success_str:>{col}}  {lat_str:>{col}}  {fill_str:>{col}}  {verdict_str:>{col}}"
        )

    lines += [
        "  " + "─" * (width - 2),
        f"  {'(C++ / CCXT for each column — lower reconnects and higher values for C++ = better)':^{width}}",
        "",
        f"  Overall: {'ALL EXCHANGES PASS ✓' if all_pass else 'ONE OR MORE EXCHANGES FAILED ✗'}",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------

def _append_to_file(path: str, content: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)
    print(f"\nReport appended to: {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    spread_cfg = SpreadConfig()
    depth_cfg  = DepthConfig()
    vol_cfg    = VolatilityConfig()
    symbol     = args.symbol
    exchanges  = args.exchanges  # already validated list

    started_at = time.time()

    if args.mock:
        # ── Mock mode ────────────────────────────────────────────────────────
        print(f"\n  Mode:            SIMULATED (--mock)")
        print(f"  Exchanges:       {', '.join(exchanges)}")
        print(f"  Symbol:          {symbol}")
        print(f"  Duration:        {args.duration}s")
        print(f"  Tick interval:   {args.tick_interval}s")
        print(f"  Report interval: {args.report_interval}s")
        print(f"  Random seed:     {args.seed}")
        print()

        root_rng = random.Random(args.seed)
        results: dict[str, ABComparator] = {}

        async def _run_one_mock(exch: str) -> None:
            seed_offset = root_rng.randint(0, 2**32)
            eng         = _MockPriceEngine(
                seed=None if args.seed is None else args.seed + seed_offset,
                initial_price=_MOCK_INITIAL_PRICE.get(exch, 0.1050),
            )
            ccxt_rng  = random.Random(root_rng.randint(0, 2**32))
            cpp_rng   = random.Random(root_rng.randint(0, 2**32))
            ccxt_conn = MockCCXTConnector(exch, eng, ccxt_rng)
            cpp_conn  = MockCppConnector( exch, eng, cpp_rng)
            comp = await _run_exchange(
                exch, ccxt_conn, cpp_conn, symbol,
                spread_cfg, depth_cfg, vol_cfg,
                args.duration, args.tick_interval, args.report_interval,
                eng,
            )
            results[exch] = comp

        await asyncio.gather(*[_run_one_mock(exch) for exch in exchanges])

    else:
        # ── Real mode ────────────────────────────────────────────────────────
        # Validate credentials for all requested exchanges upfront
        creds_map: dict[str, dict] = {}
        missing: list[str] = []
        for exch in exchanges:
            creds = _get_creds(exch)
            if creds is None:
                missing.append(exch)
            else:
                creds_map[exch] = creds

        if missing:
            print(f"\nError: missing credentials for: {', '.join(missing)}")
            for exch in missing:
                print(f"  {exch}: export {' '.join(_CRED_KEYS[exch])}")
            sys.exit(1)

        print(f"\n  Mode:            REAL (mainnet, read-only)")
        print(f"  Exchanges:       {', '.join(exchanges)}")
        print(f"  Symbol:          {symbol}")
        print(f"  Duration:        {args.duration}s  ({args.duration / 3600:.1f}h)")
        print(f"  Tick interval:   {args.tick_interval}s")
        print(f"  Report interval: {args.report_interval}s")
        print()

        results: dict[str, ABComparator] = {}

        async def _run_one_real(exch: str) -> None:
            from exchange.factory import create_connector
            creds = creds_map[exch]
            try:
                ccxt_conn = create_connector(
                    exchange_name=exch, symbol=symbol,
                    credentials=creds,
                    quote_currency=_QUOTE_CURRENCY.get(exch, "USDT"),
                    use_cpp=False,
                )
                cpp_conn = create_connector(
                    exchange_name=exch, symbol=symbol,
                    credentials=creds,
                    quote_currency=_QUOTE_CURRENCY.get(exch, "USDT"),
                    use_cpp=True,
                )
                comp = await _run_exchange(
                    exch, ccxt_conn, cpp_conn, symbol,
                    spread_cfg, depth_cfg, vol_cfg,
                    args.duration, args.tick_interval, args.report_interval,
                    None,
                )
                results[exch] = comp
            except Exception as e:
                print(f"\n  [{exch}] SKIPPED — connection failed: {e}")
                log.error("exchange_skipped", exchange=exch, error=str(e))

        await asyncio.gather(*[_run_one_real(exch) for exch in exchanges])

    elapsed = time.time() - started_at

    # ── Per-exchange full reports ──────────────────────────────────────────
    full_output: list[str] = []
    for exch in exchanges:
        report = results[exch].generate_report()
        print(report)
        full_output.append(report)

    # ── Multi-exchange summary ─────────────────────────────────────────────
    if len(exchanges) > 1:
        summary = _format_summary(results, elapsed)
        print(summary)
        full_output.append(summary)

    # ── Optional file output ───────────────────────────────────────────────
    if args.output:
        _append_to_file(args.output, "\n".join(full_output))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_exchanges(raw: str) -> list[str]:
    """Parse --exchanges value: 'all' expands to all 4, or comma-separated list."""
    if raw.strip().lower() == "all":
        return list(ALL_EXCHANGES)
    result = [e.strip().lower() for e in raw.split(",") if e.strip()]
    unknown = [e for e in result if e not in ALL_EXCHANGES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"Unknown exchange(s): {unknown}. Must be one of {ALL_EXCHANGES} or 'all'."
        )
    if not result:
        raise argparse.ArgumentTypeError("--exchanges cannot be empty.")
    return result


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="A/B Paper Trading: CCXT vs C++ connector side-by-side comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--mock", action="store_true",
        help="Use simulated connectors (no credentials or C++ .so required)",
    )
    p.add_argument(
        "--exchanges", default="kucoin", type=_parse_exchanges,
        metavar="EXCHANGE[,...]|all",
        help=(
            "Comma-separated exchanges to test, or 'all' for all four. "
            "Choices: kucoin, gate, mexc, kraken (default: kucoin)"
        ),
    )
    p.add_argument(
        "--symbol", default="ALKIMI/USDT",
        help="Trading symbol (default: ALKIMI/USDT)",
    )
    p.add_argument(
        "--duration", type=float, default=86400.0,
        help="Run duration in seconds (default: 86400 = 24 h)",
    )
    p.add_argument(
        "--tick-interval", type=float, default=30.0, dest="tick_interval",
        help="Strategy tick interval in seconds (default: 30)",
    )
    p.add_argument(
        "--report-interval", type=float, default=300.0, dest="report_interval",
        help="Interim progress report interval in seconds (default: 300 = 5 min)",
    )
    p.add_argument(
        "--output", default=None,
        help="Append all reports to this file",
    )
    p.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for mock mode (omit for non-deterministic run)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
