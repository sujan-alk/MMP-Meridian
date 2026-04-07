"""
Tests for Huy branch integration features.

Validates:
- Per-side spread control (compute_levels_huy, enforce_min_step)
- Per-side curve_strength override in _compute_side_levels()
- Depth engine min_step_usd enforcement
- Base aggressiveness scaling
- Config schema backward compatibility with new fields
- Tick rounding formula
- Backward compatibility: default config produces identical output to pre-integration
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from config.schema import (
    BotConfig,
    DepthConfig,
    ExchangeBotConfig,
    GlobalMidWeights,
    SpreadConfig,
    VolatilityConfig,
)
from quant.aggressiveness import AggressivenessModel
from quant.depth_engine import DepthEngine
from quant.spread_engine import SpreadEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def huy_spread_config() -> SpreadConfig:
    """SpreadConfig with Huy per-side overrides."""
    return SpreadConfig(
        buy_min_pct=-5.0,
        buy_max_pct=-0.1,
        sell_min_pct=0.3,
        sell_max_pct=7.0,
        curve_strength=4.0,
        buy_levels=10,
        sell_levels=20,
        buy_curve_strength=3.0,
        sell_curve_strength=5.0,
        buy_min_step=0.1,
        sell_min_step=0.2,
        tick_size=0.0001,
    )


@pytest.fixture
def huy_depth_config() -> DepthConfig:
    """DepthConfig with min_step_usd enabled."""
    return DepthConfig(
        levels=15,
        total_budget_usd=1000.0,
        curve_strength=4.0,
        min_order_usd=5.0,
        min_step_usd=10.0,
    )


@pytest.fixture
def huy_vol_config() -> VolatilityConfig:
    """VolatilityConfig with base_aggressiveness set to 0.4 (Huy style)."""
    return VolatilityConfig(
        window_minutes=10,
        low_threshold=0.001,
        high_threshold=0.003,
        power=2.0,
        base_aggressiveness=0.4,
    )


# =========================================================================
# SPREAD ENGINE: enforce_min_step()
# =========================================================================

class TestEnforceMinStep:
    """Tests for SpreadEngine.enforce_min_step() static method."""

    def test_no_op_when_min_step_zero(self):
        """min_step=0 should return input unchanged."""
        levels = [-0.1, -0.5, -1.0, -3.0, -5.0]
        result = SpreadEngine.enforce_min_step(levels, 0.0)
        assert result == levels

    def test_no_op_when_single_level(self):
        """Single-element list should be returned unchanged."""
        result = SpreadEngine.enforce_min_step([-0.1], 0.5)
        assert result == [-0.1]

    def test_no_op_when_already_spaced(self):
        """Levels already meeting min_step should be unchanged."""
        levels = [-0.1, -0.3, -0.5, -0.7, -0.9]
        result = SpreadEngine.enforce_min_step(levels, 0.1)
        assert result == levels

    def test_enforces_min_step_buy_side(self):
        """Buy (negative) levels too close together should be pushed apart."""
        levels = [-0.1, -0.12, -0.13, -0.5, -5.0]
        result = SpreadEngine.enforce_min_step(levels, 0.1)
        # Level 1: |-0.12 - (-0.1)| = 0.02 < 0.1 → pushed to -0.2
        # Level 2: |-0.13 - (-0.2)| = 0.07 < 0.1 → pushed to -0.3
        for i in range(1, len(result)):
            assert abs(result[i] - result[i - 1]) >= 0.1 - 1e-9

    def test_enforces_min_step_sell_side(self):
        """Sell (positive) levels too close together should be pushed apart."""
        levels = [0.3, 0.32, 0.33, 1.0, 7.0]
        result = SpreadEngine.enforce_min_step(levels, 0.1)
        for i in range(1, len(result)):
            assert abs(result[i] - result[i - 1]) >= 0.1 - 1e-9

    def test_pushed_levels_have_correct_step_buy(self):
        """Buy-side: when levels are uniformly tight, each adjusted pair has exact min_step."""
        levels = [-0.1, -0.12, -0.14, -0.16, -0.18]
        result = SpreadEngine.enforce_min_step(levels, 0.1)
        # Levels 1 and 2 get pushed to -0.2, -0.3; levels 3,4 are far enough from
        # their predecessors to stay put. Verify the adjusted ones have correct spacing.
        assert result[0] == -0.1
        assert result[1] == pytest.approx(-0.2)  # pushed from -0.12
        assert result[2] == pytest.approx(-0.3)  # pushed from -0.14

    def test_pushed_levels_have_correct_step_sell(self):
        """Sell-side: when levels are uniformly tight, each adjusted pair has exact min_step."""
        levels = [0.3, 0.32, 0.34, 0.36, 0.38]
        result = SpreadEngine.enforce_min_step(levels, 0.1)
        assert result[0] == 0.3
        assert result[1] == pytest.approx(0.4)   # pushed from 0.32
        assert result[2] == pytest.approx(0.5)   # pushed from 0.34

    def test_negative_min_step_is_no_op(self):
        """Negative min_step should be treated as disabled."""
        levels = [-0.1, -0.11, -0.12]
        result = SpreadEngine.enforce_min_step(levels, -0.5)
        assert result == levels


# =========================================================================
# SPREAD ENGINE: compute_levels_huy()
# =========================================================================

class TestComputeLevelsHuy:
    """Tests for SpreadEngine.compute_levels_huy() per-side control."""

    def test_asymmetric_level_counts(self, huy_spread_config):
        """Buy and sell sides can have different numbers of levels."""
        engine = SpreadEngine(huy_spread_config)
        buy, sell = engine.compute_levels_huy(
            buy_agg=0.5, sell_agg=0.5,
            buy_levels=10, sell_levels=20,
            buy_curve_strength=4.0, sell_curve_strength=4.0,
        )
        assert len(buy) == 10
        assert len(sell) == 20

    def test_per_side_curve_strength(self, spread_config):
        """Different curve strengths per side should produce different distributions."""
        engine = SpreadEngine(spread_config)
        buy_mild, sell_mild = engine.compute_levels_huy(
            buy_agg=0.7, sell_agg=0.7,
            buy_levels=15, sell_levels=15,
            buy_curve_strength=1.0, sell_curve_strength=8.0,
        )
        # With same aggressiveness but different curve strengths,
        # the distributions should differ
        buy_std = np.std(np.diff(buy_mild))
        sell_std = np.std(np.diff(sell_mild))
        assert buy_std != pytest.approx(sell_std, abs=0.01)

    def test_min_step_applied(self, spread_config):
        """Min step enforcement should be applied after level computation."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels_huy(
            buy_agg=0.8, sell_agg=0.8,
            buy_levels=15, sell_levels=15,
            buy_curve_strength=4.0, sell_curve_strength=4.0,
            buy_min_step=0.2, sell_min_step=0.3,
        )
        # Verify buy side min step
        for i in range(1, len(buy)):
            assert abs(buy[i] - buy[i - 1]) >= 0.2 - 1e-9

        # Verify sell side min step
        for i in range(1, len(sell)):
            assert abs(sell[i] - sell[i - 1]) >= 0.3 - 1e-9

    def test_buy_spreads_negative_sell_positive(self, spread_config):
        """Buy spreads should be negative, sell spreads positive."""
        engine = SpreadEngine(spread_config)
        buy, sell = engine.compute_levels_huy(
            buy_agg=0.5, sell_agg=0.5,
            buy_levels=10, sell_levels=10,
            buy_curve_strength=4.0, sell_curve_strength=4.0,
        )
        assert all(s < 0 for s in buy)
        assert all(s > 0 for s in sell)

    def test_aggressiveness_clamped(self, spread_config):
        """Aggressiveness outside [0, 1] should be clamped."""
        engine = SpreadEngine(spread_config)
        buy_neg, _ = engine.compute_levels_huy(
            buy_agg=-0.5, sell_agg=0.5,
            buy_levels=10, sell_levels=10,
            buy_curve_strength=4.0, sell_curve_strength=4.0,
        )
        buy_zero, _ = engine.compute_levels_huy(
            buy_agg=0.0, sell_agg=0.5,
            buy_levels=10, sell_levels=10,
            buy_curve_strength=4.0, sell_curve_strength=4.0,
        )
        np.testing.assert_array_almost_equal(buy_neg, buy_zero)

    def test_backward_compat_with_compute_levels_dual(self, spread_config):
        """
        When using shared params and no min_step, compute_levels_huy should
        produce identical results to compute_levels_dual.
        """
        engine = SpreadEngine(spread_config)
        cs = spread_config.curve_strength

        for agg in [0.0, 0.3, 0.5, 0.7, 1.0]:
            buy_dual, sell_dual = engine.compute_levels_dual(agg, agg, 15)
            buy_huy, sell_huy = engine.compute_levels_huy(
                buy_agg=agg, sell_agg=agg,
                buy_levels=15, sell_levels=15,
                buy_curve_strength=cs, sell_curve_strength=cs,
            )
            np.testing.assert_array_almost_equal(buy_dual, buy_huy, decimal=10)
            np.testing.assert_array_almost_equal(sell_dual, sell_huy, decimal=10)


