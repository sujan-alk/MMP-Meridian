"""
Tests for quant/depth_engine.py — DepthEngine.

Validates:
- Budget distribution across levels
- Passive (geometric decay) vs equal distribution blending
- Aggressiveness effect on distribution shape
- Inventory skew adjustment of buy/sell budgets
- Minimum order size enforcement
- USD to token conversion
- Edge cases: single level, zero price, extreme skew
"""

from __future__ import annotations

import numpy as np
import pytest

from config.schema import DepthConfig
from quant.depth_engine import DepthEngine


@pytest.fixture
def engine(depth_config) -> DepthEngine:
    return DepthEngine(depth_config)


class TestComputeAmounts:
    """Tests for compute_amounts() budget distribution."""

    def test_returns_correct_number_of_levels(self, engine):
        """Should return exactly n_levels amounts."""
        amounts = engine.compute_amounts(aggressiveness=0.5, n_levels=15)
        assert len(amounts) == 15

    def test_all_amounts_positive(self, engine):
        """All amounts should be positive."""
        for agg in [0.0, 0.25, 0.5, 0.75, 1.0]:
            amounts = engine.compute_amounts(aggressiveness=agg, n_levels=15)
            for a in amounts:
                assert a > 0, f"Amount {a} should be positive at agg={agg}"

    def test_amounts_respect_minimum(self, engine):
        """No amount should be below min_order_usd."""
        amounts = engine.compute_amounts(aggressiveness=0.0, n_levels=15)
        for a in amounts:
            assert a >= engine.cfg.min_order_usd

    def test_passive_distribution_front_loaded(self, engine):
        """At agg=0, amounts should be front-loaded (level 0 gets most)."""
        amounts = engine.compute_amounts(aggressiveness=0.0, n_levels=10)
        # Level 0 should have the largest amount (before min-order clamping)
        # Due to geometric decay, first level > last level
        assert amounts[0] > amounts[-1]

    def test_equal_distribution_at_full_agg(self, engine):
        """At agg=1, amounts should be approximately equal."""
        amounts = engine.compute_amounts(aggressiveness=1.0, n_levels=10)
        # With blend=1 (agg^4 = 1 at agg=1), distribution is uniform
        # All amounts should be close to half_budget / n_levels
        expected = (engine.cfg.total_budget_usd / 2.0) / 10
        for a in amounts:
            assert a == pytest.approx(expected, rel=0.01)

    def test_higher_agg_more_uniform(self, engine):
        """Higher aggressiveness should produce a more uniform distribution."""
        amounts_low = engine.compute_amounts(aggressiveness=0.0, n_levels=10)
        amounts_high = engine.compute_amounts(aggressiveness=1.0, n_levels=10)

        std_low = np.std(amounts_low)
        std_high = np.std(amounts_high)
        assert std_high < std_low


