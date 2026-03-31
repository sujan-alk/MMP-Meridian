"""
Tests for quant/spread_engine.py — SpreadEngine.

Validates:
- Power-curve interpolation produces correct level distributions
- Aggressiveness=0 clusters levels near the widest spread
- Aggressiveness=1 clusters levels near the tightest spread
- Aggressiveness=0.5 produces approximately linear spacing
- Spread-to-price conversion is mathematically correct
- Edge cases: single level, extreme gamma values, zero mid-price
- Asymmetric buy/sell spread ranges
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from config.schema import SpreadConfig
from quant.spread_engine import SpreadEngine


class TestComputeLevels:
    """Tests for SpreadEngine.compute_levels()."""

    def test_returns_correct_number_of_levels(self, spread_config):
        """Each side should have exactly n_levels values."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels(aggressiveness=0.5, n_levels=15)
        assert len(buy) == 15
        assert len(sell) == 15

    def test_buy_spreads_are_negative(self, spread_config):
        """All buy spread percentages should be negative (below mid)."""
        engine = SpreadEngine(spread_config)
        buy, _ = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        for s in buy:
            assert s < 0, f"Buy spread {s} should be negative"

    def test_sell_spreads_are_positive(self, spread_config):
        """All sell spread percentages should be positive (above mid)."""
        engine = SpreadEngine(spread_config)
        _, sell = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        for s in sell:
            assert s > 0, f"Sell spread {s} should be positive"

    def test_buy_level0_is_tightest(self, spread_config):
        """Level 0 should be the tightest (closest to mid, least negative)."""
        engine = SpreadEngine(spread_config)
        buy, _ = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        # Tightest = buy_max_pct = -0.1
        assert buy[0] == pytest.approx(-0.1, abs=0.01)

    def test_buy_last_level_is_widest(self, spread_config):
        """Last level should be the widest (furthest from mid, most negative)."""
        engine = SpreadEngine(spread_config)
        buy, _ = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        # Widest = buy_min_pct = -5.0
        assert buy[-1] == pytest.approx(-5.0, abs=0.01)

    def test_sell_level0_is_tightest(self, spread_config):
        """Sell level 0 should be closest to mid (sell_min_pct)."""
        engine = SpreadEngine(spread_config)
        _, sell = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        assert sell[0] == pytest.approx(0.3, abs=0.01)

    def test_sell_last_level_is_widest(self, spread_config):
        """Sell last level should be furthest from mid (sell_max_pct)."""
        engine = SpreadEngine(spread_config)
        _, sell = engine.compute_levels(aggressiveness=0.5, n_levels=10)
        assert sell[-1] == pytest.approx(7.0, abs=0.01)

    def test_levels_are_monotonically_sorted(self, spread_config):
        """Buy levels should be monotonically decreasing; sell levels increasing."""
        engine = SpreadEngine(spread_config)
        for agg in [0.0, 0.25, 0.5, 0.75, 1.0]:
            buy, sell = engine.compute_levels(aggressiveness=agg, n_levels=15)
            # Buy: -0.1, -0.5, ..., -5.0 (decreasing)
            for i in range(len(buy) - 1):
                assert buy[i] >= buy[i + 1], f"Buy levels not monotone at agg={agg}"
            # Sell: 0.3, 1.0, ..., 7.0 (increasing)
            for i in range(len(sell) - 1):
                assert sell[i] <= sell[i + 1], f"Sell levels not monotone at agg={agg}"


