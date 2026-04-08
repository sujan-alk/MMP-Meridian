"""
HMM Regime Detector — Baum-Welch (pure numpy)

Adapted for Meridian Market Making Platform.

Hidden states (MM-optimised):
  0: RANGING    — best for MM: tight spreads, high aggressiveness
  1: TRENDING   — reduce aggressiveness, lean inventory with trend
  2: HIGH_VOL   — widen spreads significantly, small depth
  3: THIN_BOOK  — order book is thin, go fully passive or pause

Observable features (each discretised to a small alphabet):
  0  price_vol        : 0=low, 1=medium, 2=high  (ALKIMI realised volatility)
  1  bid_ask_spread   : 0=tight, 1=normal, 2=wide
  2  ob_imbalance     : 0=sell-heavy, 1=balanced, 2=buy-heavy
  3  funding_rate     : 0=negative, 1=neutral, 2=positive
  4  price_change_rate: 0=<-1%, 1=-1..0%, 2=0..+1%, 3=>+1%  (global_mid change rate)

The joint observation is encoded as a single integer:
    obs = pv*48 + bas*16 + obi*8 + fr*4 + pcr
    (base sizes: 3, 3, 3, 3, 4 → 108 combos)
"""

import logging
import pickle
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────

REGIME_NAMES = {0: "RANGING", 1: "TRENDING", 2: "HIGH_VOL", 3: "THIN_BOOK"}
N_STATES = 4

# alphabet sizes per feature
_PV_SIZE  = 3   # price volatility bins: low, medium, high
_BAS_SIZE = 3   # bid-ask spread bins: tight, normal, wide
_OBI_SIZE = 3   # order book imbalance bins: sell-heavy, balanced, buy-heavy
_FR_SIZE  = 3   # funding rate bins: negative, neutral, positive
_PCR_SIZE = 4   # price change rate bins: <-1%, -1..0%, 0..+1%, >+1%

N_OBS = _PV_SIZE * _BAS_SIZE * _OBI_SIZE * _FR_SIZE * _PCR_SIZE  # 108

# MM parameters indexed by regime name
MM_PARAMETERS = {
    'RANGING':   {'spread_mult': 1.0, 'depth_mult': 1.0, 'aggressiveness': 0.8, 'inventory_skew': 0.0},
    'TRENDING':  {'spread_mult': 1.5, 'depth_mult': 0.7, 'aggressiveness': 0.4, 'inventory_skew': 0.2},
    'HIGH_VOL':  {'spread_mult': 3.0, 'depth_mult': 0.3, 'aggressiveness': 0.1, 'inventory_skew': 0.0},
    'THIN_BOOK': {'spread_mult': 2.0, 'depth_mult': 0.5, 'aggressiveness': 0.0, 'inventory_skew': 0.0},
}


# ── sensible default HMM parameters ───────────────────────────────────────────

def _default_pi() -> np.ndarray:
    """Initial state distribution — RANGING is most common MM state."""
    return np.array([0.50, 0.25, 0.15, 0.10])


def _default_A() -> np.ndarray:
    """
    Transition matrix A[i,j] = P(state j | state i).
    Regimes are sticky (high diagonal).
    """
    A = np.array([
        # RANGING  TRENDING  HIGH_VOL  THIN_BOOK
        [0.75,     0.12,     0.08,     0.05],  # from RANGING
        [0.20,     0.65,     0.10,     0.05],  # from TRENDING
        [0.15,     0.10,     0.65,     0.10],  # from HIGH_VOL
        [0.25,     0.10,     0.15,     0.50],  # from THIN_BOOK
    ], dtype=float)
    A /= A.sum(axis=1, keepdims=True)
    return A


