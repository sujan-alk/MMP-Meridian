"""
Spread Engine — computes per-level bid/ask spread percentages.

Based on Huy's Meta Config V1 power-curve interpolation:
- n_levels order levels per side
- Level 0 = tightest (closest to mid), Level n-1 = widest (furthest from mid)
- Aggressiveness controls where levels cluster:
    agg=1.0 → all levels cluster near the tightest spread (incentivise trading)
    agg=0.0 → all levels cluster near the widest spread (disincentivise trading)
    agg=0.5 → evenly spaced

Buy side spreads are negative percentages (below mid-price).
Sell side spreads are positive percentages (above mid-price).

Example (n=5, curve_strength=4):
  agg=0.0: buy_spreads ≈ [-5.0, -4.5, -4.0, -3.5, -0.1]
  agg=0.5: buy_spreads ≈ [-5.0, -3.8, -2.5, -1.3, -0.1]  (evenly spaced)
  agg=1.0: buy_spreads ≈ [-5.0, -0.5, -0.4, -0.2, -0.1]
"""

from __future__ import annotations

import numpy as np

from config.schema import SpreadConfig
from utils.math_utils import clip


class SpreadEngine:
    """
    Computes bid and ask spread levels from aggressiveness using a power curve.
    Direct Python translation of Huy's Meta Config V1 spread model.
    """

    def __init__(self, config: SpreadConfig, maker_fee_bps: float = 0.0):
        self.cfg = config
        self.maker_fee_bps = maker_fee_bps
        # Minimum profitable spread: must cover maker fees on both sides + buffer
        self.min_profitable_spread_bps = maker_fee_bps * 2 + 2

    def compute_levels(
        self,
        aggressiveness: float,
        n_levels: int,
    ) -> tuple[list[float], list[float]]:
        """
        Compute buy and sell spread levels.

        Args:
            aggressiveness: float ∈ [0.0, 1.0]
            n_levels: number of price levels per side

        Returns:
            (buy_spreads, sell_spreads): two lists of length n_levels
            buy_spreads: negative percentages, e.g. [-0.1, -0.8, ..., -5.0]
                         index 0 = tightest (closest to mid)
            sell_spreads: positive percentages, e.g. [0.3, 1.2, ..., 7.0]
                          index 0 = tightest (closest to mid)
        """
        agg = clip(aggressiveness, 0.0, 1.0)
        buy = self._compute_side_levels(
            tightest=self.cfg.buy_max_pct,   # e.g. -0.1% (closest to mid)
            widest=self.cfg.buy_min_pct,     # e.g. -5.0% (furthest from mid)
            agg=agg,
            n=n_levels,
        )
        sell = self._compute_side_levels(
            tightest=self.cfg.sell_min_pct,  # e.g. +0.3%
            widest=self.cfg.sell_max_pct,    # e.g. +7.0%
            agg=agg,
            n=n_levels,
        )

        # Enforce minimum profitable spread:
        # The total spread (sell[0] - buy[0]) must be >= min_profitable_spread_bps / 100
        if self.min_profitable_spread_bps > 0 and buy and sell:
            total_spread_pct = sell[0] - buy[0]  # buy is negative, sell is positive
            min_spread_pct = self.min_profitable_spread_bps / 100.0
            if total_spread_pct < min_spread_pct:
                # Widen symmetrically to reach minimum profitable spread
                deficit = min_spread_pct - total_spread_pct
                buy[0] -= deficit / 2.0
                sell[0] += deficit / 2.0

        return buy, sell

    def _compute_side_levels(
        self,
        tightest: float,
        widest: float,
        agg: float,
        n: int,
        curve_strength: float | None = None,
    ) -> list[float]:
        """
        Generate n spread values between tightest and widest.

        At agg=1: levels cluster tightly near `tightest`
        At agg=0: levels cluster widely near `widest`
        At agg=0.5: levels are evenly spaced

        Uses gamma = exp(curve_strength * (1 - 2 * agg)) to map aggressiveness
        onto the power-curve exponent (from Huy's model):
          - agg=0 → gamma = exp(curve_strength) → steep curve, wide clustering
          - agg=1 → gamma = exp(-curve_strength) → shallow curve, tight clustering
          - agg=0.5 → gamma = 1 → linear (even spacing)

        Args:
            curve_strength: per-side override; None → use self.cfg.curve_strength
        """
        cs = curve_strength if curve_strength is not None else self.cfg.curve_strength
        gamma = np.exp(cs * (2.0 * agg - 1.0))
        t = np.linspace(0.0, 1.0, n)
        # Level 0 = tightest, level n-1 = widest
        levels = tightest + (widest - tightest) * (t ** gamma)
        return levels.tolist()

    def compute_levels_dual(
        self,
        buy_aggressiveness: float,
        sell_aggressiveness: float,
        n_levels: int,
    ) -> tuple[list[float], list[float]]:
        """
        Compute buy and sell spread levels using independent aggressiveness values per side.
        Use this instead of compute_levels() when buy/sell aggressiveness differ (e.g. ZZ regime).
        """
        buy = self._compute_side_levels(
            tightest=self.cfg.buy_max_pct,
            widest=self.cfg.buy_min_pct,
            agg=clip(buy_aggressiveness, 0.0, 1.0),
            n=n_levels,
        )
        sell = self._compute_side_levels(
            tightest=self.cfg.sell_min_pct,
            widest=self.cfg.sell_max_pct,
            agg=clip(sell_aggressiveness, 0.0, 1.0),
            n=n_levels,
        )
        return buy, sell

    def prices_from_spreads(
        self,
        global_mid: float,
        buy_spreads: list[float],
        sell_spreads: list[float],
    ) -> tuple[list[float], list[float]]:
        """
        Convert spread percentages to absolute prices using the global mid-price.

        Args:
            global_mid: global reference price (weighted average across exchanges)
            buy_spreads: list of negative pct values
            sell_spreads: list of positive pct values

        Returns:
            (buy_prices, sell_prices): lists of absolute prices
        """
        buy_prices = [global_mid * (1.0 + s / 100.0) for s in buy_spreads]
        sell_prices = [global_mid * (1.0 + s / 100.0) for s in sell_spreads]
        return buy_prices, sell_prices

    # ------------------------------------------------------------------
    # Per-side control (Huy Phase 1 formulas)
    # ------------------------------------------------------------------

    @staticmethod
    def enforce_min_step(levels: list[float], min_step: float) -> list[float]:
        """
        Walk outward from level 0, ensuring consecutive levels are at least
        ``min_step`` apart in absolute value.

        For sell (positive) spreads: pushes levels further positive.
        For buy (negative) spreads: pushes levels further negative.
        """
        if min_step <= 0 or len(levels) < 2:
            return levels
        result = list(levels)
        for i in range(1, len(result)):
            if result[i - 1] > 0:
                min_required = result[i - 1] + min_step
            else:
                min_required = result[i - 1] - min_step
            if abs(result[i] - result[i - 1]) < min_step:
                result[i] = min_required
        return result

    def compute_levels_huy(
        self,
        buy_agg: float,
        sell_agg: float,
        buy_levels: int,
        sell_levels: int,
        buy_curve_strength: float,
        sell_curve_strength: float,
        buy_min_step: float = 0.0,
        sell_min_step: float = 0.0,
    ) -> tuple[list[float], list[float]]:
        """
        Compute spread levels with full per-side control.

        Extends ``compute_levels_dual`` with independent level counts,
        curve strengths, and minimum step enforcement per side.

        Args:
            buy_agg / sell_agg: aggressiveness per side ∈ [0, 1]
            buy_levels / sell_levels: number of orders per side
            buy_curve_strength / sell_curve_strength: power-curve exponent per side
            buy_min_step / sell_min_step: minimum % gap between consecutive levels

        Returns:
            (buy_spreads, sell_spreads) — lists may have different lengths.
        """
        buy = self._compute_side_levels(
            tightest=self.cfg.buy_max_pct,
            widest=self.cfg.buy_min_pct,
            agg=clip(buy_agg, 0.0, 1.0),
            n=buy_levels,
            curve_strength=buy_curve_strength,
        )
        sell = self._compute_side_levels(
            tightest=self.cfg.sell_min_pct,
            widest=self.cfg.sell_max_pct,
            agg=clip(sell_agg, 0.0, 1.0),
            n=sell_levels,
            curve_strength=sell_curve_strength,
        )
        if buy_min_step > 0:
            buy = self.enforce_min_step(buy, buy_min_step)
        if sell_min_step > 0:
            sell = self.enforce_min_step(sell, sell_min_step)
        return buy, sell