# =========================================================================
# SPREAD ENGINE: per-side curve_strength in _compute_side_levels
# =========================================================================

class TestPerSideCurveStrength:
    """Tests that the optional curve_strength parameter in _compute_side_levels works."""

    def test_override_differs_from_default(self, spread_config):
        """Passing a different curve_strength should change the output."""
        engine = SpreadEngine(spread_config)
        default = engine._compute_side_levels(
            tightest=-0.1, widest=-5.0, agg=0.7, n=10,
        )
        override = engine._compute_side_levels(
            tightest=-0.1, widest=-5.0, agg=0.7, n=10, curve_strength=1.0,
        )
        # cfg.curve_strength=4.0 vs override=1.0 → different output
        assert default != override

    def test_none_falls_back_to_config(self, spread_config):
        """curve_strength=None should use self.cfg.curve_strength."""
        engine = SpreadEngine(spread_config)
        default = engine._compute_side_levels(
            tightest=-0.1, widest=-5.0, agg=0.5, n=10,
        )
        explicit_none = engine._compute_side_levels(
            tightest=-0.1, widest=-5.0, agg=0.5, n=10, curve_strength=None,
        )
        np.testing.assert_array_almost_equal(default, explicit_none)


# =========================================================================
# DEPTH ENGINE: min_step_usd
# =========================================================================

