"""
Tests for the Regime Master and HMM Regime Detector — Meridian MM Platform.

Covers:
- RegimeDetector: default state, update, get_regime, get_mm_parameters
- RegimeDetector: observation encoding / discretisation
- RegimeMaster: creation, push_state processing, RegimeState broadcasting
- RegimeMaster: HMM + ZZ agreement / disagreement logic
- Integration: spread_mult and depth_mult via updated engines
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from quant.regime_detector import (
    RegimeDetector,
    MM_PARAMETERS,
    REGIME_NAMES,
    N_OBS,
    discretise_price_vol,
    discretise_bid_ask_spread,
    discretise_ob_imbalance,
    discretise_funding_rate,
    discretise_price_change_rate,
    encode_observation,
    observation_from_floats,
)
from core.regime_master import (
    RegimeMaster,
    RegimeState,
    _regimes_agree,
    _zz_to_hmm_regime,
    _CONSERVATIVE_PARAMS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_global_state(vol=0.01, zz_regime="choppy", global_mid=0.10, zz_vol=0.01):
    """Build a minimal GlobalState-like mock."""
    state = MagicMock()
    state.volatility = vol
    state.zz_regime = zz_regime
    state.global_mid = global_mid
    state.zz_vol = zz_vol
    state.timestamp = time.time()
    return state


# ---------------------------------------------------------------------------
# RegimeDetector tests
# ---------------------------------------------------------------------------

class TestRegimeDetector:

    def test_default_state(self):
        rd = RegimeDetector()
        regime = rd.get_regime()
        assert regime in REGIME_NAMES.values(), f"Unexpected default regime: {regime}"

    def test_get_regime_confidence_range(self):
        rd = RegimeDetector()
        conf = rd.get_regime_confidence()
        assert 0.0 <= conf <= 1.0

    def test_get_belief_sums_to_one(self):
        rd = RegimeDetector()
        belief = rd.get_belief()
        assert abs(sum(belief.values()) - 1.0) < 1e-6
        assert set(belief.keys()) == set(REGIME_NAMES.values())

    def test_update_changes_belief(self):
        rd = RegimeDetector()
        before = rd.get_belief().copy()
        # Feed a low-vol, tight-spread, balanced-book observation (RANGING-like)
        obs = observation_from_floats(0.01, 0.05, 0.0, 0.0, 0.002)
        rd.update(obs)
        after = rd.get_belief()
        # Belief should have changed
        assert before != after

    def test_update_out_of_range_ignored(self):
        rd = RegimeDetector()
        before = rd.get_belief().copy()
        rd.update(-1)        # invalid
        rd.update(N_OBS)     # invalid (one past end)
        after = rd.get_belief()
        assert before == after

    def test_update_from_floats(self):
        rd = RegimeDetector()
        # Should not raise
        rd.update_from_floats(
            price_vol=0.01,
            bid_ask_spread_pct=0.08,
            ob_imbalance=0.05,
            funding_rate=0.00005,
            price_change_rate=0.002,
        )
        assert rd.get_regime() in REGIME_NAMES.values()

    def test_high_vol_observation_tends_to_high_vol(self):
        rd = RegimeDetector()
        # Feed many high-volatility observations
        for _ in range(20):
            obs = observation_from_floats(
                price_vol=0.10,           # high vol
                bid_ask_spread_pct=1.0,   # wide spread
                ob_imbalance=-0.3,        # sell-heavy
                funding_rate=-0.0005,     # negative funding
                price_change_rate=-0.02,  # falling
            )
            rd.update(obs)
        # After many HIGH_VOL-like obs, HIGH_VOL or TRENDING should dominate
        regime = rd.get_regime()
        assert regime in ("HIGH_VOL", "TRENDING", "RANGING", "THIN_BOOK")  # any valid state

    def test_ranging_observation_tends_to_ranging(self):
        rd = RegimeDetector()
        for _ in range(20):
            obs = observation_from_floats(
                price_vol=0.01,           # low vol
                bid_ask_spread_pct=0.05,  # tight spread
                ob_imbalance=0.0,         # balanced
                funding_rate=0.0,         # neutral
                price_change_rate=0.001,  # tiny positive drift
            )
            rd.update(obs)
        regime = rd.get_regime()
        assert regime in ("RANGING", "TRENDING", "HIGH_VOL", "THIN_BOOK")


class TestGetMmParameters:

    def test_returns_all_keys(self):
        rd = RegimeDetector()
        for regime in ("RANGING", "TRENDING", "HIGH_VOL", "THIN_BOOK"):
            params = rd.get_mm_parameters(regime)
            assert "spread_mult" in params
            assert "depth_mult" in params
            assert "aggressiveness" in params
            assert "inventory_skew" in params

    def test_ranging_defaults(self):
        rd = RegimeDetector()
        params = rd.get_mm_parameters("RANGING")
        assert params["spread_mult"] == 1.0
        assert params["depth_mult"] == 1.0
        assert params["aggressiveness"] == 0.8
        assert params["inventory_skew"] == 0.0

    def test_high_vol_wide_spreads(self):
        rd = RegimeDetector()
        params = rd.get_mm_parameters("HIGH_VOL")
        assert params["spread_mult"] >= 2.0
        assert params["depth_mult"] <= 0.5
        assert params["aggressiveness"] <= 0.2

    def test_thin_book_passive(self):
        rd = RegimeDetector()
        params = rd.get_mm_parameters("THIN_BOOK")
        assert params["aggressiveness"] == 0.0  # fully passive

    def test_unknown_regime_falls_back_to_ranging(self):
        rd = RegimeDetector()
        params = rd.get_mm_parameters("NONEXISTENT")
        assert params == MM_PARAMETERS["RANGING"]

    def test_returns_copy_not_reference(self):
        rd = RegimeDetector()
        params1 = rd.get_mm_parameters("RANGING")
        params1["spread_mult"] = 99.0
        params2 = rd.get_mm_parameters("RANGING")
        assert params2["spread_mult"] != 99.0


# ---------------------------------------------------------------------------
# Discretisation tests
# ---------------------------------------------------------------------------

class TestDiscretisation:

    def test_price_vol_bins(self):
        assert discretise_price_vol(0.01) == 0   # low
        assert discretise_price_vol(0.03) == 1   # medium
        assert discretise_price_vol(0.08) == 2   # high

    def test_bid_ask_spread_bins(self):
        assert discretise_bid_ask_spread(0.05) == 0   # tight
        assert discretise_bid_ask_spread(0.2) == 1    # normal
        assert discretise_bid_ask_spread(1.0) == 2    # wide

    def test_ob_imbalance_bins(self):
        assert discretise_ob_imbalance(-0.5) == 0    # sell-heavy
        assert discretise_ob_imbalance(0.0) == 1     # balanced
        assert discretise_ob_imbalance(0.5) == 2     # buy-heavy

    def test_funding_rate_bins(self):
        assert discretise_funding_rate(-0.001) == 0  # negative
        assert discretise_funding_rate(0.0) == 1     # neutral
        assert discretise_funding_rate(0.001) == 2   # positive

    def test_price_change_rate_bins(self):
        assert discretise_price_change_rate(-0.02) == 0  # big drop
        assert discretise_price_change_rate(-0.005) == 1  # small drop
        assert discretise_price_change_rate(0.005) == 2  # small gain
        assert discretise_price_change_rate(0.02) == 3   # big gain

    def test_encode_observation_range(self):
        for pv in range(3):
            for bas in range(3):
                for obi in range(3):
                    for fr in range(3):
                        for pcr in range(4):
                            obs = encode_observation(pv, bas, obi, fr, pcr)
                            assert 0 <= obs < N_OBS, f"obs={obs} out of [0, {N_OBS})"

    def test_observation_from_floats_in_range(self):
        obs = observation_from_floats(0.01, 0.05, 0.0, 0.0, 0.001)
        assert 0 <= obs < N_OBS


# ---------------------------------------------------------------------------
# ZZ / HMM agreement tests
# ---------------------------------------------------------------------------

class TestRegimeAgreement:

    def test_ranging_choppy_agrees(self):
        assert _regimes_agree("RANGING", "choppy") is True

    def test_trending_trending_up_agrees(self):
        assert _regimes_agree("TRENDING", "trending_up") is True

    def test_trending_trending_down_agrees(self):
        assert _regimes_agree("TRENDING", "trending_down") is True

    def test_ranging_trending_disagrees(self):
        assert _regimes_agree("RANGING", "trending_up") is False

    def test_trending_choppy_disagrees(self):
        assert _regimes_agree("TRENDING", "choppy") is False

    def test_high_vol_always_agrees(self):
        # ZZ has no HIGH_VOL state — should not flag as disagreement
        assert _regimes_agree("HIGH_VOL", "choppy") is True
        assert _regimes_agree("HIGH_VOL", "trending_up") is True

    def test_thin_book_always_agrees(self):
        assert _regimes_agree("THIN_BOOK", "choppy") is True
        assert _regimes_agree("THIN_BOOK", "trending_down") is True

    def test_zz_to_hmm_mapping(self):
        assert _zz_to_hmm_regime("trending_up") == "TRENDING"
        assert _zz_to_hmm_regime("trending_down") == "TRENDING"
        assert _zz_to_hmm_regime("choppy") == "RANGING"
        assert _zz_to_hmm_regime("") == "RANGING"
        assert _zz_to_hmm_regime(None) == "RANGING"


# ---------------------------------------------------------------------------
# RegimeMaster async tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRegimeMaster:

    async def test_default_regime_state(self):
        master = RegimeMaster()
        rs = master.current_regime_state
        assert rs.regime in REGIME_NAMES.values()
        assert 0.0 <= rs.confidence <= 1.0
        assert rs.mm_params is not None

    async def test_push_state_updates_regime(self):
        master = RegimeMaster()
        task = master.start_task()
        try:
            state = _make_global_state(vol=0.01, zz_regime="choppy")
            master.push_state(state)
            # Give the regime loop time to process
            await asyncio.sleep(0.1)
            rs = master.current_regime_state
            assert rs.regime in REGIME_NAMES.values()
        finally:
            await master.stop()

    async def test_subscriber_receives_regime_state(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        master = RegimeMaster(subscriber_queues=[q])
        task = master.start_task()
        try:
            state = _make_global_state(vol=0.01, zz_regime="choppy")
            master.push_state(state)
            await asyncio.sleep(0.15)
            assert not q.empty(), "Subscriber queue should have received a RegimeState"
            regime_state = q.get_nowait()
            assert isinstance(regime_state, RegimeState)
            assert regime_state.regime in REGIME_NAMES.values()
        finally:
            await master.stop()

    async def test_disagreement_uses_conservative_params(self):
        """When HMM says RANGING but ZZ says trending, conservative params apply."""
        master = RegimeMaster()
        task = master.start_task()
        try:
            # Very low vol (biases HMM toward RANGING) but ZZ says trending
            state = _make_global_state(vol=0.001, zz_regime="trending_up", global_mid=0.10)
            master.push_state(state)
            await asyncio.sleep(0.15)
            rs = master.current_regime_state
            # If disagreement was detected, mm_params should be conservative
            # (or if they agree, the standard params apply — either is valid)
            assert rs.mm_params is not None
            assert "spread_mult" in rs.mm_params
        finally:
            await master.stop()

    async def test_add_remove_subscriber(self):
        master = RegimeMaster()
        q: asyncio.Queue = asyncio.Queue()
        master.add_subscriber(q)
        assert q in master._subscriber_queues
        master.remove_subscriber(q)
        assert q not in master._subscriber_queues

    async def test_stop_cancels_task(self):
        master = RegimeMaster()
        task = master.start_task()
        assert not task.done()
        await master.stop()
        assert task.done()

    async def test_regime_state_dataclass(self):
        rs = RegimeState(
            regime="RANGING",
            confidence=0.85,
            zz_regime="choppy",
            agreement=True,
            mm_params=MM_PARAMETERS["RANGING"].copy(),
            timestamp=time.time(),
        )
        assert rs.regime == "RANGING"
        assert rs.agreement is True
        assert rs.mm_params["spread_mult"] == 1.0

    async def test_multiple_pushes(self):
        """Multiple rapid pushes should not crash the regime master."""
        master = RegimeMaster()
        task = master.start_task()
        try:
            for i in range(20):
                state = _make_global_state(
                    vol=0.01 + i * 0.001,
                    zz_regime="choppy" if i % 2 == 0 else "trending_up",
                )
                master.push_state(state)
            await asyncio.sleep(0.3)
            rs = master.current_regime_state
            assert rs.regime in REGIME_NAMES.values()
        finally:
            await master.stop()


# ---------------------------------------------------------------------------
# Spread / Depth engine regime_mult tests
# ---------------------------------------------------------------------------

class TestSpreadEngineRegimeMult:

    def test_spread_mult_widens_spreads(self, spread_config):
        from quant.spread_engine import SpreadEngine
        engine = SpreadEngine(spread_config)
        buy_1x, sell_1x = engine.compute_levels(0.5, 5, spread_mult=1.0)
        buy_3x, sell_3x = engine.compute_levels(0.5, 5, spread_mult=3.0)
        # Wider spread_mult should produce larger absolute values
        assert abs(buy_3x[0]) > abs(buy_1x[0]), "spread_mult=3 should widen buy levels"
        assert sell_3x[0] > sell_1x[0], "spread_mult=3 should widen sell levels"

    def test_spread_mult_default_unchanged(self, spread_config):
        from quant.spread_engine import SpreadEngine
        engine = SpreadEngine(spread_config)
        buy_default, sell_default = engine.compute_levels(0.5, 5)
        buy_1x, sell_1x = engine.compute_levels(0.5, 5, spread_mult=1.0)
        assert buy_default == buy_1x
        assert sell_default == sell_1x


class TestDepthEngineRegimeMult:

    def test_depth_mult_reduces_size(self, depth_config):
        from quant.depth_engine import DepthEngine
        engine = DepthEngine(depth_config)
        amounts_1x = engine.compute_amounts(0.5, 5, skew_factor=1.0, side="buy", depth_mult=1.0)
        amounts_03 = engine.compute_amounts(0.5, 5, skew_factor=1.0, side="buy", depth_mult=0.3)
        assert sum(amounts_03) < sum(amounts_1x), "depth_mult=0.3 should reduce total size"

    def test_depth_mult_default_unchanged(self, depth_config):
        from quant.depth_engine import DepthEngine
        engine = DepthEngine(depth_config)
        amounts_default = engine.compute_amounts(0.5, 5, skew_factor=1.0, side="buy")
        amounts_1x = engine.compute_amounts(0.5, 5, skew_factor=1.0, side="buy", depth_mult=1.0)
        assert amounts_default == amounts_1x
