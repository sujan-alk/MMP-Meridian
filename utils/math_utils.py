"""
Pure mathematical helpers for the quant model.
All functions are stateless and unit-testable with no side-effects.
"""

from __future__ import annotations

import numpy as np


def power_curve(n: int, gamma: float) -> np.ndarray:
    """
    Generate n evenly-spaced points [0..1] mapped through a power curve t^gamma.
    gamma > 1 → front-loaded (values cluster near 0)
    gamma < 1 → back-loaded (values cluster near 1)
    gamma = 1 → linear
    """
    t = np.linspace(0.0, 1.0, n)
    return t ** gamma


def interpolate_levels(lo: float, hi: float, n: int, gamma: float) -> np.ndarray:
    """
    Interpolate n values between lo and hi using a power curve.
    lo=tightest level, hi=widest level.
    gamma controls clustering:
      - High gamma (e.g. 4) → most levels cluster near lo, one level at hi
    """
    t = power_curve(n, gamma)
    return lo + (hi - lo) * t


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """
    Normalize a dict of weights so they sum to 1.0.
    Zero-weight entries are preserved; raises if all weights are 0.
    """
    total = sum(weights.values())
    if total == 0:
        raise ValueError("All weights are zero — cannot normalize")
    return {k: v / total for k, v in weights.items()}


def geometric_decay(n: int, decay: float = 0.8) -> np.ndarray:
    """
    Generate n values with geometric decay: [1, decay, decay^2, ...].
    Returns a normalized array summing to 1.0.
    """
    weights = np.array([decay ** i for i in range(n)], dtype=float)
    return weights / weights.sum()


def clip(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi]."""
    return max(lo, min(hi, value))


def pct_change(new: float, old: float) -> float:
    """Percentage change from old to new. Returns 0 if old is 0."""
    if old == 0:
        return 0.0
    return (new - old) / old
