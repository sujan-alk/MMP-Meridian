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


class TestComputeWithHMMRegime:
    def test_crash_buy_agg_floored(self, volatility_config):
        """In HIGH_VOL_CRASH, buy aggressiveness must be >= crash_buy_agg_floor."""
        model = AggressivenessModel(volatility_config)
        floor = volatility_config.hmm.crash_buy_agg_floor
        # Use very high vol (which normally drives agg to 0.0)
        buy_agg, _ = model.compute_with_hmm_regime(0.01, "choppy", "HIGH_VOL_CRASH", 0.9)
        assert buy_agg >= floor, f"buy_agg={buy_agg} should be >= floor={floor}"

    def test_crash_sell_agg_capped(self, volatility_config):
        """In HIGH_VOL_CRASH, sell aggressiveness must be <= crash_sell_agg_ceiling."""
        model = AggressivenessModel(volatility_config)
        ceiling = volatility_config.hmm.crash_sell_agg_ceiling
        # Use low vol (which normally gives agg=1.0)
        _, sell_agg = model.compute_with_hmm_regime(0.0005, "choppy", "HIGH_VOL_CRASH", 0.9)
        assert sell_agg <= ceiling, f"sell_agg={sell_agg} should be <= ceiling={ceiling}"

    def test_crash_buy_stays_active_at_extreme_vol(self, volatility_config):
        """Even at extreme volatility, crash regime keeps buy-side active."""
        model = AggressivenessModel(volatility_config)
        buy_agg, _ = model.compute_with_hmm_regime(0.1, "trending_down", "HIGH_VOL_CRASH", 1.0)
        assert buy_agg >= volatility_config.hmm.crash_buy_agg_floor

    def test_normal_hmm_passes_through(self, volatility_config):
        """NORMAL HMM regime should pass through ZZ-regime values unchanged."""
        model = AggressivenessModel(volatility_config)
        zz_buy, zz_sell = model.compute_with_regime(0.002, "trending_up")
        hmm_buy, hmm_sell = model.compute_with_hmm_regime(0.002, "trending_up", "NORMAL", 0.9)
        assert hmm_buy == pytest.approx(zz_buy)
        assert hmm_sell == pytest.approx(zz_sell)

    def test_low_confidence_blends_toward_zz(self, volatility_config):
        """At low HMM confidence, adjustments should blend back toward ZZ values."""
        model = AggressivenessModel(volatility_config)
        zz_buy, zz_sell = model.compute_with_regime(0.002, "choppy")
        # Full confidence
        full_buy, full_sell = model.compute_with_hmm_regime(0.002, "choppy", "HIGH_VOL", 0.9)
        # Very low confidence — should be closer to ZZ values
        low_buy, low_sell = model.compute_with_hmm_regime(0.002, "choppy", "HIGH_VOL", 0.1)
        assert abs(low_buy - zz_buy) < abs(full_buy - zz_buy) or full_buy == pytest.approx(zz_buy)

    def test_recovery_boosts_buying(self, volatility_config):
        """RECOVERY regime should boost buy aggressiveness."""
        model = AggressivenessModel(volatility_config)
        zz_buy, _ = model.compute_with_regime(0.002, "choppy")
        rec_buy, _ = model.compute_with_hmm_regime(0.002, "choppy", "RECOVERY", 0.9)
        assert rec_buy >= zz_buy

    def test_recovery_dampens_selling(self, volatility_config):
        """RECOVERY regime should dampen sell aggressiveness."""
        model = AggressivenessModel(volatility_config)
        _, zz_sell = model.compute_with_regime(0.002, "choppy")
        _, rec_sell = model.compute_with_hmm_regime(0.002, "choppy", "RECOVERY", 0.9)
        assert rec_sell <= zz_sell

    def test_all_hmm_regimes_return_valid_range(self, volatility_config):
        """All HMM regime adjustments must produce values in [0.0, 1.0]."""
        model = AggressivenessModel(volatility_config)
        for hmm_regime in ["LOW_VOL", "NORMAL", "HIGH_VOL", "HIGH_VOL_CRASH", "RECOVERY"]:
            for vol in [0.0, 0.002, 0.005, 0.01]:
                for confidence in [0.1, 0.5, 0.9]:
                    b, s = model.compute_with_hmm_regime(vol, "choppy", hmm_regime, confidence)
                    assert 0.0 <= b <= 1.0, f"buy_agg={b} out of range for {hmm_regime}"
                    assert 0.0 <= s <= 1.0, f"sell_agg={s} out of range for {hmm_regime}"