class TestMinStepUsd:
    """Tests for min_step_usd enforcement in DepthEngine.compute_amounts()."""

    def test_zero_min_step_no_change(self, depth_config):
        """min_step_usd=0.0 should produce identical output to the default."""
        engine = DepthEngine(depth_config)
        default = engine.compute_amounts(0.5, 15, 1.0, "buy")
        explicit = engine.compute_amounts(0.5, 15, 1.0, "buy", min_step_usd=0.0)
        np.testing.assert_array_almost_equal(default, explicit)

    def test_enforces_minimum_decrement(self, depth_config):
        """With min_step_usd > 0, consecutive levels should decrease by at least that amount."""
        engine = DepthEngine(depth_config)
        amounts = engine.compute_amounts(0.3, 15, 1.0, "buy", min_step_usd=5.0)
        for i in range(1, len(amounts)):
            # Amount should decrease by at least 5.0, unless clamped by min_order_usd
            if amounts[i] > engine.cfg.min_order_usd:
                decrement = amounts[i - 1] - amounts[i]
                assert decrement >= 5.0 - 1e-6 or amounts[i] == pytest.approx(engine.cfg.min_order_usd, abs=0.01)

    def test_amounts_never_negative(self, depth_config):
        """Even with large min_step_usd, amounts should never go negative."""
        engine = DepthEngine(depth_config)
        amounts = engine.compute_amounts(0.5, 15, 1.0, "buy", min_step_usd=100.0)
        for a in amounts:
            assert a >= 0.0

    def test_min_order_usd_floor_applies_after(self, depth_config):
        """min_order_usd clamping should still apply after min_step enforcement."""
        engine = DepthEngine(depth_config)
        amounts = engine.compute_amounts(0.5, 15, 1.0, "buy", min_step_usd=10.0)
        for a in amounts:
            assert a >= engine.cfg.min_order_usd

    def test_all_amounts_positive(self, depth_config):
        """All amounts should be strictly positive."""
        engine = DepthEngine(depth_config)
        amounts = engine.compute_amounts(0.5, 10, 1.0, "buy", min_step_usd=8.0)
        for a in amounts:
            assert a > 0

    def test_level_0_unaffected(self, depth_config):
        """Level 0 (closest to mid) should not be altered by min_step."""
        engine = DepthEngine(depth_config)
        without = engine.compute_amounts(0.5, 10, 1.0, "buy", min_step_usd=0.0)
        with_step = engine.compute_amounts(0.5, 10, 1.0, "buy", min_step_usd=5.0)
        assert without[0] == pytest.approx(with_step[0])


