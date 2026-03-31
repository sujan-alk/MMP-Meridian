"""
Prometheus-style metrics collector for the MM bot.

Exposes counters and gauges as a simple text endpoint at /api/metrics.
Format: Prometheus text exposition format (text/plain; version=0.0.4).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class MetricsCollector:
    """
    Simple in-memory metrics collector with thread-safe counter/gauge operations.
    """

    # Counters (monotonically increasing)
    orders_placed: int = 0
    orders_cancelled: int = 0
    fills_total: int = 0

    # Gauges (point-in-time values)
    spread_bps: float = 0.0
    inventory_skew: float = 1.0
    equity_usd: float = 0.0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc_orders_placed(self, n: int = 1) -> None:
        with self._lock:
            self.orders_placed += n

    def inc_orders_cancelled(self, n: int = 1) -> None:
        with self._lock:
            self.orders_cancelled += n

    def inc_fills(self, n: int = 1) -> None:
        with self._lock:
            self.fills_total += n

    def set_spread_bps(self, value: float) -> None:
        with self._lock:
            self.spread_bps = value

    def set_inventory_skew(self, value: float) -> None:
        with self._lock:
            self.inventory_skew = value

    def set_equity_usd(self, value: float) -> None:
        with self._lock:
            self.equity_usd = value

    def render(self) -> str:
        """
        Render all metrics in Prometheus text exposition format.
        """
        with self._lock:
            lines = [
                "# HELP mm_orders_placed_total Total limit orders placed",
                "# TYPE mm_orders_placed_total counter",
                f"mm_orders_placed_total {self.orders_placed}",
                "",
                "# HELP mm_orders_cancelled_total Total orders cancelled",
                "# TYPE mm_orders_cancelled_total counter",
                f"mm_orders_cancelled_total {self.orders_cancelled}",
                "",
                "# HELP mm_fills_total Total fills received",
                "# TYPE mm_fills_total counter",
                f"mm_fills_total {self.fills_total}",
                "",
                "# HELP mm_spread_bps Current tightest spread in basis points",
                "# TYPE mm_spread_bps gauge",
                f"mm_spread_bps {self.spread_bps:.2f}",
                "",
                "# HELP mm_inventory_skew Current inventory skew factor",
                "# TYPE mm_inventory_skew gauge",
                f"mm_inventory_skew {self.inventory_skew:.4f}",
                "",
                "# HELP mm_equity_usd Total equity in USD",
                "# TYPE mm_equity_usd gauge",
                f"mm_equity_usd {self.equity_usd:.2f}",
                "",
            ]
        return "\n".join(lines) + "\n"


# Singleton instance — import and use across the bot
metrics = MetricsCollector()