def _default_B() -> np.ndarray:
    """
    Emission matrix B[i, o] = P(obs o | state i).
    Features treated as conditionally independent given state.
    """
    # price_vol_probs[state, bin]  (3 bins: low, medium, high)
    price_vol_probs = np.array([
        [0.55, 0.35, 0.10],  # RANGING: low vol
        [0.20, 0.50, 0.30],  # TRENDING: medium vol
        [0.05, 0.20, 0.75],  # HIGH_VOL: high vol
        [0.30, 0.40, 0.30],  # THIN_BOOK: mixed (thin books can appear in any vol)
    ])

    # bid_ask_spread_probs[state, bin]  (3 bins: tight, normal, wide)
    bas_probs = np.array([
        [0.60, 0.35, 0.05],  # RANGING: tight spreads
        [0.25, 0.55, 0.20],  # TRENDING: normal spreads
        [0.05, 0.30, 0.65],  # HIGH_VOL: wide spreads
        [0.10, 0.30, 0.60],  # THIN_BOOK: wide spreads (illiquid)
    ])

    # ob_imbalance_probs[state, bin]  (3 bins: sell-heavy, balanced, buy-heavy)
    obi_probs = np.array([
        [0.20, 0.60, 0.20],  # RANGING: balanced
        [0.20, 0.30, 0.50],  # TRENDING: slight buy bias (trending up)
        [0.35, 0.40, 0.25],  # HIGH_VOL: slight sell bias (fear)
        [0.25, 0.50, 0.25],  # THIN_BOOK: mixed
    ])

    # funding_rate_probs[state, bin]  (3 bins: negative, neutral, positive)
    fr_probs = np.array([
        [0.20, 0.60, 0.20],  # RANGING: neutral funding
        [0.10, 0.30, 0.60],  # TRENDING: positive funding (longs paying)
        [0.40, 0.30, 0.30],  # HIGH_VOL: mixed/negative
        [0.30, 0.50, 0.20],  # THIN_BOOK: mostly neutral
    ])

    # price_change_rate_probs[state, bin]  (4 bins: <-1%, -1..0%, 0..+1%, >+1%)
    pcr_probs = np.array([
        [0.10, 0.35, 0.45, 0.10],  # RANGING: small moves
        [0.10, 0.15, 0.40, 0.35],  # TRENDING: larger positive moves
        [0.20, 0.25, 0.25, 0.30],  # HIGH_VOL: fat tails both ways
        [0.15, 0.30, 0.40, 0.15],  # THIN_BOOK: moderate moves
    ])

    # Build joint: B[state, pv*48 + bas*16 + obi*8 + fr*4 + pcr]
    B = np.zeros((N_STATES, N_OBS))
    for pv in range(_PV_SIZE):
        for bas in range(_BAS_SIZE):
            for obi in range(_OBI_SIZE):
                for fr in range(_FR_SIZE):
                    for pcr in range(_PCR_SIZE):
                        idx = (pv * (_BAS_SIZE * _OBI_SIZE * _FR_SIZE * _PCR_SIZE)
                               + bas * (_OBI_SIZE * _FR_SIZE * _PCR_SIZE)
                               + obi * (_FR_SIZE * _PCR_SIZE)
                               + fr * _PCR_SIZE
                               + pcr)
                        for s in range(N_STATES):
                            B[s, idx] = (price_vol_probs[s, pv]
                                         * bas_probs[s, bas]
                                         * obi_probs[s, obi]
                                         * fr_probs[s, fr]
                                         * pcr_probs[s, pcr])

    # Normalise each row
    B /= B.sum(axis=1, keepdims=True)
    return B


# ── discretisation helpers ─────────────────────────────────────────────────────

def discretise_price_vol(vol: float) -> int:
    """ALKIMI realised volatility → 0=low, 1=medium, 2=high"""
    if vol < 0.02:   return 0
    if vol < 0.05:   return 1
    return 2


def discretise_bid_ask_spread(spread_pct: float) -> int:
    """Bid-ask spread as % of mid → 0=tight, 1=normal, 2=wide"""
    if spread_pct < 0.1:   return 0
    if spread_pct < 0.5:   return 1
    return 2


def discretise_ob_imbalance(imbalance: float) -> int:
    """
    Order book imbalance ∈ [-1, 1]:
      -1 = fully sell-heavy, 0 = balanced, +1 = fully buy-heavy
    """
    if imbalance < -0.2:   return 0
    if imbalance >  0.2:   return 2
    return 1


def discretise_funding_rate(rate: float) -> int:
    """funding_rate → 0=negative, 1=neutral, 2=positive"""
    if rate < -0.0001:  return 0
    if rate >  0.0001:  return 2
    return 1


def discretise_price_change_rate(pct_change: float) -> int:
    """global_mid price change rate → 0=<-1%, 1=-1..0%, 2=0..+1%, 3=>+1%"""
    if pct_change < -0.01:  return 0
    if pct_change <  0.00:  return 1
    if pct_change <=  0.01: return 2
    return 3