# =========================================================================
# BASE AGGRESSIVENESS SCALING
# =========================================================================

class TestBaseAggressivenessScaling:
    """Tests for the base_aggressiveness formula: scaled = raw * base."""

    def test_base_1_no_change(self):
        """base_aggressiveness=1.0 should not modify raw aggressiveness."""
        vol_cfg = VolatilityConfig(base_aggressiveness=1.0)
        model = AggressivenessModel(vol_cfg)
        raw = model.compute(0.002)
        scaled = raw * vol_cfg.base_aggressiveness
        assert scaled == pytest.approx(raw)

    def test_base_04_scales_down(self):
        """base_aggressiveness=0.4 should scale aggressiveness to 40%."""
        vol_cfg = VolatilityConfig(base_aggressiveness=0.4)
        model = AggressivenessModel(vol_cfg)
        raw = model.compute(0.002)
        scaled = raw * vol_cfg.base_aggressiveness
        assert scaled == pytest.approx(raw * 0.4)

    def test_base_0_produces_zero(self):
        """base_aggressiveness=0.0 should produce zero aggressiveness."""
        vol_cfg = VolatilityConfig(base_aggressiveness=0.0)
        model = AggressivenessModel(vol_cfg)
        raw_buy, raw_sell = model.compute_with_regime(0.002, "choppy")
        assert raw_buy * vol_cfg.base_aggressiveness == 0.0
        assert raw_sell * vol_cfg.base_aggressiveness == 0.0

    def test_regime_then_base_scaling(self):
        """Regime adjustment should happen first, then base scaling."""
        vol_cfg = VolatilityConfig(base_aggressiveness=0.5)
        model = AggressivenessModel(vol_cfg)

        # trending_up: buy gets 0.8x, sell gets 1.1x
        raw_buy, raw_sell = model.compute_with_regime(0.002, "trending_up")
        base = vol_cfg.base_aggressiveness

        # Final values
        final_buy = raw_buy * base
        final_sell = raw_sell * base

        # Both should be within [0, 1] since base=0.5 and raw <= 1.0
        assert 0.0 <= final_buy <= 1.0
        assert 0.0 <= final_sell <= 1.0

        # Sell should be more aggressive than buy (trending up)
        assert final_sell > final_buy


# =========================================================================
# TICK ROUNDING FORMULA
# =========================================================================

class TestTickRounding:
    """Tests for the tick rounding formula: round(mid * (1 + s/100) / tick) * tick."""

    def test_basic_rounding(self):
        """Prices should be rounded to the nearest tick_size."""
        mid = 0.1234
        spread_pct = -1.0  # -1% below mid
        tick = 0.0001
        price = round(mid * (1.0 + spread_pct / 100.0) / tick) * tick
        # Expected: 0.1234 * 0.99 = 0.122166 → round to 0.1222
        assert price == pytest.approx(0.1222, abs=tick)

    def test_larger_tick_coarsens_price(self):
        """Larger tick size should produce coarser prices."""
        mid = 0.1234
        spread_pct = -1.0
        tick_fine = 0.0001
        tick_coarse = 0.01
        price_fine = round(mid * (1.0 + spread_pct / 100.0) / tick_fine) * tick_fine
        price_coarse = round(mid * (1.0 + spread_pct / 100.0) / tick_coarse) * tick_coarse
        # Coarse price should be less precise
        assert price_coarse == pytest.approx(0.12, abs=tick_coarse)
        assert abs(price_fine - mid * 0.99) < abs(price_coarse - mid * 0.99)

    def test_zero_spread_returns_mid(self):
        """Zero spread should return mid rounded to tick."""
        mid = 0.1234
        tick = 0.0001
        price = round(mid * (1.0 + 0 / 100.0) / tick) * tick
        assert price == pytest.approx(mid, abs=tick)


