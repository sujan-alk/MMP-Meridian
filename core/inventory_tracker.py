"""
Inventory Tracker — monitors balance drift from initial state.

Huy's model tracks initial balances (Alchemy tokens + USD) and adjusts
the buy/sell side aggressiveness based on how far the current balance
has drifted from the initial state.

Key insight from the meeting:
  "The quant model has a memory of our initial balance.
   If we start at 1M ALKIMI and 1M USD, and we're now at 800k ALKIMI + 1.2M USD,
   that means we've sold ALKIMI. So the bot prioritises buying it back."

skew_factor ∈ [0.5, 2.0]:
  1.0 = balanced (no drift)
  > 1.0 = token deficit → increase buy budget, decrease sell budget
  < 1.0 = token surplus → decrease buy budget, increase sell budget
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.schema import ExchangeBotConfig
from exchange.base import Balance
from utils.logging import get_logger
from utils.math_utils import clip

log = get_logger("inventory_tracker")


@dataclass
class InventoryState:
    """Snapshot of current inventory state."""
    usd: float
    token: float
    initial_usd: float
    initial_token: float
    usd_drift_pct: float    # positive = have MORE USD than initial (sold tokens)
    token_drift_pct: float  # positive = have MORE tokens than initial (bought tokens)
    skew_factor: float      # ∈ [0.5, 2.0]


class InventoryTracker:
    """
    Tracks ALKIMI + USD balance drift and produces a skew_factor
    that the DepthEngine uses to bias buy/sell order sizes.
    """

    def __init__(self, config: ExchangeBotConfig, max_position_tokens: float = 0.0, max_position_usd: float = 0.0, min_position_tokens: float = 0.0):
        self.config = config
        self.exchange = config.exchange
        self._initial_usd: Optional[float] = None
        self._initial_token: Optional[float] = None
        self._current_usd: float = 0.0
        self._current_token: float = 0.0
        self._drift_threshold: float = 0.10  # 10% drift triggers meaningful skew
        self.max_position_tokens = max_position_tokens
        self.max_position_usd = max_position_usd
        self.min_position_tokens = min_position_tokens

    def record_initial(self, balance: Balance) -> None:
        """
        Record the baseline balance. Called once on bot startup.
        If initial_balances is configured in bot.json, use that instead.
        """
        self._initial_usd = balance.usd
        self._initial_token = balance.token
        log.info(
            "inventory_initial_recorded",
            exchange=self.exchange,
            initial_usd=balance.usd,
            initial_token=balance.token,
        )

    def update(self, balance: Balance) -> None:
        """Update current balance. Call on every tick (after fetching balance)."""
        if self._initial_usd is None:
            self.record_initial(balance)
        self._current_usd = balance.usd
        self._current_token = balance.token

    def skew_factor(self) -> float:
        """
        Compute the skew factor ∈ [0.5, 2.0].

        Logic:
          - Token drift = (current_token - initial_token) / initial_token
          - Positive token drift → bought more → slightly reduce buy budget
          - Negative token drift → sold tokens → increase buy budget

        The skew is applied to the buy side:
          skew_factor > 1 → more buy budget (need to accumulate tokens)
          skew_factor < 1 → more sell budget (token-heavy, need to rebalance to USD)
        """
        if self._initial_token is None or self._initial_token <= 0:
            return 1.0

        token_drift = (self._current_token - self._initial_token) / self._initial_token
        # Negative token_drift (sold tokens) → skew_factor > 1 (buy more)
        # Positive token_drift (bought tokens) → skew_factor < 1 (sell more)
        raw_skew = 1.0 - (token_drift * 2.0)
        return clip(raw_skew, 0.5, 2.0)

    def state(self) -> InventoryState:
        """Return a full snapshot of inventory state for logging/API."""
        init_usd = self._initial_usd or 0.0
        init_token = self._initial_token or 1.0  # avoid division by zero

        usd_drift = (self._current_usd - init_usd) / max(init_usd, 1.0)
        token_drift = (self._current_token - init_token) / init_token

        return InventoryState(
            usd=self._current_usd,
            token=self._current_token,
            initial_usd=init_usd,
            initial_token=init_token,
            usd_drift_pct=usd_drift * 100.0,
            token_drift_pct=token_drift * 100.0,
            skew_factor=self.skew_factor(),
        )

    def should_rebalance(self) -> bool:
        """
        True if token drift exceeds the rebalance threshold.
        Used as an additional trigger to repost orders (beyond the 1s periodic check).
        """
        if self._initial_token is None or self._initial_token <= 0:
            return False
        token_drift = abs(self._current_token - self._initial_token) / self._initial_token
        return token_drift >= self._drift_threshold

    def is_position_limit_reached(self, side: str, current_tokens: float, token_price: float) -> bool:
        """
        Check if position limits would be breached.

        Args:
            side: "buy" or "sell"
            current_tokens: current token balance
            token_price: current token price in USD

        Returns:
            True if the position limit for the given side is reached.
        """
        if self.max_position_usd <= 0 and self.max_position_tokens <= 0:
            return False  # No limits configured

        position_usd = current_tokens * token_price

        if side == "buy" and self.max_position_usd > 0 and position_usd >= self.max_position_usd:
            log.warning("position_limit_reached", side=side, position_usd=position_usd, max_usd=self.max_position_usd)
            return True
        if side == "sell" and self.min_position_tokens > 0 and current_tokens <= self.min_position_tokens:
            log.warning("position_limit_reached", side=side, current_tokens=current_tokens, min_tokens=self.min_position_tokens)
            return True
        return False

    def set_initial_from_config(self, usd: float, token: float) -> None:
        """Override the auto-detected initial balance with configured values."""
        self._initial_usd = usd
        self._initial_token = token
        log.info(
            "inventory_initial_from_config",
            exchange=self.exchange,
            initial_usd=usd,
            initial_token=token,
        )
