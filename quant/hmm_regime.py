"""
Baum-Welch HMM Regime Detector.

Uses a Gaussian Hidden Markov Model to classify market regimes from
observable features derived from OHLCV candle data. The Baum-Welch
(EM) algorithm refines the model parameters online.

5 hidden states:
  LOW_VOL        — calm market, trade aggressively
  NORMAL         — standard conditions
  HIGH_VOL       — elevated volatility, widen spreads
  HIGH_VOL_CRASH — crash/freefall: keep buy-side liquidity to absorb selling
  RECOVERY       — post-crash bounce

4D observation vector (all from existing candle data):
  [0] zz_vol           — Zhang-Zhang volatility
  [1] net_direction     — mean(ln(C/O)), directional bias
  [2] volume_change     — (current - mean) / mean, normalised volume spike
  [3] price_roc         — rate of price change over recent candles
"""

from __future__ import annotations

from collections import deque
from enum import IntEnum

import numpy as np

from config.schema import HMMConfig

try:
    from hmmlearn.hmm import GaussianHMM

    _HAS_HMMLEARN = True
except ImportError:
    _HAS_HMMLEARN = False


class HMMRegime(IntEnum):
    LOW_VOL = 0
    NORMAL = 1
    HIGH_VOL = 2
    HIGH_VOL_CRASH = 3
    RECOVERY = 4


HMM_REGIME_NAMES: dict[int, str] = {
    HMMRegime.LOW_VOL: "LOW_VOL",
    HMMRegime.NORMAL: "NORMAL",
    HMMRegime.HIGH_VOL: "HIGH_VOL",
    HMMRegime.HIGH_VOL_CRASH: "HIGH_VOL_CRASH",
    HMMRegime.RECOVERY: "RECOVERY",
}

N_STATES = 5
N_FEATURES = 4


def _default_transition_matrix() -> np.ndarray:
    """Hand-tuned transition probabilities (rows must sum to 1.0)."""
    return np.array([
        # To:  LOW_VOL  NORMAL  HIGH_VOL  CRASH  RECOVERY
        [0.90,   0.08,   0.02,   0.00,   0.00],   # From LOW_VOL
        [0.05,   0.85,   0.08,   0.01,   0.01],   # From NORMAL
        [0.02,   0.10,   0.70,   0.15,   0.03],   # From HIGH_VOL
        [0.00,   0.02,   0.08,   0.60,   0.30],   # From HIGH_VOL_CRASH
        [0.10,   0.35,   0.05,   0.00,   0.50],   # From RECOVERY
    ])


def _default_start_probs() -> np.ndarray:
    return np.array([0.15, 0.60, 0.15, 0.05, 0.05])


def _default_means() -> np.ndarray:
    """Emission means for each state across the 4D feature space."""
    return np.array([
        # [zz_vol,  net_dir,  vol_change,  price_roc]
        [0.0005,  0.0000,   0.00,   0.0000],   # LOW_VOL: calm
        [0.0015,  0.0000,   0.10,   0.0010],   # NORMAL: moderate
        [0.0040, -0.0010,   0.30,  -0.0020],   # HIGH_VOL: elevated
        [0.0080, -0.0050,   0.80,  -0.0080],   # HIGH_VOL_CRASH: freefall
        [0.0030,  0.0020,   0.40,   0.0030],   # RECOVERY: bounce
    ])


def _default_covars() -> np.ndarray:
    """Diagonal covariance for each state (shape: n_states x n_features)."""
    return np.array([
        [1e-6, 1e-6, 0.01, 1e-6],   # LOW_VOL: tight
        [5e-6, 5e-6, 0.05, 5e-6],   # NORMAL
        [1e-5, 1e-5, 0.10, 1e-5],   # HIGH_VOL
        [5e-5, 5e-5, 0.30, 5e-5],   # HIGH_VOL_CRASH: wide spread
        [1e-5, 1e-5, 0.15, 1e-5],   # RECOVERY
    ])