def encode_observation(pv: int, bas: int, obi: int, fr: int, pcr: int) -> int:
    return (pv * (_BAS_SIZE * _OBI_SIZE * _FR_SIZE * _PCR_SIZE)
            + bas * (_OBI_SIZE * _FR_SIZE * _PCR_SIZE)
            + obi * (_FR_SIZE * _PCR_SIZE)
            + fr * _PCR_SIZE
            + pcr)


def observation_from_floats(
    price_vol: float,
    bid_ask_spread_pct: float,
    ob_imbalance: float,
    funding_rate: float,
    price_change_rate: float,
) -> int:
    """Build an encoded observation integer from raw feature floats."""
    return encode_observation(
        discretise_price_vol(price_vol),
        discretise_bid_ask_spread(bid_ask_spread_pct),
        discretise_ob_imbalance(ob_imbalance),
        discretise_funding_rate(funding_rate),
        discretise_price_change_rate(price_change_rate),
    )


# ── Baum-Welch (forward-backward) ─────────────────────────────────────────────

def _forward(obs: Sequence[int], pi: np.ndarray, A: np.ndarray, B: np.ndarray):
    T = len(obs)
    N = pi.shape[0]
    alpha = np.zeros((T, N))
    alpha[0] = pi * B[:, obs[0]]
    scale = np.zeros(T)
    scale[0] = alpha[0].sum()
    if scale[0] == 0:
        scale[0] = 1e-300
    alpha[0] /= scale[0]

    for t in range(1, T):
        alpha[t] = (alpha[t - 1] @ A) * B[:, obs[t]]
        scale[t] = alpha[t].sum()
        if scale[t] == 0:
            scale[t] = 1e-300
        alpha[t] /= scale[t]

    log_likelihood = np.sum(np.log(scale + 1e-300))
    return alpha, scale, log_likelihood


def _backward(obs: Sequence[int], A: np.ndarray, B: np.ndarray, scale: np.ndarray):
    T = len(obs)
    N = A.shape[0]
    beta = np.zeros((T, N))
    beta[-1] = 1.0

    for t in range(T - 2, -1, -1):
        beta[t] = A @ (B[:, obs[t + 1]] * beta[t + 1])
        beta[t] /= scale[t + 1]

    return beta


