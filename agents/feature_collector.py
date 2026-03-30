"""
Feature Collector — gathers the observation vector for the RL agent each tick.
Also persists features to the rl_features SQLite table for future training.
"""

from __future__ import annotations

from agents.base_agent import AgentObservation
from db.database import Database
from db.queries import insert_rl_features, get_fill_rate, get_recent_pnl
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("feature_collector")


class FeatureCollector:
    """
    Builds AgentObservation from current bot state and writes to DB.
    One instance shared across all exchange bots (writes global-scope features).
    """

    def __init__(self, db: Database):
        self.db = db

    async def collect(
        self,
        vol_simple: float,
        vol_zz: float,
        zz_regime: str,
        aggressiveness: float,
        skew_factor: float,
        token_drift_pct: float,
        global_mid: float,
        buy_spread_l1: float | None,
        sell_spread_l1: float | None,
    ) -> AgentObservation:
        """
        Build and return an AgentObservation from current state.
        Also persists the features to the rl_features table.
        """
        fill_rate = await get_fill_rate(self.db)
        pnl_1h = await get_recent_pnl(self.db, window_s=3600.0)

        # Compute bid/ask spread in basis points
        spread_bps = 0.0
        if buy_spread_l1 is not None and sell_spread_l1 is not None:
            # buy_spread_l1 is negative, sell_spread_l1 is positive
            spread_bps = (sell_spread_l1 - buy_spread_l1) * 100.0  # pct → bps

        # Persist to DB for future training
        await insert_rl_features(
            self.db,
            vol_simple=vol_simple,
            vol_zz=vol_zz if vol_zz else None,
            zz_regime=zz_regime,
            aggressiveness=aggressiveness,
            global_mid=global_mid,
            buy_spread_l1=buy_spread_l1,
            sell_spread_l1=sell_spread_l1,
            skew_factor=skew_factor,
            fill_rate_1m=fill_rate,
            pnl_1h=pnl_1h,
        )

        return AgentObservation(
            vol_simple=vol_simple,
            vol_zz=vol_zz or 0.0,
            zz_regime=zz_regime,
            aggressiveness_current=aggressiveness,
            skew_factor=skew_factor,
            token_drift_pct=token_drift_pct,
            global_mid=global_mid,
            bid_ask_spread_bps=spread_bps,
            fill_rate_1m=fill_rate,
            pnl_1h=pnl_1h,
        )
