"""
Random agent — v1 placeholder.
Returns aggressiveness_override=None on every tick, meaning the quant model's
value is always used unchanged.

This agent exists so the agent interface is exercised in production from day 1,
making the v2 swap-in straightforward.
"""

from __future__ import annotations

from agents.base_agent import AgentObservation, AgentAction, BaseAgent
from utils.logging import get_logger

log = get_logger("random_agent")


class PassthroughAgent(BaseAgent):
    """
    Does nothing — defers all aggressiveness decisions to the quant model.
    Default agent for v1.
    """

    def observe(self, obs: AgentObservation) -> AgentAction:
        # Always return None override → quant model value is used
        return AgentAction(aggressiveness_override=None)

    def record_reward(self, pnl_delta: float, fill_delta: float) -> None:
        # No learning in v1
        pass
