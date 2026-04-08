"""
Exchange Bot — per-exchange market making tick loop.

One ExchangeBot runs per exchange (KuCoin, Gate, MEXC, Kraken).
It receives a GlobalState from the Orchestrator via an asyncio.Queue
and manages the order grid for its exchange.

Part of Meridian's Option B hierarchical architecture: each ExchangeBot
receives a RegimeState from the shared RegimeMaster and applies regime
multipliers (spread_mult, depth_mult, aggressiveness, inventory_skew) to
ensure all bots quote consistently with the same market view.

Tick sequence:
1. Receive GlobalState (global_mid + volatility + aggressiveness + regime_state)
2. Drain latest RegimeState from regime_queue (non-blocking)
3. Fetch balance (cached, refreshed every BALANCE_CACHE_S seconds)
4. Safety checks: Q-Switch + CircuitBreaker
5. Update InventoryTracker → skew_factor (adjusted by regime inventory_skew)
6. Compute spread levels + amounts from quant model using regime multipliers
7. OrderManager.diff_and_repost → cancel stale, place new
8. Record inventory snapshot + RL features to DB
9. Emit TICK_UPDATE event to WebSocket feed
10. Heartbeat.beat()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config.schema import ExchangeBotConfig
from core.inventory_tracker import InventoryTracker
from core.order_manager import OrderGrid, OrderManager
from db.database import Database
from db.queries import insert_inventory_snapshot, insert_rl_features, get_fill_rate, get_recent_pnl
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
    from core.regime_master import RegimeState

log = get_logger("exchange_bot")

BALANCE_CACHE_S = 10.0          # Re-fetch balance at most every 10 seconds
SNAPSHOT_INTERVAL_S = 30.0      # Write inventory snapshot every 30 seconds
RL_FEATURE_INTERVAL_S = 10.0    # Write RL features every 10 seconds


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
        regime_queue: asyncio.Queue | None = None,
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

        # Regime queue: receives RegimeState updates from the RegimeMaster
        self.regime_queue: asyncio.Queue | None = regime_queue
        self._current_regime_state: "RegimeState | None" = None

        self._running = False
        self._balance: Balance | None = None
        self._balance_cached_at: float = 0.0
        self._last_snapshot_at: float = 0.0
        self._last_rl_at: float = 0.0
        self._last_global_state: "GlobalState | None" = None
        self._fill_tick_counter: int = 0
        self._last_fill_ts: float = 0.0

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

    async def _tick(self, state: "GlobalState") -> None:
        """Process one tick: update quant model + manage orders."""
        self._last_global_state = state

        # 1. Drain latest RegimeState from regime queue (non-blocking)
        self._drain_regime_queue()

        # Resolve mm_params from regime state (fallback: use state.regime_state)
        mm_params = self._resolve_mm_params(state)

        # 2. Fetch balance (cached)
        balance = await self._get_balance()
        if balance is None:
            return

        # 3. Safety checks
        if self.q_switch.check(balance):
            log.warning("tick_skipped_q_switch", exchange=self.exchange)
            return
        self.circuit_breaker.record_equity(balance.usd, balance.token, state.global_mid)
        if self.circuit_breaker.is_tripped():
            log.warning("tick_skipped_circuit_breaker", exchange=self.exchange,
                        reason=self.circuit_breaker.trip_reason)
            return

        # 4. Inventory tracking (adjusted by regime inventory_skew)
        self.inventory.update(balance)
        skew = self.inventory.skew_factor()
        # Regime inventory_skew biases the skew_factor: positive = lean long
        inventory_skew = mm_params.get('inventory_skew', 0.0)
        if inventory_skew != 0.0:
            skew = max(0.5, min(2.0, skew + inventory_skew))

        # 5. Compute order grid using quant model + regime multipliers
        n = self.config.depth.levels

        # aggressiveness: regime overrides the model output
        regime_agg = mm_params.get('aggressiveness', None)
        if regime_agg is not None:
            buy_agg = regime_agg
            sell_agg = regime_agg
        else:
            buy_agg, sell_agg = self.agg_model.compute_with_regime(
                state.volatility, state.zz_regime
            )

        spread_mult = mm_params.get('spread_mult', 1.0)
        depth_mult = mm_params.get('depth_mult', 1.0)

        # Compute spread levels, then apply spread_mult
        buy_spreads, _ = self.spread_engine.compute_levels(buy_agg, n, spread_mult=spread_mult)
        _, sell_spreads = self.spread_engine.compute_levels(sell_agg, n, spread_mult=spread_mult)

        buy_prices, sell_prices = self.spread_engine.prices_from_spreads(
            state.global_mid, buy_spreads, sell_spreads
        )

        buy_usd_amounts = self.depth_engine.compute_amounts(
            buy_agg, n, skew, "buy", depth_mult=depth_mult
        )
        sell_usd_amounts = self.depth_engine.compute_amounts(
            sell_agg, n, skew, "sell", depth_mult=depth_mult
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

        # 6. Periodic DB writes
        ts = now_s()
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
            )
            self._last_rl_at = ts

        # 7. Check fills every 5 ticks (~5 seconds)
        self._fill_tick_counter += 1
        if self._fill_tick_counter >= 5:
            self._fill_tick_counter = 0
            await self._check_fills()

        # 8. Emit WebSocket event
        await self.live_feed.emit_tick(
            exchange=self.exchange,
            global_mid=state.global_mid,
            volatility=state.volatility,
            aggressiveness=grid.aggressiveness,
            skew_factor=skew,
            open_orders=self.order_manager.open_order_count,
            placed_count=len(placed),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _check_fills(self) -> None:
        """Fetch recent trades and persist new fills to DB."""
        try:
            await self.rate_limiter.acquire()
            trades = await self.connector.fetch_my_trades(
                self.config.symbol, since=int(self._last_fill_ts)
            )
            for t in trades:
                fill = Fill(
                    id=t.get("id", ""),
                    order_id=t.get("order", ""),
                    exchange=self.exchange,
                    symbol=t.get("symbol", self.config.symbol),
                    side=t.get("side", ""),
                    filled_price=float(t.get("price", 0)),
                    filled_amount=float(t.get("amount", 0)),
                    fee=float(t.get("fee", {}).get("cost", 0)) if t.get("fee") else 0.0,
                    fee_currency=t.get("fee", {}).get("currency", "") if t.get("fee") else "",
                    timestamp=float(t.get("timestamp", now_s() * 1000)) / 1000,
                )
                await insert_fill(self.db, fill)
                ts = float(t.get("timestamp", 0))
                if ts > self._last_fill_ts:
                    self._last_fill_ts = ts
            if trades:
                log.info("fills_recorded", exchange=self.exchange, count=len(trades))
        except Exception as exc:
            log.warning("check_fills_failed", exchange=self.exchange, error=str(exc))

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

    def _drain_regime_queue(self) -> None:
        """
        Non-blocking drain of the regime queue.
        Only the latest RegimeState is kept — stale intermediate states are discarded.
        """
        if self.regime_queue is None:
            return
        latest = None
        while True:
            try:
                latest = self.regime_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if latest is not None:
            self._current_regime_state = latest

    def _resolve_mm_params(self, state: "GlobalState") -> dict:
        """
        Return mm_params from the latest RegimeState.
        Priority: regime_queue update > state.regime_state > default RANGING params.
        """
        from quant.regime_detector import MM_PARAMETERS
        rs = self._current_regime_state or state.regime_state
        if rs is not None:
            return rs.mm_params
        return MM_PARAMETERS['RANGING'].copy()

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
        rs = self._current_regime_state or (state.regime_state if state else None)
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
            # Regime Master fields
            "regime": rs.regime if rs else None,
            "regime_confidence": round(rs.confidence, 4) if rs else None,
            "regime_agreement": rs.agreement if rs else None,
            "regime_mm_params": rs.mm_params if rs else None,
            "balance_usd": inv.usd,
            "balance_token": inv.token,
            "skew_factor": inv.skew_factor,
            "token_drift_pct": inv.token_drift_pct,
        }