def baum_welch(
    obs_sequences: List[List[int]],
    pi: np.ndarray,
    A: np.ndarray,
    B: np.ndarray,
    n_iter: int = 50,
    tol: float = 1e-4,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run Baum-Welch EM until convergence or n_iter."""
    N = pi.shape[0]
    M = B.shape[1]
    prev_ll = -np.inf

    for iteration in range(n_iter):
        pi_acc = np.zeros(N)
        A_acc  = np.zeros((N, N))
        B_acc  = np.zeros((N, M))
        total_ll = 0.0

        for obs in obs_sequences:
            if len(obs) < 2:
                continue
            alpha, scale, ll = _forward(obs, pi, A, B)
            beta = _backward(obs, A, B, scale)
            total_ll += ll

            gamma = alpha * beta
            row_sum = gamma.sum(axis=1, keepdims=True)
            row_sum[row_sum == 0] = 1e-300
            gamma /= row_sum

            T = len(obs)
            for t in range(T - 1):
                numer = np.outer(alpha[t], beta[t + 1] * B[:, obs[t + 1]]) * A
                denom = numer.sum()
                if denom == 0:
                    denom = 1e-300
                xi_t = numer / denom
                A_acc += xi_t

            pi_acc += gamma[0]
            for t in range(T):
                B_acc[:, obs[t]] += gamma[t]

        pi = pi_acc / (pi_acc.sum() + 1e-300)
        A  = A_acc / (A_acc.sum(axis=1, keepdims=True) + 1e-300)
        B  = B_acc / (B_acc.sum(axis=1, keepdims=True) + 1e-300)

        pi = np.maximum(pi, 1e-10); pi /= pi.sum()
        A  = np.maximum(A, 1e-10); A /= A.sum(axis=1, keepdims=True)
        B  = np.maximum(B, 1e-10); B /= B.sum(axis=1, keepdims=True)

        delta = total_ll - prev_ll
        logger.debug(f"Baum-Welch iter {iteration}: ll={total_ll:.2f}  Δ={delta:.4f}")
        if abs(delta) < tol and iteration > 0:
            logger.info(f"Baum-Welch converged after {iteration + 1} iterations")
            break
        prev_ll = total_ll

    return pi, A, B


# ── RegimeDetector ─────────────────────────────────────────────────────────────

class RegimeDetector:
    """
    4-state Hidden Markov Model regime detector for Meridian MM Platform.

    Hidden states: RANGING, TRENDING, HIGH_VOL, THIN_BOOK
    Observable features: ALKIMI price volatility, bid-ask spread,
                         order book imbalance, funding rate, price change rate.

    Usage
    -----
    >>> rd = RegimeDetector()
    >>> obs = observation_from_floats(0.015, 0.08, 0.1, 0.0001, 0.003)
    >>> rd.update(obs)
    >>> rd.get_regime()
    'RANGING'
    """

    def __init__(self):
        self.pi: np.ndarray = _default_pi()
        self.A:  np.ndarray = _default_A()
        self.B:  np.ndarray = _default_B()

        self._belief: np.ndarray = self.pi.copy()
        self._last_obs: Optional[int] = None

    # ── public API ──────────────────────────────────────────────────────────

    def update(self, observation: int) -> None:
        """Update belief state with a new encoded observation."""
        if not (0 <= observation < N_OBS):
            logger.warning(f"Observation {observation} out of range [0, {N_OBS}), ignoring.")
            return

        pred = self._belief @ self.A
        likelihood = self.B[:, observation]
        updated = pred * likelihood

        denom = updated.sum()
        if denom < 1e-300:
            logger.warning("All likelihoods near-zero; resetting belief to prior.")
            self._belief = self.pi.copy()
        else:
            self._belief = updated / denom

        self._last_obs = observation

    def update_from_floats(
        self,
        price_vol: float,
        bid_ask_spread_pct: float,
        ob_imbalance: float,
        funding_rate: float,
        price_change_rate: float,
    ) -> None:
        """Convenience wrapper: pass raw feature values directly."""
        obs = observation_from_floats(
            price_vol, bid_ask_spread_pct, ob_imbalance, funding_rate, price_change_rate
        )
        self.update(obs)

    def get_regime(self) -> str:
        """Return the name of the most likely current regime."""
        return REGIME_NAMES[int(np.argmax(self._belief))]

    def get_regime_confidence(self) -> float:
        """Return the posterior probability of the most likely state (0-1)."""
        return float(np.max(self._belief))

    def get_belief(self) -> Dict[str, float]:
        """Return the full posterior distribution over regimes."""
        return {REGIME_NAMES[i]: float(self._belief[i]) for i in range(N_STATES)}

    def get_mm_parameters(self, regime: Optional[str] = None) -> dict:
        """
        Return market making parameters for the given (or current) regime.

        Returns a dict with:
            spread_mult     : multiplier for computed spread width
            depth_mult      : multiplier for order depth/size
            aggressiveness  : override for aggressiveness model output
            inventory_skew  : directional bias for inventory target (positive = lean long)
        """
        name = regime if regime is not None else self.get_regime()
        return MM_PARAMETERS.get(name, MM_PARAMETERS['RANGING']).copy()

    # ── training ────────────────────────────────────────────────────────────

    def train(
        self,
        observation_sequences: List[List[int]],
        n_iter: int = 100,
        tol: float = 1e-4,
    ) -> None:
        """Run Baum-Welch on a list of observation sequences."""
        logger.info(f"Training HMM on {len(observation_sequences)} sequences "
                    f"(max {n_iter} iters, tol={tol})")
        self.pi, self.A, self.B = baum_welch(
            observation_sequences, self.pi, self.A, self.B, n_iter, tol
        )
        self._belief = self.pi.copy()
        logger.info("Training complete.")

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Persist model parameters to a pickle file."""
        state = {"pi": self.pi, "A": self.A, "B": self.B, "belief": self._belief}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(state, fh)
        logger.info(f"RegimeDetector saved → {path}")

    def load(self, path: str) -> bool:
        """Load model parameters from a pickle file. Returns True on success."""
        p = Path(path)
        if not p.exists():
            logger.info(f"No model file at {path}, using defaults.")
            return False
        try:
            with open(p, "rb") as fh:
                state = pickle.load(fh)
            self.pi    = state["pi"]
            self.A     = state["A"]
            self.B     = state["B"]
            self._belief = state.get("belief", self.pi.copy())
            logger.info(f"RegimeDetector loaded ← {path}")
            return True
        except Exception as exc:
            logger.warning(f"Failed to load model from {path}: {exc}")
            return False

    def __repr__(self) -> str:
        return (f"RegimeDetector(regime={self.get_regime()!r}, "
                f"confidence={self.get_regime_confidence():.1%})")
