"""
ABComparator — CCXT vs C++ connector side-by-side paper trading comparison.

Runs both connectors through identical strategy ticks simultaneously and
collects independent per-connector metrics for the Step 11 A/B validation.

Architecture
────────────
  ┌──────────────── ABComparator ─────────────────────────────────────┐
  │                                                                    │
  │  _strategy_loop()  ─ every TICK_INTERVAL_S seconds:               │
  │    1. Fetch ticker from CCXT + C++ in parallel (time both)        │
  │    2. Update VolatilityEngine → compute aggressiveness             │
  │    3. Compute identical order grid for both connectors             │
  │    4. Cancel previous in-memory orders (both connectors)          │
  │    5. Simulate fill-crossing between prev_mid and curr_mid        │
  │    6. "Place" new grid in memory (track as open orders)           │
  │                                                                    │
  │  _report_loop()  ─ every report_interval_s seconds:               │
  │    Print interim comparison table to stdout                        │
  └────────────────────────────────────────────────────────────────────┘

Design notes
────────────
- Orders are tracked in memory only — no real exchange API calls for order
  placement. This keeps the script safe in both mock and real modes.
  (Step 10 already benchmarked order placement latency; Step 11 focuses on
  connection reliability, ticker performance, and fill-rate comparison.)
- Fill simulation uses price-crossing between ticks:
    buy  order fills when ask crosses DOWN through the order price
    sell order fills when bid crosses UP   through the order price
  The ask/bid are estimated as mid ± SPREAD_APPROX.
- In real mode the script only calls fetch_ticker() on both connectors,
  so only read-only API keys are required and no orders touch the exchange.

Metrics collected per connector
────────────────────────────────
  connection : uptime_pct, reconnection_count, total_downtime_s
  ticker     : attempts, successes, success_rate_pct,
               latency mean/p50/p99
  orders     : ticks_attempted, orders_placed, order_errors,
               fill_count, fill_rate_pct
  pnl        : realized_pnl_usd, total_volume_usd
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from config.schema import DepthConfig, SpreadConfig, VolatilityConfig
from quant.aggressiveness import AggressivenessModel
from quant.depth_engine import DepthEngine
from quant.spread_engine import SpreadEngine
from quant.volatility import VolatilityEngine
from utils.logging import get_logger

log = get_logger("ab_comparator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_INTERVAL_S   = 30.0   # strategy tick period (cancel-and-replace cycle)
SPREAD_APPROX     = 0.001  # ~0.1% spread used for fill-crossing estimate


# ---------------------------------------------------------------------------
# In-memory order representation
# ---------------------------------------------------------------------------

@dataclass
class _OpenOrder:
    """Lightweight order tracked in memory (never sent to exchange)."""
    oid:       str
    side:      str    # "buy" | "sell"
    price:     float
    amount:    float  # token amount
    placed_at: float  # Unix seconds


# ---------------------------------------------------------------------------
# ConnectorMetrics
# ---------------------------------------------------------------------------

@dataclass
class ConnectorMetrics:
    """All metrics for one connector during the A/B run."""

    name: str

    # ── timing ──────────────────────────────────────────────────────────────
    started_at:       float = 0.0

    # ── connection health ────────────────────────────────────────────────────
    reconnection_count:  int   = 0
    total_downtime_s:    float = 0.0
    _last_disconnect_at: float = field(default=0.0, repr=False)
    _is_connected:       bool  = field(default=True,  repr=False)

    # ── ticker ───────────────────────────────────────────────────────────────
    ticker_attempts:    int        = 0
    ticker_successes:   int        = 0
    ticker_latencies_ms: list[float] = field(default_factory=list)

    # ── orders (in-memory simulation) ───────────────────────────────────────
    ticks_attempted:  int = 0   # price-loop ticks where we tried to "place" a grid
    orders_placed:    int = 0   # individual limit orders tracked in memory
    order_errors:     int = 0   # ticks where ticker fetch failed → grid skipped
    fill_count:       int = 0
    total_volume_usd: float = 0.0
    realized_pnl_usd: float = 0.0

    # ── raw fill history (for detailed report) ───────────────────────────────
    _fill_prices: list[float] = field(default_factory=list, repr=False)

    # ────────────────────────────────────────────────────────────────────────
    # Connection event recording
    # ────────────────────────────────────────────────────────────────────────

    def record_connect(self) -> None:
        """Call when the connector re-establishes its connection."""
        if not self._is_connected:
            if self._last_disconnect_at > 0:
                self.total_downtime_s += time.time() - self._last_disconnect_at
            self._is_connected = True

    def record_disconnect(self) -> None:
        """Call when the connector loses its connection."""
        if self._is_connected:
            self.reconnection_count += 1
            self._last_disconnect_at = time.time()
            self._is_connected = False

    # ────────────────────────────────────────────────────────────────────────
    # Ticker recording
    # ────────────────────────────────────────────────────────────────────────

    def record_ticker(self, elapsed_ms: float, success: bool) -> None:
        self.ticker_attempts += 1
        if success:
            self.ticker_successes += 1
            self.ticker_latencies_ms.append(elapsed_ms)

    # ────────────────────────────────────────────────────────────────────────
    # Fill recording
    # ────────────────────────────────────────────────────────────────────────

    def record_fill(self, side: str, price: float, amount: float) -> None:
        """Record a simulated fill and update P&L."""
        self.fill_count += 1
        volume = price * amount
        self.total_volume_usd += volume
        self._fill_prices.append(price)
        fee = volume * 0.001  # 0.1% maker fee estimate
        if side == "sell":
            self.realized_pnl_usd += volume - fee
        else:
            self.realized_pnl_usd -= volume + fee

    # ────────────────────────────────────────────────────────────────────────
    # Derived properties
    # ────────────────────────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at if self.started_at > 0 else 0.0

    @property
    def uptime_pct(self) -> float:
        e = self.elapsed_s
        if e <= 0:
            return 100.0
        return max(0.0, (e - self.total_downtime_s) / e * 100.0)

    @property
    def ticker_success_rate_pct(self) -> float:
        return (self.ticker_successes / self.ticker_attempts * 100.0
                if self.ticker_attempts else 100.0)

    @property
    def fill_rate_pct(self) -> float:
        return (self.fill_count / self.orders_placed * 100.0
                if self.orders_placed else 0.0)

    @property
    def ticker_latency_mean_ms(self) -> float:
        return (statistics.mean(self.ticker_latencies_ms)
                if self.ticker_latencies_ms else 0.0)

    @property
    def ticker_latency_p50_ms(self) -> float:
        return (statistics.median(self.ticker_latencies_ms)
                if self.ticker_latencies_ms else 0.0)

    @property
    def ticker_latency_p99_ms(self) -> float:
        data = self.ticker_latencies_ms
        if not data:
            return 0.0
        s = sorted(data)
        k = (len(s) - 1) * 0.99
        lo = int(k)
        hi = lo + 1
        if hi >= len(s):
            return s[-1]
        return s[lo] + (k - lo) * (s[hi] - s[lo])

    @property
    def downtime_human(self) -> str:
        td = timedelta(seconds=int(self.total_downtime_s))
        h, r = divmod(int(td.total_seconds()), 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s"


# ---------------------------------------------------------------------------
# ABComparator
# ---------------------------------------------------------------------------

class ABComparator:
    """
    Orchestrates a side-by-side paper trading comparison between a CCXT and a
    C++ connector.

    Both connectors receive exactly the same GlobalState (same ticker prices,
    same aggressiveness, same order grid).  Differences in fill rate, latency,
    and reliability reveal the real-world advantage of the C++ connector.

    Usage
    ──────
    comparator = ABComparator(ccxt_conn, cpp_conn, exchange, symbol,
                              spread_cfg, depth_cfg, vol_cfg)
    await comparator.start(duration_s=86400, report_interval_s=300)
    print(comparator.generate_report())
    """

    def __init__(
        self,
        ccxt_connector,
        cpp_connector,
        exchange: str,
        symbol: str,
        spread_cfg: SpreadConfig,
        depth_cfg: DepthConfig,
        vol_cfg: VolatilityConfig,
        tick_interval_s: float = TICK_INTERVAL_S,
    ):
        self._ccxt = ccxt_connector
        self._cpp  = cpp_connector
        self._exchange = exchange
        self._symbol   = symbol

        self._spread_engine = SpreadEngine(spread_cfg)
        self._depth_engine  = DepthEngine(depth_cfg)
        self._vol_engine    = VolatilityEngine(vol_cfg)
        self._agg_model     = AggressivenessModel(vol_cfg)
        self._n_levels      = depth_cfg.levels
        self._tick_interval = tick_interval_s

        self.ccxt_metrics = ConnectorMetrics("CCXT")
        self.cpp_metrics  = ConnectorMetrics("C++")

        # In-memory open orders per connector
        self._ccxt_orders: list[_OpenOrder] = []
        self._cpp_orders:  list[_OpenOrder] = []

        self._prev_mid: float = 0.0
        self._curr_mid: float = 0.0
        self._running  = False
        self._started_at: float = 0.0

    # ────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────────

    async def start(
        self,
        duration_s: float = 86400.0,
        report_interval_s: float = 300.0,
    ) -> None:
        """Connect both connectors and run until duration_s elapses."""
        log.info("ab_comparator_connecting",
                 exchange=self._exchange, symbol=self._symbol)
        await asyncio.gather(self._ccxt.connect(), self._cpp.connect())

        self._started_at = time.time()
        self.ccxt_metrics.started_at = self._started_at
        self.cpp_metrics.started_at  = self._started_at
        self._running = True

        log.info("ab_comparator_started",
                 duration_s=duration_s, tick_interval_s=self._tick_interval)

        try:
            await asyncio.gather(
                self._strategy_loop(duration_s),
                self._report_loop(report_interval_s, duration_s),
            )
        finally:
            self._running = False
            await asyncio.gather(
                self._ccxt.disconnect(),
                self._cpp.disconnect(),
                return_exceptions=True,
            )
            log.info("ab_comparator_stopped")

    async def stop(self) -> None:
        """Signal the strategy loop to stop."""
        self._running = False

    # ────────────────────────────────────────────────────────────────────────
    # Main loops
    # ────────────────────────────────────────────────────────────────────────

    async def _strategy_loop(self, duration_s: float) -> None:
        end_time = time.time() + duration_s
        while self._running and time.time() < end_time:
            await self._tick()
            await asyncio.sleep(self._tick_interval)
        self._running = False

    async def _report_loop(
        self, report_interval_s: float, duration_s: float
    ) -> None:
        end_time      = time.time() + duration_s
        next_report   = time.time() + report_interval_s
        _POLL_S       = 1.0  # wake up every second to check running/duration
        while self._running and time.time() < end_time:
            sleep_s = min(_POLL_S, max(0.0, next_report - time.time()))
            await asyncio.sleep(sleep_s)
            if not self._running or time.time() >= end_time:
                break
            if time.time() >= next_report:
                elapsed = time.time() - self._started_at
                print(self._format_interim_report(elapsed))
                next_report = time.time() + report_interval_s

    # ────────────────────────────────────────────────────────────────────────
    # Per-tick logic
    # ────────────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Execute one full strategy tick on both connectors."""

        # 1. Fetch ticker from both connectors in parallel, time each
        ccxt_mid, cpp_mid = await asyncio.gather(
            self._fetch_ticker_safe(self._ccxt, self.ccxt_metrics),
            self._fetch_ticker_safe(self._cpp,  self.cpp_metrics),
        )

        # Use best available mid price for strategy calculations
        global_mid = ccxt_mid or cpp_mid
        if global_mid is None or global_mid <= 0:
            self.ccxt_metrics.order_errors += 1
            self.cpp_metrics.order_errors  += 1
            return

        # 2. Update shared volatility model
        self._prev_mid = self._curr_mid if self._curr_mid > 0 else global_mid
        self._curr_mid = global_mid
        self._vol_engine.update_price(global_mid)
        vol = self._vol_engine.rolling_vol()
        agg = self._agg_model.compute(vol)

        # 3. Compute the shared order grid (identical for both connectors)
        buy_spreads, sell_spreads = self._spread_engine.compute_levels(
            agg, self._n_levels
        )
        buy_amts  = self._depth_engine.compute_amounts(agg, self._n_levels, side="buy")
        sell_amts = self._depth_engine.compute_amounts(agg, self._n_levels, side="sell")

        # 4. Simulate fills for orders already open (before replacing them)
        if self._prev_mid > 0 and self._prev_mid != self._curr_mid:
            self._ccxt_orders = self._simulate_fills(
                self._ccxt_orders, self.ccxt_metrics,
                self._prev_mid, self._curr_mid,
            )
            self._cpp_orders = self._simulate_fills(
                self._cpp_orders, self.cpp_metrics,
                self._prev_mid, self._curr_mid,
            )

        # 5. Cancel previous orders (clear in-memory state)
        #    In real mode this avoids calling exchange cancel endpoints;
        #    order book positions are just dropped from tracking.
        self._ccxt_orders.clear()
        self._cpp_orders.clear()

        # 6. "Place" new grid in memory for each connector
        #    CCXT connector: occasionally fails (simulated REST timeout in mock mode,
        #                    real failure if ticker call itself failed above)
        #    C++  connector: rarely fails
        if ccxt_mid is not None:
            self._ccxt_orders = self._build_grid(
                global_mid, buy_spreads, sell_spreads,
                buy_amts, sell_amts, "CCXT", self.ccxt_metrics,
            )
        else:
            self.ccxt_metrics.order_errors += 1

        if cpp_mid is not None:
            self._cpp_orders = self._build_grid(
                global_mid, buy_spreads, sell_spreads,
                buy_amts, sell_amts, "C++", self.cpp_metrics,
            )
        else:
            self.cpp_metrics.order_errors += 1

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    async def _fetch_ticker_safe(
        self, connector, metrics: ConnectorMetrics
    ) -> Optional[float]:
        """Fetch ticker, record latency + success/failure. Returns mid or None."""
        metrics.ticks_attempted += 1
        t0 = time.perf_counter()
        try:
            ticker = await connector.fetch_ticker()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            metrics.record_ticker(elapsed_ms, success=True)
            # If connector was previously disconnected, mark as reconnected
            if not metrics._is_connected:
                metrics.record_connect()
            return ticker.mid
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            metrics.record_ticker(elapsed_ms, success=False)
            metrics.record_disconnect()
            log.warning("ab_ticker_error",
                        connector=metrics.name,
                        exchange=self._exchange,
                        error=str(exc))
            return None

    def _build_grid(
        self,
        mid: float,
        buy_spreads: list[float],
        sell_spreads: list[float],
        buy_amts: list[float],
        sell_amts: list[float],
        label: str,
        metrics: ConnectorMetrics,
    ) -> list[_OpenOrder]:
        """Construct in-memory open orders for a full grid at the given mid."""
        orders: list[_OpenOrder] = []
        now = time.time()

        for i, (spread_pct, amt_usd) in enumerate(zip(buy_spreads, buy_amts)):
            price  = mid * (1.0 + spread_pct / 100.0)
            amount = amt_usd / price if price > 0 else 0.0
            if amount <= 0:
                continue
            orders.append(_OpenOrder(
                oid=f"{label}-buy-{int(now*1000)}-{i}",
                side="buy", price=price, amount=amount, placed_at=now,
            ))
            metrics.orders_placed += 1

        for i, (spread_pct, amt_usd) in enumerate(zip(sell_spreads, sell_amts)):
            price  = mid * (1.0 + spread_pct / 100.0)
            amount = amt_usd / price if price > 0 else 0.0
            if amount <= 0:
                continue
            orders.append(_OpenOrder(
                oid=f"{label}-sell-{int(now*1000)}-{i}",
                side="sell", price=price, amount=amount, placed_at=now,
            ))
            metrics.orders_placed += 1

        return orders

    def _simulate_fills(
        self,
        orders: list[_OpenOrder],
        metrics: ConnectorMetrics,
        prev_mid: float,
        curr_mid: float,
    ) -> list[_OpenOrder]:
        """
        Check if price movement between ticks crossed any open order.

        Estimated ask ≈ mid * (1 + SPREAD_APPROX)
        Estimated bid ≈ mid * (1 - SPREAD_APPROX)

        Buy  fills when ask drops through the order price.
        Sell fills when bid rises through the order price.
        """
        prev_ask = prev_mid * (1.0 + SPREAD_APPROX)
        curr_ask = curr_mid * (1.0 + SPREAD_APPROX)
        prev_bid = prev_mid * (1.0 - SPREAD_APPROX)
        curr_bid = curr_mid * (1.0 - SPREAD_APPROX)

        remaining: list[_OpenOrder] = []
        for order in orders:
            filled = False
            if order.side == "buy":
                # Ask crossed down through order price
                if curr_ask <= order.price < prev_ask:
                    filled = True
            else:
                # Bid crossed up through order price
                if curr_bid >= order.price > prev_bid:
                    filled = True

            if filled:
                metrics.record_fill(order.side, order.price, order.amount)
            else:
                remaining.append(order)

        return remaining

    # ────────────────────────────────────────────────────────────────────────
    # Reporting
    # ────────────────────────────────────────────────────────────────────────

    def generate_report(self) -> str:
        """Generate the full comparison report string."""
        elapsed = time.time() - self._started_at
        return self._format_full_report(elapsed)

    def verdict(self) -> dict[str, bool]:
        """Return a dict of Step 11 checkpoint pass/fail results."""
        c = self.ccxt_metrics
        p = self.cpp_metrics
        return {
            "cpp_fill_rate_gte_ccxt":       p.fill_rate_pct      >= c.fill_rate_pct,
            "cpp_reconnects_lte_ccxt":      p.reconnection_count <= c.reconnection_count,
            "cpp_uptime_gte_ccxt":          p.uptime_pct         >= c.uptime_pct,
            "cpp_ticker_success_gte_ccxt":  p.ticker_success_rate_pct >= c.ticker_success_rate_pct,
        }

    def _format_row(self, label: str, ccxt_val: str, cpp_val: str) -> str:
        return f"  {label:<32}  {ccxt_val:>16}  {cpp_val:>16}"

    def _div(self, width: int = 68) -> str:
        return "  " + "─" * width

    def _section(self, title: str) -> str:
        return f"\n  {title}"

    def _elapsed_human(self, elapsed_s: float) -> str:
        td = timedelta(seconds=int(elapsed_s))
        h, r = divmod(int(td.total_seconds()), 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m:02d}m {s:02d}s"

    def _format_full_report(self, elapsed_s: float) -> str:
        c = self.ccxt_metrics
        p = self.cpp_metrics
        width = 74

        lines: list[str] = []

        # Header
        title    = "A/B Paper Trading Report"
        subtitle = (f"exchange: {self._exchange}  |  symbol: {self._symbol}  |  "
                    f"duration: {self._elapsed_human(elapsed_s)}")
        lines += [
            f"\n  {'═'*width}",
            f"  {title:^{width}}",
            f"  {subtitle:^{width}}",
            f"  {'═'*width}",
        ]

        # Connection health
        lines += [
            self._section("Connection Health"),
            self._div(),
            self._format_row("Metric", "CCXT", "C++"),
            self._div(),
            self._format_row("Uptime",
                             f"{c.uptime_pct:.2f}%",
                             f"{p.uptime_pct:.2f}%"),
            self._format_row("Reconnections",
                             str(c.reconnection_count),
                             str(p.reconnection_count)),
            self._format_row("Total downtime",
                             c.downtime_human,
                             p.downtime_human),
            self._div(),
        ]

        # Ticker performance
        lines += [
            self._section("Ticker Performance (fetch_ticker)"),
            self._div(),
            self._format_row("Metric", "CCXT", "C++"),
            self._div(),
            self._format_row("Calls / errors",
                             f"{c.ticker_attempts:,} / {c.ticker_attempts - c.ticker_successes:,}",
                             f"{p.ticker_attempts:,} / {p.ticker_attempts - p.ticker_successes:,}"),
            self._format_row("Success rate",
                             f"{c.ticker_success_rate_pct:.2f}%",
                             f"{p.ticker_success_rate_pct:.2f}%"),
            self._format_row("Mean latency",
                             f"{c.ticker_latency_mean_ms:.1f}ms",
                             f"{p.ticker_latency_mean_ms:.1f}ms"),
            self._format_row("p50 latency",
                             f"{c.ticker_latency_p50_ms:.1f}ms",
                             f"{p.ticker_latency_p50_ms:.1f}ms"),
            self._format_row("p99 latency",
                             f"{c.ticker_latency_p99_ms:.1f}ms",
                             f"{p.ticker_latency_p99_ms:.1f}ms"),
            self._div(),
        ]

        # Order / fill performance
        lines += [
            self._section("Paper Order & Fill Performance"),
            self._div(),
            self._format_row("Metric", "CCXT", "C++"),
            self._div(),
            self._format_row("Ticks run",
                             f"{c.ticks_attempted:,}",
                             f"{p.ticks_attempted:,}"),
            self._format_row("Orders placed",
                             f"{c.orders_placed:,}",
                             f"{p.orders_placed:,}"),
            self._format_row("Ticks with errors",
                             f"{c.order_errors:,}",
                             f"{p.order_errors:,}"),
            self._format_row("Fill count",
                             f"{c.fill_count:,}",
                             f"{p.fill_count:,}"),
            self._format_row("Fill rate",
                             f"{c.fill_rate_pct:.3f}%",
                             f"{p.fill_rate_pct:.3f}%"),
            self._div(),
        ]

        # P&L
        lines += [
            self._section("Paper P&L"),
            self._div(),
            self._format_row("Metric", "CCXT", "C++"),
            self._div(),
            self._format_row("Realized P&L",
                             f"${c.realized_pnl_usd:+.4f}",
                             f"${p.realized_pnl_usd:+.4f}"),
            self._format_row("Total volume",
                             f"${c.total_volume_usd:.2f}",
                             f"${p.total_volume_usd:.2f}"),
            self._div(),
        ]

        # Verdict
        v = self.verdict()
        lines.append(self._section("Step 11 Checkpoint:"))
        checks = [
            ("cpp_fill_rate_gte_ccxt",
             f"C++ fill rate ≥ CCXT           ({p.fill_rate_pct:.3f}% vs {c.fill_rate_pct:.3f}%)"),
            ("cpp_reconnects_lte_ccxt",
             f"C++ reconnections ≤ CCXT       ({p.reconnection_count} vs {c.reconnection_count})"),
            ("cpp_uptime_gte_ccxt",
             f"C++ uptime ≥ CCXT              ({p.uptime_pct:.2f}% vs {c.uptime_pct:.2f}%)"),
            ("cpp_ticker_success_gte_ccxt",
             f"C++ ticker success ≥ CCXT      ({p.ticker_success_rate_pct:.2f}% vs {c.ticker_success_rate_pct:.2f}%)"),
        ]
        for key, desc in checks:
            mark = "✓" if v[key] else "✗"
            lines.append(f"    {mark}  {desc}")

        all_pass = all(v.values())
        lines.append(f"\n  {'PASS ✓' if all_pass else 'FAIL ✗'} — "
                     f"{'All checkpoints met.' if all_pass else 'One or more checkpoints failed.'}")
        lines.append("")

        return "\n".join(lines)

    def _format_interim_report(self, elapsed_s: float) -> str:
        """Condensed one-screen update for periodic printing."""
        c = self.ccxt_metrics
        p = self.cpp_metrics
        return (
            f"\n  ── A/B progress @ {self._elapsed_human(elapsed_s)} ──────────────────────────\n"
            f"  {'':32}  {'CCXT':>16}  {'C++':>16}\n"
            f"  {'Ticker mean latency':<32}  {c.ticker_latency_mean_ms:>14.1f}ms  {p.ticker_latency_mean_ms:>14.1f}ms\n"
            f"  {'Ticker success rate':<32}  {c.ticker_success_rate_pct:>15.2f}%  {p.ticker_success_rate_pct:>15.2f}%\n"
            f"  {'Fill rate':<32}  {c.fill_rate_pct:>15.3f}%  {p.fill_rate_pct:>15.3f}%\n"
            f"  {'Reconnections':<32}  {c.reconnection_count:>16}  {p.reconnection_count:>16}\n"
            f"  {'Realized P&L':<32}  ${c.realized_pnl_usd:>+15.4f}  ${p.realized_pnl_usd:>+15.4f}\n"
        )
