"""
Tests for core/inventory_tracker.py — InventoryTracker.

Validates:
- Initial balance recording (auto-detect and config override)
- Skew factor computation at various drift levels
- Skew clamping to [0.5, 2.0] at extreme drift
- Drift percentage calculations
- Rebalance threshold detection
- Edge cases: zero initial balance, no initial recorded
"""

from __future__ import annotations

import pytest

from config.schema import ExchangeBotConfig, SpreadConfig, DepthConfig, SafetyConfig
from core.inventory_tracker import InventoryTracker, InventoryState
from exchange.base import Balance


@pytest.fixture
def tracker(exchange_bot_config) -> InventoryTracker:
    """Create an InventoryTracker with default config."""
    t = InventoryTracker(exchange_bot_config)
    t.record_initial(Balance(usd=1000.0, token=5000.0))
    return t


class TestRecordInitial:
    """Tests for recording the baseline balance."""

    def test_record_initial_from_balance(self, exchange_bot_config):
        """record_initial should set the baseline from a Balance object."""
        t = InventoryTracker(exchange_bot_config)
        t.record_initial(Balance(usd=500.0, token=3000.0))
        state = t.state()
        assert state.initial_usd == 500.0
        assert state.initial_token == 3000.0

    def test_set_initial_from_config(self, exchange_bot_config):
        """set_initial_from_config should override any auto-detected initial."""
        t = InventoryTracker(exchange_bot_config)
        t.record_initial(Balance(usd=500.0, token=3000.0))
        t.set_initial_from_config(usd=1000.0, token=10000.0)
        state = t.state()
        assert state.initial_usd == 1000.0
        assert state.initial_token == 10000.0

    def test_update_auto_records_initial(self, exchange_bot_config):
        """First call to update() should auto-record the initial balance."""
        t = InventoryTracker(exchange_bot_config)
        t.update(Balance(usd=800.0, token=4000.0))
        state = t.state()
        assert state.initial_usd == 800.0
        assert state.initial_token == 4000.0


class TestSkewFactor:
    """Tests for skew_factor() computation."""

    def test_no_drift_returns_one(self, tracker):
        """When current == initial, skew should be 1.0."""
        tracker.update(Balance(usd=1000.0, token=5000.0))
        assert tracker.skew_factor() == pytest.approx(1.0)

    def test_sold_tokens_increases_skew(self, tracker):
        """Selling tokens (token deficit) should produce skew > 1.0."""
        # Sold 1000 tokens (20% deficit)
        tracker.update(Balance(usd=1100.0, token=4000.0))
        skew = tracker.skew_factor()
        # drift = (4000-5000)/5000 = -0.2 → skew = 1 - (-0.2)*2 = 1.4
        assert skew == pytest.approx(1.4)
        assert skew > 1.0

    def test_bought_tokens_decreases_skew(self, tracker):
        """Buying tokens (token surplus) should produce skew < 1.0."""
        # Bought 500 tokens (10% surplus)
        tracker.update(Balance(usd=950.0, token=5500.0))
        skew = tracker.skew_factor()
        # drift = (5500-5000)/5000 = 0.1 → skew = 1 - 0.1*2 = 0.8
        assert skew == pytest.approx(0.8)
        assert skew < 1.0

    def test_skew_clamped_at_upper_bound(self, tracker):
        """Extreme token deficit should clamp skew to 2.0."""
        # Sold 90% of tokens
        tracker.update(Balance(usd=1800.0, token=500.0))
        skew = tracker.skew_factor()
        # drift = (500-5000)/5000 = -0.9 → raw_skew = 1 - (-0.9)*2 = 2.8 → clamp to 2.0
        assert skew == 2.0

    def test_skew_clamped_at_lower_bound(self, tracker):
        """Extreme token surplus should clamp skew to 0.5."""
        # Doubled token position
        tracker.update(Balance(usd=200.0, token=10000.0))
        skew = tracker.skew_factor()
        # drift = (10000-5000)/5000 = 1.0 → raw_skew = 1 - 1.0*2 = -1.0 → clamp to 0.5
        assert skew == 0.5

    def test_skew_with_no_initial_returns_one(self, exchange_bot_config):
        """If no initial balance is recorded, skew should default to 1.0."""
        t = InventoryTracker(exchange_bot_config)
        assert t.skew_factor() == 1.0

    def test_skew_with_zero_initial_token_returns_one(self, exchange_bot_config):
        """Zero initial token should return skew=1.0 (avoid division by zero)."""
        t = InventoryTracker(exchange_bot_config)
        t.record_initial(Balance(usd=1000.0, token=0.0))
        t.update(Balance(usd=1000.0, token=100.0))
        assert t.skew_factor() == 1.0


class TestInventoryState:
    """Tests for the state() snapshot."""

    def test_drift_percentages(self, tracker):
        """Drift percentages should be correctly computed."""
        # Start: 1000 USD, 5000 token
        # Current: 1200 USD (sold tokens), 4000 token
        tracker.update(Balance(usd=1200.0, token=4000.0))
        state = tracker.state()
        # USD drift: (1200-1000)/1000 * 100 = 20%
        assert state.usd_drift_pct == pytest.approx(20.0)
        # Token drift: (4000-5000)/5000 * 100 = -20%
        assert state.token_drift_pct == pytest.approx(-20.0)

    def test_state_contains_current_balances(self, tracker):
        """State should reflect the most recent balance update."""
        tracker.update(Balance(usd=750.0, token=6000.0))
        state = tracker.state()
        assert state.usd == 750.0
        assert state.token == 6000.0

    def test_state_contains_skew(self, tracker):
        """State should include the computed skew_factor."""
        tracker.update(Balance(usd=1000.0, token=5000.0))
        state = tracker.state()
        assert state.skew_factor == pytest.approx(1.0)


class TestShouldRebalance:
    """Tests for rebalance threshold detection."""

    def test_no_rebalance_within_threshold(self, tracker):
        """Small drift should not trigger rebalance."""
        # 5% token drift (below 10% threshold)
        tracker.update(Balance(usd=1050.0, token=4750.0))
        assert tracker.should_rebalance() is False

    def test_rebalance_triggered_above_threshold(self, tracker):
        """Large drift should trigger rebalance."""
        # 20% token deficit (above 10% threshold)
        tracker.update(Balance(usd=1200.0, token=4000.0))
        assert tracker.should_rebalance() is True

    def test_rebalance_triggered_for_surplus(self, tracker):
        """Token surplus above threshold should also trigger rebalance."""
        # 15% token surplus
        tracker.update(Balance(usd=850.0, token=5750.0))
        assert tracker.should_rebalance() is True

    def test_no_rebalance_without_initial(self, exchange_bot_config):
        """Without initial balance, should_rebalance returns False."""
        t = InventoryTracker(exchange_bot_config)
        assert t.should_rebalance() is False
