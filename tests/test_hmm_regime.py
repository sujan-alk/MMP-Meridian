"""
Unit tests for HMMRegimeDetector — Baum-Welch HMM regime classification.
"""

from __future__ import annotations

import pytest

from config.schema import HMMConfig
from quant.hmm_regime import HMMRegimeDetector, HMM_REGIME_NAMES


@pytest.fixture
def hmm_config() -> HMMConfig:
    return HMMConfig(
        enabled=True,
        hmm_lookback=30,
        hmm_min_observations=5,  # lower for testing
        refit_interval=100,
    )


@pytest.fixture
def detector(hmm_config) -> HMMRegimeDetector:
    return HMMRegimeDetector(hmm_config)


class TestInitialization:
    def test_starts_as_normal(self, detector):
        assert detector.regime == "NORMAL"

    def test_not_ready_initially(self, detector):
        assert not detector.is_ready()

    def test_confidence_starts_at_zero(self, detector):
        assert detector.confidence == 0.0


class TestIsReady:
    def test_not_ready_below_min_observations(self, detector):
        for _ in range(4):
            detector.update(0.001, 0.0, 0.0, 0.0)
        assert not detector.is_ready()

    def test_ready_at_min_observations(self, detector):
        for _ in range(5):
            detector.update(0.001, 0.0, 0.0, 0.0)
        assert detector.is_ready()


class TestRegimeDetection:
    def test_calm_market_produces_low_vol_or_normal(self, detector):
        """Feeding calm observations should produce LOW_VOL or NORMAL."""
        for _ in range(10):
            regime = detector.update(0.0004, 0.0, 0.0, 0.0)
        assert regime in ("LOW_VOL", "NORMAL")

    def test_crash_observations_produce_crash(self, detector):
        """High vol + strong negative direction + volume spike → HIGH_VOL_CRASH."""
        # Warm up with some normal observations
        for _ in range(5):
            detector.update(0.001, 0.0, 0.1, 0.0)
        # Now feed crash-like observations
        for _ in range(10):
            regime = detector.update(0.01, -0.008, 1.5, -0.01)
        assert regime == "HIGH_VOL_CRASH"

    def test_recovery_after_crash(self, detector):
        """After crash, positive direction should produce RECOVERY."""
        # Establish crash
        for _ in range(5):
            detector.update(0.001, 0.0, 0.1, 0.0)
        for _ in range(8):
            detector.update(0.01, -0.008, 1.5, -0.01)

        # Now feed recovery-like observations
        for _ in range(8):
            regime = detector.update(0.004, 0.003, 0.5, 0.004)
        assert regime in ("RECOVERY", "NORMAL")

    def test_high_vol_detection(self, detector):
        """Elevated volatility without crash signals → HIGH_VOL."""
        for _ in range(5):
            detector.update(0.001, 0.0, 0.1, 0.0)
        for _ in range(10):
            regime = detector.update(0.005, -0.0005, 0.3, -0.001)
        assert regime in ("HIGH_VOL", "HIGH_VOL_CRASH")

    def test_regime_is_valid_string(self, detector):
        """All returned regimes should be valid regime names."""
        valid = set(HMM_REGIME_NAMES.values())
        for _ in range(20):
            regime = detector.update(0.002, 0.001, 0.2, 0.001)
            assert regime in valid, f"Invalid regime: {regime}"


class TestConfidence:
    def test_confidence_in_range(self, detector):
        """Confidence should always be in [0.0, 1.0]."""
        for _ in range(15):
            detector.update(0.002, 0.0, 0.1, 0.0)
        assert 0.0 <= detector.confidence <= 1.0

    def test_confidence_increases_with_consistent_data(self, detector):
        """Feeding consistent crash data should produce reasonable confidence."""
        for _ in range(5):
            detector.update(0.001, 0.0, 0.1, 0.0)
        for _ in range(10):
            detector.update(0.01, -0.008, 1.5, -0.01)
        assert detector.confidence > 0.3


class TestEdgeCases:
    def test_zero_features(self, detector):
        """All-zero features should not crash."""
        for _ in range(10):
            regime = detector.update(0.0, 0.0, 0.0, 0.0)
        assert regime in set(HMM_REGIME_NAMES.values())

    def test_extreme_values(self, detector):
        """Very large feature values should not crash."""
        for _ in range(10):
            regime = detector.update(1.0, 0.5, 100.0, 0.5)
        assert regime in set(HMM_REGIME_NAMES.values())

    def test_single_observation_returns_normal(self, detector):
        """Before ready, should return NORMAL."""
        regime = detector.update(0.01, -0.005, 0.8, -0.008)
        assert regime == "NORMAL"  # not ready yet

    def test_negative_volume_change(self, detector):
        """Negative volume change (volume dropping) should be handled."""
        for _ in range(10):
            regime = detector.update(0.001, 0.0, -0.5, 0.0)
        assert regime in set(HMM_REGIME_NAMES.values())