class TestSkewAdjustment:
    """Tests for inventory skew effect on buy/sell budgets."""

    def test_neutral_skew_equal_budgets(self, engine):
        """Skew=1.0 should give equal buy and sell budgets."""
        buy = engine.compute_amounts(0.5, 10, skew_factor=1.0, side="buy")
        sell = engine.compute_amounts(0.5, 10, skew_factor=1.0, side="sell")
        assert sum(buy) == pytest.approx(sum(sell), rel=0.01)

    def test_high_skew_increases_buy_budget(self, engine):
        """Skew > 1.0 (token deficit) should increase buy budget."""
        buy_neutral = engine.compute_amounts(0.5, 10, skew_factor=1.0, side="buy")
        buy_high = engine.compute_amounts(0.5, 10, skew_factor=1.5, side="buy")
        assert sum(buy_high) > sum(buy_neutral)

    def test_high_skew_decreases_sell_budget(self, engine):
        """Skew > 1.0 (token deficit) should decrease sell budget."""
        sell_neutral = engine.compute_amounts(0.5, 10, skew_factor=1.0, side="sell")
        sell_high = engine.compute_amounts(0.5, 10, skew_factor=1.5, side="sell")
        assert sum(sell_high) < sum(sell_neutral)

    def test_low_skew_increases_sell_budget(self, engine):
        """Skew < 1.0 (token surplus) should increase sell budget."""
        sell_neutral = engine.compute_amounts(0.5, 10, skew_factor=1.0, side="sell")
        sell_low = engine.compute_amounts(0.5, 10, skew_factor=0.7, side="sell")
        assert sum(sell_low) > sum(sell_neutral)

    def test_skew_clamped_to_bounds(self, engine):
        """Extreme skew values should be clamped to [0.5, 2.0]."""
        buy_extreme = engine.compute_amounts(0.5, 10, skew_factor=5.0, side="buy")
        buy_max = engine.compute_amounts(0.5, 10, skew_factor=2.0, side="buy")
        assert sum(buy_extreme) == pytest.approx(sum(buy_max), rel=0.01)

    def test_buy_sell_budgets_complementary(self, engine):
        """Buy and sell budgets should sum correctly given the skew."""
        for skew in [0.5, 0.8, 1.0, 1.2, 2.0]:
            buy = engine.compute_amounts(1.0, 5, skew_factor=skew, side="buy")
            sell = engine.compute_amounts(1.0, 5, skew_factor=skew, side="sell")
            # buy_budget = half * skew, sell_budget = half * (2 - skew)
            # Total should be approximately total_budget_usd (before min-order clamping)
            half = engine.cfg.total_budget_usd / 2.0
            expected_total = half * min(max(skew, 0.5), 2.0) + half * min(max(2.0 - skew, 0.5), 2.0)
            # With agg=1.0 and large n, amounts are uniform so sum ~ budget
            actual_total = sum(buy) + sum(sell)
            assert actual_total == pytest.approx(expected_total, rel=0.01)


class TestUsdToTokenAmount:
    """Tests for USD to token conversion."""

    def test_basic_conversion(self, engine):
        """$10 at $0.10/token should be 100 tokens."""
        assert engine.usd_to_token_amount(10.0, 0.10) == pytest.approx(100.0)

    def test_zero_price_returns_zero(self, engine):
        """Zero price should return 0 tokens (avoid division by zero)."""
        assert engine.usd_to_token_amount(10.0, 0.0) == 0.0

    def test_negative_price_returns_zero(self, engine):
        """Negative price should return 0 tokens."""
        assert engine.usd_to_token_amount(10.0, -0.05) == 0.0

    def test_small_price_large_amount(self, engine):
        """Very small token price should produce large token amount."""
        tokens = engine.usd_to_token_amount(100.0, 0.001)
        assert tokens == pytest.approx(100000.0)


class TestSingleLevel:
    """Edge case: single order level."""

    def test_single_level_gets_full_budget(self, engine):
        """With 1 level, it should get the full half-budget."""
        amounts = engine.compute_amounts(0.5, n_levels=1, side="buy")
        assert len(amounts) == 1
        # Single level: passive=[1.0], equal=[1.0] → amount = half_budget
        expected = engine.cfg.total_budget_usd / 2.0
        assert amounts[0] == pytest.approx(expected)


class TestCurveStrength:
    """Test the effect of varying depth curve_strength."""

    def test_higher_curve_strength_sharper_transition(self):
        """Higher curve_strength should make the passive-to-equal transition sharper."""
        config_mild = DepthConfig(levels=15, total_budget_usd=1000.0, curve_strength=1.0, min_order_usd=1.0)
        config_strong = DepthConfig(levels=15, total_budget_usd=1000.0, curve_strength=8.0, min_order_usd=1.0)

        engine_mild = DepthEngine(config_mild)
        engine_strong = DepthEngine(config_strong)

        # At agg=0.7:
        # mild: blend = 0.7^1 = 0.7 (mostly equal)
        # strong: blend = 0.7^8 ≈ 0.057 (mostly passive)
        amounts_mild = engine_mild.compute_amounts(0.7, 10)
        amounts_strong = engine_strong.compute_amounts(0.7, 10)

        # Strong curve should have more variance (passive = front-loaded)
        assert np.std(amounts_strong) > np.std(amounts_mild)
