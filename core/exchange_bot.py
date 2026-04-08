"""
Exchange Bot — per-exchange market making tick loop.

One ExchangeBot runs per exchange (KuCoin, Gate, MEXC, Kraken).
It receives a GlobalState from the Orchestrator via an asyncio.Queue
and manages the order grid for its exchange.

Tick sequence:
1. Receive GlobalState (global_mid + volatility + aggressiveness)
2. Fetch balance (cached, refreshed every BALANCE_CACHE_S seconds)
3. Safety checks: Q-Switch + CircuitBreaker
4. Update InventoryTracker → skew_factor
5. Compute spread levels + amounts from quant model
6. OrderManager.diff_and_repost → cancel stale, place new
7. Record inventory snapshot + RL features to DB
8. Emit TICK_UPDATE event to WebSocket feed
9. Heartbeat.beat()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config.schema import ExchangeBotConfig
from core.inventory_tracker import InventoryTracker
from core.order_manager import OrderGrid, OrderManager
from db.database import Database
from db.queries import insert_fill, insert_inventory_snapshot, insert_rl_features, get_fill_rate, get_recent_pnl
from exchange.base import BaseConnector, Balance
from quant.aggressiveness import AggressivenessModel
from quant.depth_engine import DepthEngine
from quant.spread_engine import SpreadEngine
from safety.circuit_breaker import CircuitBreaker
from safety.heartbeat import Heartbeat
from safety.q_switch import QSwitch
from safety.rate_limiter import RateLimiter
from utils.logging import get_logger
from utils.time_utils import now_s

if TYPE_CHECKING:
    from api.websocket import LiveFeed
    from core.orchestrator import GlobalState

log = get_logger("exchange_bot")

BALANCE_CACHE_S = 10.0          # Re-fetch balance at most every 10 seconds
SNAPSHOT_INTERVAL_S = 30.0      # Write inventory snapshot every 30 seconds
RL_FEATURE_INTERVAL_S = 10.0    # Write RL features every 10 seconds
FILL_POLL_INTERVAL_S = 5.0      # Poll exchange for fills every 5 seconds


class ExchangeBot:
    """
    Per-exchange market making bot.
    Instantiated and managed by the Orchestrator.
    """

    def __init__(
        self,
        config: ExchangeBotConfig,
        connector: BaseConnector,
        queue: asyncio.Queue,
        db: Database,
        live_feed: "LiveFeed",
        agg_model: AggressivenessModel,
        live_mode: bool = False,
    ):
        self.config = config
        self.exchange = config.exchange
        self.connector = connector
        self.queue = queue
        self.db = db
        self.live_feed = live_feed
        self.live_mode = live_mode

        self.rate_limiter = RateLimiter(config.safety.max_requests_per_second, exchange=self.exchange)
        self.q_switch = QSwitch(config.safety, exchange=self.exchange)
        self.heartbeat = Heartbeat(config.safety, exchange=self.exchange)
        self.circuit_breaker = CircuitBreaker(config.safety, exchange=self.exchange)
        self.inventory = InventoryTracker(config)
        self.spread_engine = SpreadEngine(config.spread)
        self.depth_engine = DepthEngine(config.depth)
        self.agg_model = agg_model
        self.order_manager = OrderManager(connector, config, db, self.rate_limiter, live_mode)

        self._running = False
        self._balance: Balance | None = None
        self._balance_cached_at: float = 0.0
        self._last_snapshot_at: float = 0.0
        self._last_rl_at: float = 0.0
        self._last_fill_poll_at: float = 0.0
        self._last_fill_ts: float | None = None   # timestamp of most recent fill seen
        self._last_global_state: "GlobalState | None" = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to exchange and start the tick loop."""
        log.info("exchange_bot_starting", exchange=self.exchange)
        await self.connector.connect()
        await self._warmup()
        self._running = True
        await self.heartbeat.start(on_failure=self._emergency_stop)
        log.info("exchange_bot_started", exchange=self.exchange)
        await self._run_loop()

    async def stop(self) -> None:
        """Graceful shutdown: cancel orders and disconnect."""
        log.info("exchange_bot_stopping", exchange=self.exchange)
        self._running = False
        await self.heartbeat.stop()
        await self.order_manager.cancel_all()
        await self.connector.disconnect()
        log.info("exchange_bot_stopped", exchange=self.exchange)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            try:
                # Wait for a GlobalState update from the Orchestrator (max 5s)
                state: "GlobalState" = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                await self._tick(state)
                self.heartbeat.beat()
            except asyncio.TimeoutError:
                log.warning("exchange_bot_no_state", exchange=self.exchange)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("exchange_bot_tick_error", exchange=self.exchange, error=str(exc), exc_info=True)
                if not self.connector.is_connected:
                    await self._try_reconnect()

    async def _tick(self, state: "GlobalState") -> None:
        """Process one tick: update quant model + manage orders."""
        self._last_global_state = state

        # 1. Fetch balance (cached)
        balance = await self._get_balance()
        if balance is None:
            return

        # 2. Safety checks
        if self.q_switch.check(balance):
            log.warning("tick_skipped_q_switch", exchange=self.exchange)
            return
        self.circuit_breaker.record_equity(balance.usd, balance.token, state.global_mid)
        if self.circuit_breaker.is_tripped():
            log.warning("tick_skipped_circuit_breaker", exchange=self.exchange,
                        reason=self.circuit_breaker.trip_reason)
            return

        # 3. Inventory tracking
        self.inventory.update(balance)
        skew = self.inventory.skew_factor()

        # 4. Compute order grid using the quant model
        # Base aggressiveness scaling (Huy formula):
        # scale raw values by user-defined ceiling (default 1.0 = no change)
        # Use HMM-enhanced aggressiveness when HMM regime is active
        if state.hmm_regime and state.hmm_regime != "NORMAL":
            raw_buy_agg, raw_sell_agg = self.agg_model.compute_with_hmm_regime(
                state.volatility, state.zz_regime,
                state.hmm_regime, state.hmm_regime_confidence,
            )
        else:
            raw_buy_agg, raw_sell_agg = self.agg_model.compute_with_regime(
                state.volatility, state.zz_regime
            )
        base = self.agg_model.cfg.base_aggressiveness
        buy_agg = raw_buy_agg * base
        sell_agg = raw_sell_agg * base

        # Resolve per-side params (fall back to shared defaults when None)
        buy_n = self.config.spread.buy_levels or self.config.depth.levels
        sell_n = self.config.spread.sell_levels or self.config.depth.levels
        buy_cs = self.config.spread.buy_curve_strength or self.config.spread.curve_strength
        sell_cs = self.config.spread.sell_curve_strength or self.config.spread.curve_strength

        # Spread computation with full per-side control
        buy_spreads, sell_spreads = self.spread_engine.compute_levels_huy(
            buy_agg=buy_agg,
            sell_agg=sell_agg,
            buy_levels=buy_n,
            sell_levels=sell_n,
            buy_curve_strength=buy_cs,
            sell_curve_strength=sell_cs,
            buy_min_step=self.config.spread.buy_min_step,
            sell_min_step=self.config.spread.sell_min_step,
        )

        # Price conversion with tick rounding (Huy formula)
        tick = self.config.spread.tick_size
        buy_prices = [
            round(state.global_mid * (1.0 + s / 100.0) / tick) * tick
            for s in buy_spreads
        ]
        sell_prices = [
            round(state.global_mid * (1.0 + s / 100.0) / tick) * tick
            for s in sell_spreads
        ]

        # Depth with min_step_usd enforcement
        buy_usd_amounts = self.depth_engine.compute_amounts(
            buy_agg, buy_n, skew, "buy",
            min_step_usd=self.config.depth.min_step_usd,
        )
        sell_usd_amounts = self.depth_engine.compute_amounts(
            sell_agg, sell_n, skew, "sell",
            min_step_usd=self.config.depth.min_step_usd,
        )

        # HIGH_VOL_CRASH: shift capital from sell to buy side for absorptive floor
        if state.hmm_regime == "HIGH_VOL_CRASH":
            hmm_cfg = self.agg_model.cfg.hmm
            buy_usd_amounts = [amt * hmm_cfg.crash_buy_depth_multiplier for amt in buy_usd_amounts]
            sell_usd_amounts = [amt * hmm_cfg.crash_sell_depth_factor for amt in sell_usd_amounts]
            log.info(
                "crash_depth_override",
                exchange=self.exchange,
                buy_multiplier=hmm_cfg.crash_buy_depth_multiplier,
                sell_factor=hmm_cfg.crash_sell_depth_factor,
            )

        buy_token_amounts = [
            self.depth_engine.usd_to_token_amount(usd, p)
            for usd, p in zip(buy_usd_amounts, buy_prices)
        ]
        sell_token_amounts = [
            self.depth_engine.usd_to_token_amount(usd, p)
            for usd, p in zip(sell_usd_amounts, sell_prices)
        ]

        grid = OrderGrid(
            buy_prices=buy_prices,
            buy_amounts=buy_token_amounts,
            sell_prices=sell_prices,
            sell_amounts=sell_token_amounts,
            global_mid=state.global_mid,
            aggressiveness=(buy_agg + sell_agg) / 2.0,
        )

        # 5. Diff-and-repost orders
        placed = await self.order_manager.diff_and_repost(grid)

        # 6. Poll for fills periodically
        ts = now_s()
        if ts - self._last_fill_poll_at >= FILL_POLL_INTERVAL_S:
            await self._ingest_fills()
            self._last_fill_poll_at = ts

        # 7. Periodic DB writes
        if ts - self._last_snapshot_at >= SNAPSHOT_INTERVAL_S:
            await insert_inventory_snapshot(
                self.db, self.exchange, balance.usd, balance.token,
                state.global_mid, state.volatility, grid.aggressiveness, skew,
            )
            self._last_snapshot_at = ts

        if ts - self._last_rl_at >= RL_FEATURE_INTERVAL_S:
            fill_rate = await get_fill_rate(self.db)
            pnl_1h = await get_recent_pnl(self.db, window_s=3600.0)
            await insert_rl_features(
                self.db,
                vol_simple=state.volatility,
                vol_zz=state.zz_vol,
                zz_regime=state.zz_regime,
                aggressiveness=grid.aggressiveness,
                global_mid=state.global_mid,
                buy_spread_l1=buy_spreads[0] if buy_spreads else None,
                sell_spread_l1=sell_spreads[0] if sell_spreads else None,
                skew_factor=skew,
                fill_rate_1m=fill_rate,
                pnl_1h=pnl_1h,
                hmm_regime=state.hmm_regime,
                hmm_confidence=state.hmm_regime_confidence,
            )
            self._last_rl_at = ts

        # 8. Emit WebSocket event (includes order grid for UI dashboard)
        await self.live_feed.emit_tick(
            exchange=self.exchange,
            global_mid=state.global_mid,
            volatility=state.volatility,
            aggressiveness=grid.aggressiveness,
            skew_factor=skew,
            open_orders=self.order_manager.open_order_count,
            placed_count=len(placed),
            regime=state.zz_regime,
            hmm_regime=state.hmm_regime,
            hmm_regime_confidence=state.hmm_regime_confidence,
            buy_prices=buy_prices,
            sell_prices=sell_prices,
            buy_amounts=buy_usd_amounts,
            sell_amounts=sell_usd_amounts,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _warmup(self) -> None:
        """Fetch initial balance and set up inventory baseline."""
        try:
            await self.rate_limiter.acquire()
            balance = await self.connector.fetch_balance()
            self._balance = balance
            self._balance_cached_at = now_s()

            # Apply configured initial balance override if set
            if hasattr(self.config, "_initial_balance"):
                init = self.config._initial_balance
                self.inventory.set_initial_from_config(init.usd, init.token)
            else:
                self.inventory.record_initial(balance)

            log.info(
                "exchange_bot_warmup",
                exchange=self.exchange,
                usd=balance.usd,
                token=balance.token,
            )
        except Exception as exc:
            log.error("exchange_bot_warmup_failed", exchange=self.exchange, error=str(exc))

    async def _get_balance(self) -> Balance | None:
        """Return cached balance, refreshing if stale."""
        if self._balance is None or now_s() - self._balance_cached_at > BALANCE_CACHE_S:
            try:
                await self.rate_limiter.acquire()
                self._balance = await self.connector.fetch_balance()
                self._balance_cached_at = now_s()
            except Exception as exc:
                log.warning("fetch_balance_failed", exchange=self.exchange, error=str(exc))
                return self._balance  # Return stale if we have it
        return self._balance

    async def _ingest_fills(self) -> None:
        """Fetch recent fills from the exchange and persist them to the database."""
        since = (self._last_fill_ts + 0.001) if self._last_fill_ts is not None else (now_s() - 3600.0)
        try:
            await self.rate_limiter.acquire()
            fills = await self.connector.fetch_fills(since_ts=since)
        except Exception as exc:
            log.warning("fill_ingestion_failed", exchange=self.exchange, error=str(exc))
            return
        for fill in fills:
            await insert_fill(self.db, fill)
            if self._last_fill_ts is None or fill.timestamp > self._last_fill_ts:
                self._last_fill_ts = fill.timestamp
        if fills:
            log.info("fills_ingested", exchange=self.exchange, count=len(fills))

    async def _try_reconnect(self) -> None:
        """Attempt to reconnect after a connection drop. Triggers Q-Switch if it fails."""
        log.warning("exchange_bot_reconnecting", exchange=self.exchange)
        try:
            await self.connector.reconnect()
            log.info("exchange_bot_reconnected", exchange=self.exchange)
        except Exception as exc:
            log.error("exchange_bot_reconnect_failed", exchange=self.exchange, error=str(exc))
            self._running = False
            self.q_switch.trigger_manually("Reconnect failed — manual review required")

    async def _emergency_stop(self) -> None:
        """Called by heartbeat monitor on failure."""
        log.error("emergency_stop", exchange=self.exchange)
        self._running = False
        self.q_switch.trigger_manually("Heartbeat failure")
        await self.order_manager.cancel_all()
        await self.live_feed.emit_emergency_stop(self.exchange, "Heartbeat failure")

    # ------------------------------------------------------------------
    # State accessors (for API)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        state = self._last_global_state
        inv = self.inventory.state()
        return {
            "exchange": self.exchange,
            "running": self._running,
            "dry_run": not self.live_mode,
            "q_switch_triggered": self.q_switch.is_triggered,
            "circuit_breaker_tripped": self.circuit_breaker.is_tripped(),
            "circuit_breaker_reason": self.circuit_breaker.trip_reason,
            "open_orders": self.order_manager.open_order_count,
            "global_mid": state.global_mid if state else None,
            "volatility": state.volatility if state else None,
            "aggressiveness": state.aggressiveness if state else None,
            "zz_regime": state.zz_regime if state else None,
            "hmm_regime": state.hmm_regime if state else None,
            "hmm_regime_confidence": state.hmm_regime_confidence if state else None,
            "balance_usd": inv.usd,
            "balance_token": inv.token,
            "skew_factor": inv.skew_factor,
            "token_drift_pct": inv.token_drift_pct,
        }