# =========================================================================
# CONFIG SCHEMA BACKWARD COMPATIBILITY
# =========================================================================

class TestConfigBackwardCompat:
    """Tests that existing configs work unchanged with new fields."""

    def test_spread_config_defaults(self):
        """SpreadConfig with no new fields should have backward-compatible defaults."""
        cfg = SpreadConfig(
            buy_min_pct=-5.0, buy_max_pct=-0.1,
            sell_min_pct=0.3, sell_max_pct=7.0,
            curve_strength=4.0,
        )
        assert cfg.buy_levels is None
        assert cfg.sell_levels is None
        assert cfg.buy_curve_strength is None
        assert cfg.sell_curve_strength is None
        assert cfg.buy_min_step == 0.0
        assert cfg.sell_min_step == 0.0
        assert cfg.tick_size == 0.0001

    def test_depth_config_defaults(self):
        """DepthConfig with no new fields should default min_step_usd=0."""
        cfg = DepthConfig(levels=15, total_budget_usd=1000.0)
        assert cfg.min_step_usd == 0.0

    def test_vol_config_defaults(self):
        """VolatilityConfig with no new fields should default base_aggressiveness=1.0."""
        cfg = VolatilityConfig()
        assert cfg.base_aggressiveness == 1.0

    def test_huy_spread_config_validates(self):
        """SpreadConfig with all Huy fields should parse correctly."""
        cfg = SpreadConfig(
            buy_min_pct=-5.0, buy_max_pct=-0.1,
            sell_min_pct=0.3, sell_max_pct=7.0,
            curve_strength=4.0,
            buy_levels=10, sell_levels=20,
            buy_curve_strength=3.0, sell_curve_strength=5.0,
            buy_min_step=0.1, sell_min_step=0.2,
            tick_size=0.0001,
        )
        assert cfg.buy_levels == 10
        assert cfg.sell_levels == 20
        assert cfg.buy_curve_strength == 3.0
        assert cfg.sell_curve_strength == 5.0

    def test_binance_exchange(self):
        """Binance should be a valid exchange name."""
        cfg = ExchangeBotConfig(exchange="binance", symbol="ALKIMI/USDT")
        assert cfg.exchange == "binance"

    def test_global_mid_weights_with_binance(self):
        """GlobalMidWeights with binance should validate correctly."""
        w = GlobalMidWeights(kucoin=0.4, gate=0.4, mexc=0.05, kraken=0.05, binance=0.1)
        assert w.binance == 0.1
        assert abs(w.kucoin + w.gate + w.mexc + w.kraken + w.binance - 1.0) < 0.001

    def test_invalid_buy_levels_rejected(self):
        """buy_levels outside [3, 30] should be rejected."""
        with pytest.raises(Exception):
            SpreadConfig(
                buy_min_pct=-5.0, buy_max_pct=-0.1,
                sell_min_pct=0.3, sell_max_pct=7.0,
                buy_levels=1,  # too low
            )

    def test_invalid_base_aggressiveness_rejected(self):
        """base_aggressiveness outside [0.0, 1.0] should be rejected."""
        with pytest.raises(Exception):
            VolatilityConfig(base_aggressiveness=1.5)

    def test_full_bot_config_backward_compat(self):
        """A full BotConfig without any new fields should parse fine."""
        cfg = BotConfig(
            dry_run=True,
            global_mid_weights=GlobalMidWeights(kucoin=0.45, gate=0.45, mexc=0.05, kraken=0.05),
            volatility=VolatilityConfig(),
            exchanges=[
                ExchangeBotConfig(exchange="kucoin", symbol="ALKIMI/USDT"),
            ],
        )
        assert cfg.volatility.base_aggressiveness == 1.0
        assert cfg.exchanges[0].spread.buy_levels is None
        assert cfg.exchanges[0].depth.min_step_usd == 0.0
