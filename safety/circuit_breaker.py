"""
Circuit Breaker — halts trading if daily loss or drawdown thresholds are breached.

Tracks:
- Starting equity for the day (reset at midnight UTC)
- Peak equity since start (for drawdown calculation)

Halts when:
- Daily loss > max_daily_loss_pct
- Drawdown from peak > max_drawdown_pct
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from config.schema import SafetyConfig
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("circuit_breaker")


class CircuitBreaker:
    """
    Tracks P&L and halts the bot if daily loss or drawdown limits are breached.
    Equity is measured in USD-equivalent (current_usd + current_token * mid_price).
    """

    def __init__(self, config: SafetyConfig, exchange: str):
        self.config = config
        self.exchange = exchange
        self._start_equity: float | None = None
        self._peak_equity: float | None = None
        self._day_start: int = self._current_day()
        self._tripped = False
        self._trip_reason: str = ""

    def record_equity(self, usd: float, token: float, mid_price: float) -> None:
        """
        Update equity snapshot. Call each tick after fetching balance + global mid.

        Args:
            usd: current USD/USDT balance
            token: current ALKIMI token balance
            mid_price: current global mid-price
        """
        equity = usd + token * mid_price

        # Reset on new calendar day (UTC)
        today = self._current_day()
        if today != self._day_start:
            log.info("circuit_breaker_daily_reset", exchange=self.exchange)
            self._start_equity = equity
            self._peak_equity = equity
            self._day_start = today
            self._tripped = False
            self._trip_reason = ""
            return

        if self._start_equity is None:
            self._start_equity = equity
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity

        # Check daily loss
        if self._start_equity > 0:
            daily_loss_pct = (self._start_equity - equity) / self._start_equity * 100.0
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                self._trip(f"Daily loss {daily_loss_pct:.1f}% >= limit {self.config.max_daily_loss_pct}%")
                return

        # Check drawdown from peak
        if self._peak_equity and self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100.0
            if drawdown_pct >= self.config.max_drawdown_pct:
                self._trip(f"Drawdown {drawdown_pct:.1f}% >= limit {self.config.max_drawdown_pct}%")

    def is_tripped(self) -> bool:
        return self._tripped

    def reset(self) -> None:
        """Manual reset via API. Re-records current equity as start."""
        self._tripped = False
        self._trip_reason = ""
        self._start_equity = None  # Will be re-recorded on next tick
        self._peak_equity = None
        log.info("circuit_breaker_reset", exchange=self.exchange)

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def _trip(self, reason: str) -> None:
        if not self._tripped:
            self._tripped = True
            self._trip_reason = reason
            log.error("circuit_breaker_tripped", exchange=self.exchange, reason=reason)

    @staticmethod
    def _current_day() -> int:
        """Returns the current UTC day as an integer (days since epoch)."""
        return datetime.now(timezone.utc).toordinal()
