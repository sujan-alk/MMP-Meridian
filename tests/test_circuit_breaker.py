"""
Tests for safety/circuit_breaker.py — CircuitBreaker.

Validates:
- Daily loss detection and tripping
- Drawdown from peak detection
- Automatic daily reset at midnight UTC
- Manual reset via API
- Edge cases: first tick, zero equity, peak tracking
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from config.schema import SafetyConfig
from safety.circuit_breaker import CircuitBreaker


@pytest.fixture
def cb(safety_config) -> CircuitBreaker:
    return CircuitBreaker(safety_config, exchange="kucoin")


class TestInitialState:
    """Tests for circuit breaker initial conditions."""

    def test_not_tripped_initially(self, cb):
        """Circuit breaker should start in non-tripped state."""
        assert cb.is_tripped() is False

    def test_no_trip_reason_initially(self, cb):
        """Trip reason should be empty initially."""
        assert cb.trip_reason == ""


class TestDailyLoss:
    """Tests for daily loss threshold detection."""

    def test_no_trip_within_limit(self, cb):
        """Losses within the daily limit should not trip the breaker."""
        # Start equity: 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        # Equity = 5000 + 50000*0.10 = 10000

        # 5% loss → equity = 9500
        cb.record_equity(usd=4500.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is False

    def test_trip_on_daily_loss(self, cb):
        """Losses exceeding the daily limit (10%) should trip the breaker."""
        # Start equity: 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)

        # 11% loss → equity = 8900
        cb.record_equity(usd=3900.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True
        assert "Daily loss" in cb.trip_reason

    def test_trip_at_exact_threshold(self, cb):
        """Loss at exactly the threshold should trip."""
        # Start equity: 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)

        # Exactly 10% loss → equity = 9000
        cb.record_equity(usd=4000.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True

    def test_trip_stays_tripped(self, cb):
        """Once tripped, recording more equity should not un-trip."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        cb.record_equity(usd=3000.0, token=50000.0, mid_price=0.10)  # Trip
        assert cb.is_tripped() is True

        # Equity recovers
        cb.record_equity(usd=6000.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True  # Still tripped


class TestDrawdown:
    """Tests for drawdown from peak detection."""

    def test_no_trip_within_drawdown_limit(self, cb):
        """Drawdown within the limit (15%) should not trip."""
        # Peak equity: 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)

        # Equity rises to 12000 (new peak)
        cb.record_equity(usd=7000.0, token=50000.0, mid_price=0.10)

        # 10% drawdown from peak → equity = 10800
        cb.record_equity(usd=5800.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is False

    def test_trip_on_drawdown(self, cb):
        """Drawdown exceeding the limit (15%) should trip."""
        # Start and peak at 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)

        # Equity rises to 12000 (new peak)
        cb.record_equity(usd=7000.0, token=50000.0, mid_price=0.10)

        # 16% drawdown from peak → equity = 10080
        cb.record_equity(usd=5080.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True
        assert "Drawdown" in cb.trip_reason

    def test_peak_tracking(self, cb):
        """Peak should update as equity increases."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)  # 10000
        cb.record_equity(usd=6000.0, token=50000.0, mid_price=0.10)  # 11000
        cb.record_equity(usd=7000.0, token=50000.0, mid_price=0.10)  # 12000
        assert cb._peak_equity == 12000.0

        # Equity drops but peak stays
        cb.record_equity(usd=6500.0, token=50000.0, mid_price=0.10)  # 11500
        assert cb._peak_equity == 12000.0


class TestDailyReset:
    """Tests for automatic daily reset at midnight UTC."""

    def test_reset_on_new_day(self, cb):
        """Circuit breaker should auto-reset when the calendar day changes."""
        # Trip the breaker
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        cb.record_equity(usd=3000.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True

        # Simulate day change by mocking _current_day
        original_day = cb._day_start
        with patch.object(CircuitBreaker, '_current_day', return_value=original_day + 1):
            cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
            assert cb.is_tripped() is False
            assert cb.trip_reason == ""


class TestManualReset:
    """Tests for manual reset via API."""

    def test_reset_clears_trip(self, cb):
        """Manual reset should clear the tripped state."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        cb.record_equity(usd=3000.0, token=50000.0, mid_price=0.10)
        assert cb.is_tripped() is True

        cb.reset()
        assert cb.is_tripped() is False
        assert cb.trip_reason == ""

    def test_reset_clears_baseline(self, cb):
        """After reset, next equity recording becomes the new baseline."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        cb.reset()

        assert cb._start_equity is None
        assert cb._peak_equity is None

        # New baseline
        cb.record_equity(usd=3000.0, token=50000.0, mid_price=0.10)
        assert cb._start_equity == 8000.0  # 3000 + 50000*0.10


class TestEdgeCases:
    """Edge case tests."""

    def test_first_tick_records_baseline(self, cb):
        """First equity recording should set start and peak."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)
        assert cb._start_equity == 10000.0
        assert cb._peak_equity == 10000.0

    def test_zero_mid_price(self, cb):
        """Zero mid-price should result in equity = usd only."""
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.0)
        assert cb._start_equity == 5000.0

    def test_zero_start_equity_no_division_error(self, cb):
        """Zero start equity should not cause division by zero."""
        cb.record_equity(usd=0.0, token=0.0, mid_price=0.10)
        cb.record_equity(usd=100.0, token=0.0, mid_price=0.10)
        # Should not raise — _start_equity is 0, check is skipped
        assert cb.is_tripped() is False

    def test_equity_via_token_price_change(self, cb):
        """P&L from token price movement should be detected."""
        # Start: equity = 5000 + 50000*0.10 = 10000
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.10)

        # Token price crashes 30%: equity = 5000 + 50000*0.07 = 8500 (15% loss)
        cb.record_equity(usd=5000.0, token=50000.0, mid_price=0.07)
        assert cb.is_tripped() is True