class HMMRegimeDetector:
    """
    Gaussian HMM regime detector using Baum-Welch for parameter refinement.

    Call `update()` each time new candle features are available (typically
    every 60 seconds). The detector maintains a rolling observation buffer
    and runs Viterbi decoding to classify the current regime.
    """

    def __init__(self, config: HMMConfig):
        self.cfg = config
        self._observations: deque[np.ndarray] = deque(maxlen=config.hmm_lookback)
        self._current_regime: str = "NORMAL"
        self._confidence: float = 0.0
        self._obs_count: int = 0

        if _HAS_HMMLEARN:
            self._model = self._build_model()
        else:
            self._model = None

    def _build_model(self) -> "GaussianHMM":
        model = GaussianHMM(
            n_components=N_STATES,
            covariance_type="diag",
            n_iter=0,  # don't fit on init, we set params manually
        )
        model.startprob_ = _default_start_probs()
        model.transmat_ = _default_transition_matrix()
        model.means_ = _default_means()
        model.covars_ = _default_covars()
        return model

    def update(
        self,
        zz_vol: float,
        net_direction: float,
        volume_change: float,
        price_roc: float,
    ) -> str:
        """
        Add a new observation and decode the current regime.

        Returns:
            Regime string: "LOW_VOL", "NORMAL", "HIGH_VOL", "HIGH_VOL_CRASH", or "RECOVERY"
        """
        obs = np.array([zz_vol, net_direction, volume_change, price_roc])
        self._observations.append(obs)
        self._obs_count += 1

        if not self.is_ready():
            return self._current_regime

        self._current_regime, self._confidence = self._decode_regime()

        # Periodically refit the model with Baum-Welch
        if (
            self._model is not None
            and self._obs_count > 0
            and self._obs_count % self.cfg.refit_interval == 0
            and len(self._observations) >= self.cfg.hmm_min_observations
        ):
            self._refit()

        return self._current_regime

    def _decode_regime(self) -> tuple[str, float]:
        """Run Viterbi decoding on the observation buffer."""
        obs_matrix = np.array(list(self._observations))

        if self._model is not None:
            try:
                log_prob, state_seq = self._model.decode(obs_matrix, algorithm="viterbi")
                current_state = int(state_seq[-1])
                # Approximate confidence from posterior marginals
                posteriors = self._model.predict_proba(obs_matrix)
                confidence = float(posteriors[-1, current_state])
                return HMM_REGIME_NAMES[current_state], confidence
            except Exception:
                return self._fallback_decode(obs_matrix)
        else:
            return self._fallback_decode(obs_matrix)

    def _fallback_decode(self, obs_matrix: np.ndarray) -> tuple[str, float]:
        """
        Fallback rule-based regime detection when hmmlearn is unavailable.
        Uses thresholds on the latest observation to classify regime.
        """
        latest = obs_matrix[-1]
        zz_vol, net_dir, vol_change, price_roc = latest

        # HIGH_VOL_CRASH: high vol + strong negative direction + volume spike
        if zz_vol > 0.005 and net_dir < -0.003 and price_roc < -0.004:
            return "HIGH_VOL_CRASH", 0.8

        # RECOVERY: elevated vol + positive direction after recent crash
        if zz_vol > 0.002 and net_dir > 0.001 and price_roc > 0.001:
            if self._current_regime in ("HIGH_VOL_CRASH", "RECOVERY"):
                return "RECOVERY", 0.7

        # HIGH_VOL: elevated vol
        if zz_vol > 0.003:
            return "HIGH_VOL", 0.7

        # LOW_VOL: very calm
        if zz_vol < 0.0008:
            return "LOW_VOL", 0.8

        return "NORMAL", 0.6

    def _refit(self) -> None:
        """Re-estimate HMM parameters via Baum-Welch (EM) on the observation buffer."""
        if self._model is None:
            return

        obs_matrix = np.array(list(self._observations))
        try:
            refit_model = GaussianHMM(
                n_components=N_STATES,
                covariance_type="diag",
                n_iter=10,
                init_params="",  # don't randomise — use our current params as start
            )
            # Seed from current model
            refit_model.startprob_ = self._model.startprob_.copy()
            refit_model.transmat_ = self._model.transmat_.copy()
            refit_model.means_ = self._model.means_.copy()
            refit_model.covars_ = self._model.covars_.copy()
            refit_model.fit(obs_matrix)
            self._model = refit_model
        except Exception:
            pass  # keep existing params if refit fails

    def is_ready(self) -> bool:
        """True once enough observations have been collected."""
        return len(self._observations) >= self.cfg.hmm_min_observations

    @property
    def regime(self) -> str:
        return self._current_regime

    @property
    def confidence(self) -> float:
        return self._confidence
