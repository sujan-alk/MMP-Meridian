"""
Depth Engine — distributes total USD budget across order levels.

Based on Huy's Meta Config V1 blended distribution:

  passive  = geometric decay  → front-loaded (most size near mid)
  equal    = uniform          → same size at every level
  blend    = aggressiveness ^ curve_strength
  amounts  = (1 - blend) * passive + blend * equal

At agg=0 (passive mode): heavy concentration near mid — safer, protects against
  institutional sweep of whole book (they get worse average price).
At agg=1 (aggressive mode): equal amounts at every level — incentivises trading
  by showing large depth uniformly.

skew_factor adjusts total budget between buy and sell sides based on inventory drift.
depth_mult (regime multiplier from RegimeMaster) scales total budget up/down.
"""

from __future__ import annotations

import numpy as np

from config.schema import DepthConfig
from utils.math_utils import clip, geometric_decay


class DepthEngine:
    """
    Distributes total budget across n order levels using aggressiveness-blended distribution.
    """

    def __init__(self, config: DepthConfig):
        self.cfg = config

    def compute_amounts(
        self,
        aggressiveness: float,
        n_levels: int,
        skew_factor: float = 1.0,
        side: str = "buy",
        min_step_usd: float = 0.0,
        depth_mult: float = 1.0,
    ) -> list[float]:
        """
        Compute USD amounts for each order level.

        Args:
            aggressiveness: float ∈ [0.0, 1.0]
            n_levels: number of order levels
            skew_factor: float ∈ [0.5, 2.0]
                > 1.0 → increase buy budget (token deficit, need to buy more)
                < 1.0 → increase sell budget (token surplus, need to sell more)
            side: "buy" | "sell"
            min_step_usd: minimum USD decrement between consecutive levels
                (level 0 = largest). 0.0 = disabled.
            depth_mult: regime multiplier for order size (< 1.0 reduces size in risky regimes).
                        Supplied by the ExchangeBot from RegimeState.mm_params.

        Returns:
            list[float]: USD amount per level, length n_levels
            Smallest amounts are filtered to cfg.min_order_usd.
        """
        agg = clip(aggressiveness, 0.0, 1.0)
        n = n_levels
        mult = max(0.0, float(depth_mult))

        # Passive distribution: geometric decay (most size near mid = level 0)
        passive = geometric_decay(n, decay=0.8)

        # Equal distribution: uniform across all levels
        equal = np.ones(n) / n

        # Blend coefficient
        blend = agg ** self.cfg.curve_strength
        ratio = (1.0 - blend) * passive + blend * equal

        # Total budget for this side, adjusted by inventory skew and regime depth_mult
        half_budget = self.cfg.total_budget_usd / 2.0 * mult
        if side == "buy":
            side_budget = half_budget * clip(skew_factor, 0.5, 2.0)
        else:
            side_budget = half_budget * clip(2.0 - skew_factor, 0.5, 2.0)

        raw_amounts = ratio * side_budget

        # Enforce minimum USD decrement between consecutive levels
        # (level 0 = closest to mid = largest; levels decrease outward)
        if min_step_usd > 0 and n > 1:
            for i in range(1, n):
                min_val = raw_amounts[i - 1] - min_step_usd
                if raw_amounts[i] > min_val:
                    raw_amounts[i] = max(min_val, 0.0)

        # Apply minimum order size, then cap total back to side_budget.
        # (min_order × n_levels can exceed side_budget on tight budgets.)
        amounts = np.maximum(raw_amounts, self.cfg.min_order_usd)
        total_after_min = float(amounts.sum())
        if total_after_min > side_budget:
            scale = side_budget / total_after_min
            scaled = amounts * scale
            # Only apply the scale if every level still meets min_order after scaling
            if float(scaled.min()) >= self.cfg.min_order_usd:
                amounts = scaled

        # Final safety floor to prevent floating-point drift below min_order_usd
        amounts = np.maximum(amounts, self.cfg.min_order_usd)

        return amounts.tolist()

    def usd_to_token_amount(self, usd_amount: float, price: float) -> float:
        """Convert USD amount to token amount at the given price."""
        if price <= 0:
            return 0.0
        return usd_amount / price
