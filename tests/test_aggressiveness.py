"""
Unit tests for AggressivenessModel — volatility → aggressiveness mapping.
"""

from __future__ import annotations

import pytest

from quant.aggressiveness import AggressivenessModel


class TestCompute:
    def test_below_low_threshold_returns_1(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        # vol <= low_threshold (0.001) → agg = 1.0
        assert model.compute(0.0) == pytest.approx(1.0)
        assert model.compute(0.001) == pytest.approx(1.0)
        assert model.compute(0.0005) == pytest.approx(1.0)

    def test_above_high_threshold_returns_0(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        # vol >= high_threshold (0.003) → agg = 0.0
        assert model.compute(0.003) == pytest.approx(0.0)
        assert model.compute(0.01) == pytest.approx(0.0)
        assert model.compute(1.0) == pytest.approx(0.0)

    def test_midpoint_returns_intermediate(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        # vol at midpoint (0.002) should return something in (0, 1)
        agg = model.compute(0.002)
        assert 0.0 < agg < 1.0

    def test_result_always_in_0_1(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        for vol in [-0.1, 0.0, 0.0005, 0.001, 0.002, 0.003, 0.005, 1.0]:
            agg = model.compute(vol)
            assert 0.0 <= agg <= 1.0, f"agg={agg} out of [0,1] at vol={vol}"

    def test_monotonically_decreasing(self, volatility_config):
        """Higher volatility → lower aggressiveness."""
        model = AggressivenessModel(volatility_config)
        vols = [0.001, 0.0015, 0.002, 0.0025, 0.003]
        aggs = [model.compute(v) for v in vols]
        for i in range(len(aggs) - 1):
            assert aggs[i] >= aggs[i + 1], f"agg not monotone at vol={vols[i+1]}"

    def test_power_curve_shape(self, volatility_config):
        """With power=2, the decay should be quadratic (concave)."""
        model = AggressivenessModel(volatility_config)
        # At 25% through the range, agg should be > 0.75 (above linear)
        vol_25pct = volatility_config.low_threshold + 0.25 * (volatility_config.high_threshold - volatility_config.low_threshold)
        agg = model.compute(vol_25pct)
        linear_expected = 1.0 - 0.25
        assert agg > linear_expected, f"Power=2 should give concave (faster) decay than linear"


class TestComputeWithRegime:
    def test_choppy_regime_symmetric(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        buy_agg, sell_agg = model.compute_with_regime(0.002, "choppy")
        assert buy_agg == pytest.approx(sell_agg)

    def test_unknown_regime_symmetric(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        buy_agg, sell_agg = model.compute_with_regime(0.002, "unknown")
        assert buy_agg == pytest.approx(sell_agg)

    def test_trending_up_sell_more_aggressive(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        buy_agg, sell_agg = model.compute_with_regime(0.002, "trending_up")
        assert sell_agg > buy_agg

    def test_trending_down_buy_more_aggressive(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        buy_agg, sell_agg = model.compute_with_regime(0.002, "trending_down")
        assert buy_agg > sell_agg

    def test_regime_results_in_0_1(self, volatility_config):
        model = AggressivenessModel(volatility_config)
        for regime in ["choppy", "trending_up", "trending_down"]:
            for vol in [0.0, 0.002, 0.005]:
                b, s = model.compute_with_regime(vol, regime)
                assert 0.0 <= b <= 1.0
                assert 0.0 <= s <= 1.0
