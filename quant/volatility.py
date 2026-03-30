"""
Volatility Engine — two models:

1. Simple rolling volatility (Phase 1):
   - 10-minute rolling window of percentage returns
   - Standard deviation of returns

2. Zhang-Zhang (2018) volatility (Phase 1b):
   - Uses OHLCV candle data to produce a more robust estimator
   - Distinguishes between trending (up/down) and choppy regimes
   - Recommended by Huy as used by Barclays, Deutsche Bank
   - Formula: σ² = mean[0.5*(ln(H/L))² - (2ln2-1)*(ln(C/O))²]
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from config.schema import VolatilityConfig
from exchange.base import Candle
from utils.time_utils import now_s


class VolatilityEngine:
    """
    Computes volatility from a rolling window of mid-price observations or OHLCV candles.
    """

    def __init__(self, config: VolatilityConfig):
        self.cfg = config
        # Rolling window for simple std-dev vol (sized to hold window_minutes × 60 1-second samples)
        max_samples = config.window_minutes * 60 + 10
        self._closes: deque[float] = deque(maxlen=max_samples)
        # Candle buffer for Zhang-Zhang (1-minute candles)
        self._candles: deque[Candle] = deque(maxlen=config.window_minutes + 5)
        self._last_update_s: float = 0.0

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def update_price(self, price: float) -> None:
        """Add a new price observation (called every tick, ~1s)."""
        self._closes.append(price)
        self._last_update_s = now_s()

    def update_candle(self, candle: Candle) -> None:
        """Add a completed OHLCV candle (called when a new 1-minute candle closes)."""
        self._candles.append(candle)

    def update_candles(self, candles: list[Candle]) -> None:
        """Bulk-load candles (called on warmup)."""
        for c in candles:
            self._candles.append(c)

    # ------------------------------------------------------------------
    # Simple rolling volatility (Phase 1)
    # ------------------------------------------------------------------

    def rolling_vol(self) -> float:
        """
        Standard deviation of percentage returns over the rolling window.

        Returns:
            float: volatility reading (e.g. 0.002 = 0.2% std dev)
                   Returns 0.0 if insufficient data.
        """
        closes = list(self._closes)
        if len(closes) < 3:
            return 0.0

        closes_arr = np.array(closes, dtype=float)
        # Percentage returns: (p_t - p_{t-1}) / p_{t-1}
        returns = np.diff(closes_arr) / closes_arr[:-1]
        return float(np.std(returns))

    # ------------------------------------------------------------------
    # Zhang-Zhang (2018) volatility (Phase 1b)
    # ------------------------------------------------------------------

    def zhang_zhang_vol(self) -> tuple[float, str]:
        """
        Zhang-Zhang (2018) volatility estimator using OHLCV candle data.

        More robust than simple std-dev; differentiates between:
          - 'trending_up'   → price drifting upward
          - 'trending_down' → price drifting downward
          - 'choppy'        → sideways oscillation

        Formula:
          For each candle: term_i = 0.5*(ln(H/L))² - (2ln2-1)*(ln(C/O))²
          σ² = mean(term_i)  → σ = sqrt(max(σ², 0))

        Regime detection:
          net_direction = mean(ln(C/O))
          > +threshold  → trending_up
          < -threshold  → trending_down
          else          → choppy

        Returns:
            (zz_vol, regime): volatility estimate + regime string
        """
        candles = list(self._candles)
        if len(candles) < 3:
            return 0.0, "choppy"

        _2ln2_minus_1 = 2.0 * math.log(2) - 1.0
        hl_terms: list[float] = []
        co_terms: list[float] = []
        net_co: list[float] = []

        for c in candles:
            if c.high <= 0 or c.low <= 0 or c.close <= 0 or c.open <= 0:
                continue
            # Guard against degenerate candles (high == low)
            if c.high == c.low:
                hl_terms.append(0.0)
            else:
                hl_terms.append(0.5 * (math.log(c.high / c.low)) ** 2)
            co_val = math.log(c.close / c.open)
            co_terms.append(_2ln2_minus_1 * co_val ** 2)
            net_co.append(co_val)

        if not hl_terms:
            return 0.0, "choppy"

        zz_variance = float(np.mean(np.array(hl_terms) - np.array(co_terms)))
        zz_vol = math.sqrt(max(zz_variance, 0.0))

        net = float(np.mean(net_co))
        threshold = 0.0015  # 0.15% mean candle direction to call a trend
        if net > threshold:
            regime = "trending_up"
        elif net < -threshold:
            regime = "trending_down"
        else:
            regime = "choppy"

        return zz_vol, regime

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """True once we have enough data for a meaningful vol estimate."""
        return len(self._closes) >= 10

    def candles_ready(self) -> bool:
        """True once we have enough candles for Zhang-Zhang."""
        return len(self._candles) >= 3

    @property
    def sample_count(self) -> int:
        return len(self._closes)

    @property
    def candle_count(self) -> int:
        return len(self._candles)
