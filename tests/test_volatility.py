"""
Tests for quant/volatility.py — VolatilityEngine.

Validates:
- Simple rolling volatility computation
- Zhang-Zhang estimator formula
- Regime detection (trending_up, trending_down, choppy)
- Edge cases: insufficient data, degenerate candles, zero prices
- Readiness checks
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from config.schema import VolatilityConfig
from exchange.base import Candle
from quant.volatility import VolatilityEngine
from tests.conftest import make_candle, make_candles


@pytest.fixture
def vol_engine(volatility_config) -> VolatilityEngine:
    return VolatilityEngine(volatility_config)


class TestRollingVol:
    """Tests for the simple rolling std-dev volatility."""

    def test_insufficient_data_returns_zero(self, vol_engine):
        """With < 3 price observations, rolling_vol should return 0.0."""
        vol_engine.update_price(0.10)
        vol_engine.update_price(0.11)
        assert vol_engine.rolling_vol() == 0.0

    def test_constant_prices_zero_vol(self, vol_engine):
        """If all prices are identical, volatility should be 0."""
        for _ in range(20):
            vol_engine.update_price(0.10)
        assert vol_engine.rolling_vol() == pytest.approx(0.0, abs=1e-10)

    def test_increasing_prices_positive_vol(self, vol_engine):
        """Trending prices should produce non-zero volatility."""
        for i in range(20):
            vol_engine.update_price(0.10 + i * 0.001)
        vol = vol_engine.rolling_vol()
        assert vol > 0.0

    def test_volatile_prices_higher_vol(self, vol_engine):
        """More volatile prices should produce higher volatility."""
        # Low volatility: small oscillations
        engine_low = VolatilityEngine(VolatilityConfig())
        for i in range(50):
            engine_low.update_price(0.10 + (i % 2) * 0.0001)

        # High volatility: large oscillations
        engine_high = VolatilityEngine(VolatilityConfig())
        for i in range(50):
            engine_high.update_price(0.10 + (i % 2) * 0.01)

        assert engine_high.rolling_vol() > engine_low.rolling_vol()

    def test_rolling_vol_is_std_of_returns(self, vol_engine):
        """Verify the computation matches numpy std of percentage returns."""
        prices = [0.10, 0.105, 0.103, 0.108, 0.107, 0.110, 0.112]
        for p in prices:
            vol_engine.update_price(p)

        arr = np.array(prices)
        returns = np.diff(arr) / arr[:-1]
        expected = float(np.std(returns))

        assert vol_engine.rolling_vol() == pytest.approx(expected, rel=1e-6)


class TestZhangZhangVol:
    """Tests for the Zhang-Zhang range-based volatility estimator."""

    def test_insufficient_candles_returns_zero(self, vol_engine):
        """With < 3 candles, should return (0.0, "choppy")."""
        vol_engine.update_candle(make_candle())
        vol_engine.update_candle(make_candle())
        zz_vol, regime = vol_engine.zhang_zhang_vol()
        assert zz_vol == 0.0
        assert regime == "choppy"

    def test_returns_non_negative_vol(self, vol_engine):
        """Zhang-Zhang vol should always be >= 0."""
        for c in make_candles(10):
            vol_engine.update_candle(c)
        zz_vol, _ = vol_engine.zhang_zhang_vol()
        assert zz_vol >= 0.0

    def test_formula_manually(self, vol_engine):
        """Verify the ZZ formula against manual calculation."""
        candles = [
            Candle(timestamp=1.0, open=100.0, high=110.0, low=95.0, close=105.0, volume=1000),
            Candle(timestamp=2.0, open=105.0, high=115.0, low=100.0, close=108.0, volume=1000),
            Candle(timestamp=3.0, open=108.0, high=112.0, low=102.0, close=110.0, volume=1000),
        ]
        for c in candles:
            vol_engine.update_candle(c)

        zz_vol, regime = vol_engine.zhang_zhang_vol()

        # Manual computation
        _2ln2_minus_1 = 2.0 * math.log(2) - 1.0
        terms = []
        for c in candles:
            hl = 0.5 * (math.log(c.high / c.low)) ** 2
            co = _2ln2_minus_1 * (math.log(c.close / c.open)) ** 2
            terms.append(hl - co)
        expected_var = np.mean(terms)
        expected_vol = math.sqrt(max(expected_var, 0.0))

        assert zz_vol == pytest.approx(expected_vol, rel=1e-6)

    def test_degenerate_candle_high_equals_low(self, vol_engine):
        """Candle where high == low should contribute 0 to hl term."""
        candles = [
            Candle(timestamp=1.0, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
            Candle(timestamp=2.0, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
            Candle(timestamp=3.0, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        ]
        for c in candles:
            vol_engine.update_candle(c)
        zz_vol, regime = vol_engine.zhang_zhang_vol()
        assert zz_vol == pytest.approx(0.0, abs=1e-10)
        assert regime == "choppy"

    def test_zero_price_candle_skipped(self, vol_engine):
        """Candles with zero prices should be skipped."""
        candles = [
            Candle(timestamp=1.0, open=0.0, high=0.0, low=0.0, close=0.0, volume=0),
            Candle(timestamp=2.0, open=0.0, high=0.0, low=0.0, close=0.0, volume=0),
            Candle(timestamp=3.0, open=0.0, high=0.0, low=0.0, close=0.0, volume=0),
        ]
        for c in candles:
            vol_engine.update_candle(c)
        zz_vol, regime = vol_engine.zhang_zhang_vol()
        assert zz_vol == 0.0
        assert regime == "choppy"


class TestRegimeDetection:
    """Tests for trending_up / trending_down / choppy classification."""

    def test_trending_up(self, vol_engine):
        """Consistently rising candles should be classified as trending_up."""
        for i in range(10):
            base = 100.0 + i * 2.0  # Strong uptrend
            vol_engine.update_candle(Candle(
                timestamp=float(i), open=base, high=base + 3, low=base - 1, close=base + 2, volume=1000
            ))
        _, regime = vol_engine.zhang_zhang_vol()
        assert regime == "trending_up"

    def test_trending_down(self, vol_engine):
        """Consistently falling candles should be classified as trending_down."""
        for i in range(10):
            base = 100.0 - i * 2.0  # Strong downtrend
            vol_engine.update_candle(Candle(
                timestamp=float(i), open=base, high=base + 1, low=base - 3, close=base - 2, volume=1000
            ))
        _, regime = vol_engine.zhang_zhang_vol()
        assert regime == "trending_down"

    def test_choppy_sideways(self, vol_engine):
        """Alternating up/down candles should be classified as choppy."""
        for i in range(10):
            base = 100.0
            if i % 2 == 0:
                vol_engine.update_candle(Candle(
                    timestamp=float(i), open=base, high=base + 1, low=base - 0.5, close=base + 0.5, volume=1000
                ))
            else:
                vol_engine.update_candle(Candle(
                    timestamp=float(i), open=base + 0.5, high=base + 1, low=base - 0.5, close=base, volume=1000
                ))
        _, regime = vol_engine.zhang_zhang_vol()
        assert regime == "choppy"


class TestReadiness:
    """Tests for data sufficiency checks."""

    def test_not_ready_initially(self, vol_engine):
        """Engine should not be ready with no data."""
        assert vol_engine.is_ready() is False

    def test_ready_after_10_prices(self, vol_engine):
        """Engine should be ready after 10 price updates."""
        for i in range(10):
            vol_engine.update_price(0.10 + i * 0.001)
        assert vol_engine.is_ready() is True

    def test_candles_not_ready_initially(self, vol_engine):
        """Candles should not be ready with no data."""
        assert vol_engine.candles_ready() is False

    def test_candles_ready_after_3(self, vol_engine):
        """Candles should be ready after 3 candle updates."""
        for c in make_candles(3):
            vol_engine.update_candle(c)
        assert vol_engine.candles_ready() is True

    def test_sample_count(self, vol_engine):
        """sample_count should track number of price observations."""
        for i in range(5):
            vol_engine.update_price(0.10)
        assert vol_engine.sample_count == 5

    def test_candle_count(self, vol_engine):
        """candle_count should track number of candle observations."""
        for c in make_candles(7):
            vol_engine.update_candle(c)
        assert vol_engine.candle_count == 7


class TestBulkCandleLoad:
    """Tests for bulk candle loading (warmup)."""

    def test_update_candles_bulk(self, vol_engine):
        """Bulk loading candles should work the same as individual updates."""
        candles = make_candles(10)
        vol_engine.update_candles(candles)
        assert vol_engine.candle_count == 10
        zz_vol, _ = vol_engine.zhang_zhang_vol()
        assert zz_vol >= 0.0
