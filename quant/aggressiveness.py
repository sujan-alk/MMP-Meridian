"""
Volatility → Aggressiveness mapping.

Maps the rolling volatility value to an aggressiveness scalar ∈ [0.0, 1.0]:
  - 1.0 = fully aggressive (tight spreads, cluster orders near mid-price)
  - 0.0 = fully passive (wide spreads, cluster orders away from mid-price)

Formula (from Huy's Meta Config V1):
  vol <= low_threshold   → agg = 1.0
  vol >= high_threshold  → agg = 0.0
  between               → agg = 1 - ((vol - low) / (high - low))^power
"""

from __future__ import annotations

from config.schema import VolatilityConfig
from utils.math_utils import clip


class AggressivenessModel:
    """
    Converts a volatility reading into an aggressiveness value.

    Aggressiveness is used independently for both spread and depth:
    - SpreadEngine uses it to shift where levels cluster (tight vs wide)
    - DepthEngine uses it to blend between passive and equal distributions
    """

    def __init__(self, config: VolatilityConfig):
        self.cfg = config

    def compute(self, vol: float) -> float:
        """
        Map volatility → aggressiveness ∈ [0.0, 1.0].

        Args:
            vol: Rolling volatility (e.g. 0.002 = 0.2% standard deviation of returns)

        Returns:
            float: Aggressiveness value ∈ [0.0, 1.0]
        """
        lo = self.cfg.low_threshold
        hi = self.cfg.high_threshold
        p = self.cfg.power

        if vol <= lo:
            return 1.0
        if vol >= hi:
            return 0.0

        # Normalise to [0, 1] within the threshold window
        normalised = (vol - lo) / (hi - lo)
        # Apply power curve: gentle decay at low vol, steeper at high vol
        agg = 1.0 - (normalised ** p)
        return clip(agg, 0.0, 1.0)

    def compute_with_regime(self, vol: float, regime: str) -> tuple[float, float]:
        """
        Return separate aggressiveness values for buy and sell sides,
        adjusted by the Zhang-Zhang regime (Phase 1b).

        trending_up   → more aggressive selling (ride the pump), normal buying
        trending_down → more aggressive buying (accumulate), reduce selling
        choppy        → equal aggressiveness on both sides

        Returns:
            (buy_aggressiveness, sell_aggressiveness) both ∈ [0.0, 1.0]
        """
        base_agg = self.compute(vol)

        if regime == "trending_up":
            buy_agg = clip(base_agg * 0.8, 0.0, 1.0)   # slightly less aggressive buying
            sell_agg = clip(base_agg * 1.1, 0.0, 1.0)  # more aggressive selling
        elif regime == "trending_down":
            buy_agg = clip(base_agg * 1.1, 0.0, 1.0)   # more aggressive buying (accumulate)
            sell_agg = clip(base_agg * 0.8, 0.0, 1.0)  # less aggressive selling
        else:
            # choppy / unknown — symmetric
            buy_agg = base_agg
            sell_agg = base_agg

        return buy_agg, sell_agg

    def compute_with_hmm_regime(
        self,
        vol: float,
        zz_regime: str,
        hmm_regime: str,
        hmm_confidence: float,
    ) -> tuple[float, float]:
        """
        Return (buy_agg, sell_agg) adjusted by both ZZ regime and HMM regime.

        The HMM regime applies a secondary overlay on top of ZZ-regime adjustments.
        At low confidence (<0.5), HMM adjustments blend toward neutral to avoid
        false regime switches.

        Critical behavior for HIGH_VOL_CRASH: buy-side aggressiveness is FLOORED
        (never goes passive) to maintain absorptive liquidity during sell-offs.

        Returns:
            (buy_aggressiveness, sell_aggressiveness) both ∈ [0.0, 1.0]
        """
        # Start from ZZ-regime adjusted values
        zz_buy, zz_sell = self.compute_with_regime(vol, zz_regime)
        hmm_cfg = self.cfg.hmm

        if hmm_regime == "LOW_VOL":
            buy_agg = zz_buy * 1.1
            sell_agg = zz_sell * 1.1
        elif hmm_regime == "HIGH_VOL":
            buy_agg = zz_buy * 0.7
            sell_agg = zz_sell * 0.6
        elif hmm_regime == "HIGH_VOL_CRASH":
            # CRITICAL: floor buy-side, ceiling sell-side.
            # Normal vol→agg drives both to ~0 in crashes. The floor guarantees
            # buy-side presence to absorb selling pressure and slow cascading.
            buy_agg = max(zz_buy, hmm_cfg.crash_buy_agg_floor)
            sell_agg = min(zz_sell, hmm_cfg.crash_sell_agg_ceiling)
        elif hmm_regime == "RECOVERY":
            buy_agg = zz_buy * hmm_cfg.recovery_buy_agg_boost
            sell_agg = zz_sell * hmm_cfg.recovery_sell_agg_dampen
        else:
            # NORMAL or unknown — pass through ZZ values
            return zz_buy, zz_sell

        # Confidence blending: at low confidence, lerp back toward ZZ-only values
        # to avoid false regime switches jerking aggressiveness
        if hmm_confidence < 0.5:
            blend = hmm_confidence / 0.5
            buy_agg = zz_buy + blend * (buy_agg - zz_buy)
            sell_agg = zz_sell + blend * (sell_agg - zz_sell)

        return clip(buy_agg, 0.0, 1.0), clip(sell_agg, 0.0, 1.0)
