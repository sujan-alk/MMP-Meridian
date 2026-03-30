"""
Abstract RL agent interface — v1 scaffold only.
In v1, RandomAgent is used (returns no override).
In v2, a PPO/SAC agent will be trained on the rl_features table.

The agent receives an AgentObservation each tick and optionally overrides
the aggressiveness value computed by the quant model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentObservation:
    """
    Feature vector passed to the RL agent each tick.
    Populated by FeatureCollector and passed to agent.observe().
    """
    # Volatility features
    vol_simple: float               # Rolling std dev (simple model)
    vol_zz: float                   # Zhang-Zhang vol estimate
    zz_regime: str                  # "trending_up" | "trending_down" | "choppy"

    # Current quant model output
    aggressiveness_current: float   # What the quant model computed

    # Inventory state
    skew_factor: float              # ∈ [0.5, 2.0]
    token_drift_pct: float          # % drift from initial token balance

    # Market microstructure
    global_mid: float
    bid_ask_spread_bps: float       # Best bid/ask spread in basis points

    # Recent performance
    fill_rate_1m: float             # Fills per minute (rolling)
    pnl_1h: float                   # 1-hour rolling realized P&L


@dataclass
class AgentAction:
    """
    Agent output. aggressiveness_override=None means accept the quant model's value.
    """
    aggressiveness_override: Optional[float] = None  # ∈ [0.0, 1.0] if overriding


class BaseAgent(ABC):
    """
    Abstract RL agent. Implement this to override aggressiveness with a learned policy.
    """

    @abstractmethod
    def observe(self, obs: AgentObservation) -> AgentAction:
        """
        Given an observation, return an action.
        The ExchangeBot applies action.aggressiveness_override if not None.
        """

    @abstractmethod
    def record_reward(self, pnl_delta: float, fill_delta: float) -> None:
        """
        Record the outcome of the last action (for online learning or replay buffer).
        pnl_delta: change in realized P&L since last action
        fill_delta: change in fill rate since last action
        """

    def save(self, path: str) -> None:
        """Persist model weights to disk."""

    def load(self, path: str) -> None:
        """Load model weights from disk."""