class TestAggressivenessEffect:
    """Tests that aggressiveness correctly controls level clustering."""

    def test_high_agg_clusters_near_tightest_end(self, spread_config):
        """
        At agg=1.0, gamma = exp(+curve_strength) ≈ 54.6.
        With gamma >> 1, t^gamma stays near 0 for most t, so most levels
        cluster near the tightest value (buy_max_pct = -0.1%).
        This matches the intent: low volatility → aggressive → tight spreads.
        """
        engine = SpreadEngine(spread_config)
        buy, _ = engine.compute_levels(aggressiveness=1.0, n_levels=15)
        median_buy = sorted(buy)[len(buy) // 2]
        assert median_buy > -1.0, f"Median buy {median_buy} should be near tightest at high agg"

    def test_low_agg_clusters_near_widest_end(self, spread_config):
        """
        At agg=0.0, gamma = exp(-curve_strength) ≈ 0.018.
        With gamma < 1, t^gamma rises quickly → most levels jump toward widest.
        This matches the intent: high volatility → passive → wide spreads.
        """
        engine = SpreadEngine(spread_config)
        buy_high, _ = engine.compute_levels(aggressiveness=1.0, n_levels=15)
        buy_low, _ = engine.compute_levels(aggressiveness=0.0, n_levels=15)
        med_high = sorted(buy_high)[len(buy_high) // 2]
        med_low = sorted(buy_low)[len(buy_low) // 2]
        assert med_low < med_high, "Low agg should push median toward widest"

    def test_mid_agg_is_approximately_linear(self, spread_config):
        """At agg=0.5, levels should be approximately evenly spaced (gamma≈1)."""
        engine = SpreadEngine(spread_config)
        buy, _ = engine.compute_levels(aggressiveness=0.5, n_levels=5)
        # Gamma should be exp(0) = 1.0 → linear
        # Expect levels at approx -0.1, -1.325, -2.55, -3.775, -5.0
        expected_step = (buy[-1] - buy[0]) / (len(buy) - 1)
        for i in range(1, len(buy)):
            actual_step = buy[i] - buy[i - 1]
            assert actual_step == pytest.approx(expected_step, rel=0.05)

    def test_agg_clamped_below_zero(self, spread_config):
        """Aggressiveness below 0.0 should be clamped to 0.0."""
        engine = SpreadEngine(spread_config)
        buy_neg, _ = engine.compute_levels(aggressiveness=-0.5, n_levels=5)
        buy_zero, _ = engine.compute_levels(aggressiveness=0.0, n_levels=5)
        np.testing.assert_array_almost_equal(buy_neg, buy_zero)

    def test_agg_clamped_above_one(self, spread_config):
        """Aggressiveness above 1.0 should be clamped to 1.0."""
        engine = SpreadEngine(spread_config)
        buy_high, _ = engine.compute_levels(aggressiveness=1.5, n_levels=5)
        buy_one, _ = engine.compute_levels(aggressiveness=1.0, n_levels=5)
        np.testing.assert_array_almost_equal(buy_high, buy_one)


class TestPricesFromSpreads:
    """Tests for SpreadEngine.prices_from_spreads()."""

    def test_basic_conversion(self, spread_config):
        """Spread percentages should be correctly applied to global mid."""
        engine = SpreadEngine(spread_config)
        mid = 0.10  # $0.10
        buy_spreads = [-1.0, -2.0, -3.0]
        sell_spreads = [1.0, 2.0, 3.0]
        buy_prices, sell_prices = engine.prices_from_spreads(mid, buy_spreads, sell_spreads)

        assert buy_prices[0] == pytest.approx(0.10 * (1 - 0.01), rel=1e-6)
        assert buy_prices[1] == pytest.approx(0.10 * (1 - 0.02), rel=1e-6)
        assert sell_prices[0] == pytest.approx(0.10 * (1 + 0.01), rel=1e-6)
        assert sell_prices[2] == pytest.approx(0.10 * (1 + 0.03), rel=1e-6)

    def test_buy_prices_below_mid(self, spread_config):
        """All buy prices should be below the global mid."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels(0.5, 10)
        buy_prices, sell_prices = engine.prices_from_spreads(0.10, buy, sell)
        for p in buy_prices:
            assert p < 0.10

    def test_sell_prices_above_mid(self, spread_config):
        """All sell prices should be above the global mid."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels(0.5, 10)
        buy_prices, sell_prices = engine.prices_from_spreads(0.10, buy, sell)
        for p in sell_prices:
            assert p > 0.10

    def test_zero_mid_price(self, spread_config):
        """Zero mid-price should produce zero prices (edge case)."""
        engine = SpreadEngine(spread_config)
        buy_prices, sell_prices = engine.prices_from_spreads(0.0, [-1.0], [1.0])
        assert buy_prices[0] == 0.0
        assert sell_prices[0] == 0.0


class TestSingleLevel:
    """Edge case: n_levels = 1."""

    def test_single_level_returns_tightest(self, spread_config):
        """With 1 level, it should be at the tightest spread."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels(aggressiveness=0.5, n_levels=1)
        assert len(buy) == 1
        assert len(sell) == 1
        # Single level = tightest (t=0 → level = tightest)
        assert buy[0] == pytest.approx(-0.1, abs=0.01)
        assert sell[0] == pytest.approx(0.3, abs=0.01)


class TestCurveStrength:
    """Test the effect of varying curve_strength."""

    def test_higher_curve_strength_amplifies_agg_effect(self):
        """
        Higher curve_strength amplifies the difference between agg=0 and agg=1.
        With stronger curves, the same aggressiveness delta produces a bigger
        difference in level distributions.
        """
        config_mild = SpreadConfig(
            buy_min_pct=-5.0, buy_max_pct=-0.1,
            sell_min_pct=0.3, sell_max_pct=7.0,
            curve_strength=1.0,
        )
        config_strong = SpreadConfig(
            buy_min_pct=-5.0, buy_max_pct=-0.1,
            sell_min_pct=0.3, sell_max_pct=7.0,
            curve_strength=8.0,
        )
        engine_mild = SpreadEngine(config_mild)
        engine_strong = SpreadEngine(config_strong)

        # Measure the spread between agg=0 and agg=1 distributions
        buy_mild_low, _ = engine_mild.compute_levels(0.0, 15)
        buy_mild_high, _ = engine_mild.compute_levels(1.0, 15)
        buy_strong_low, _ = engine_strong.compute_levels(0.0, 15)
        buy_strong_high, _ = engine_strong.compute_levels(1.0, 15)

        # Difference in median level between agg=0 and agg=1
        diff_mild = abs(np.median(buy_mild_high) - np.median(buy_mild_low))
        diff_strong = abs(np.median(buy_strong_high) - np.median(buy_strong_low))

        # Stronger curve should amplify the difference
        assert diff_strong > diff_mild
