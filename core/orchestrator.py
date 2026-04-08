"""
Orchestrator — central coordinator for all 4 exchange bots.

Responsibilities:
1. Fetches tickers from all 4 exchanges every 1 second in parallel
2. Computes global_mid = 0.45*gate + 0.45*kucoin + 0.05*mexc + 0.05*kraken
   (weights are normalised if one or more exchanges fail to respond)
3. Feeds global_mid into the shared VolatilityEngine
4. Computes aggressiveness via AggressivenessModel
5. Runs the RegimeMaster (Option B hierarchical architecture) which:
   - Runs the HMM regime detector continuously
   - Cross-checks with Zhang-Zhang classifier
   - Broadcasts unified RegimeState to all ExchangeBots
6. Distributes GlobalState to all ExchangeBot queues (put_nowait, drop if full)
7. Handles config hot-reload via asyncio.Event (triggered by PUT /api/config)
8. Manages bot lifecycle (start, stop, emergency_stop all)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from config.schema import BotConfig
from core.exchange_bot import ExchangeBot
from core.inventory_tracker import InventoryTracker
from core.regime_master import RegimeMaster, RegimeState
from db.database import Database
from exchange.base import BaseConnector
from exchange.factory import create_connector
from quant.aggressiveness import AggressivenessModel
from quant.volatility import VolatilityEngine
from utils.logging import get_logger
from utils.time_utils import now_s
from utils.math_utils import normalize_weights

if TYPE_CHECKING:
    from api.websocket import LiveFeed

log = get_logger("orchestrator")

PRICE_LOOP_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# GlobalState — shared price/vol context broadcast to all exchange bots
# ---------------------------------------------------------------------------

@dataclass
class GlobalState:
    global_mid: float
    volatility: float
    aggressiveness: float
    zz_vol: float
    zz_regime: str
    timestamp: float
    contributing_exchanges: list[str] = field(default_factory=list)
    regime_state: Optional[RegimeState] = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Top-level coordinator. One instance per running bot process.
    """

    def __init__(
        self,
        config: BotConfig,
        db: Database,
        live_feed: "LiveFeed",
        live_mode: bool = False,
    ):
        self.config = config
        self.db = db
        self.live_feed = live_feed
        self.live_mode = live_mode

        # Shared quant models (volatility is global, aggressiveness uses global vol)
        self.vol_engine = VolatilityEngine(config.volatility)
        self.agg_model = AggressivenessModel(config.volatility)

        # Regime Master — single unified regime source for all exchange bots (Option B)
        self.regime_master = RegimeMaster()

        # Per-exchange connectors and bots (built in start())
        self._connectors: dict[str, BaseConnector] = {}
        self._bots: dict[str, ExchangeBot] = {}
        self._bot_queues: dict[str, asyncio.Queue] = {}
        self._regime_queues: dict[str, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []

        # Config hot-reload
        self._config_reload_event = asyncio.Event()
        self._running = False
        self._state: GlobalState | None = None
        self._state_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build connectors, start bots, and enter the price loop."""
        log.info("orchestrator_starting", live_mode=self.live_mode, dry_run=self.config.dry_run)
        self._running = True

        # Build connectors for all enabled exchanges
        from config.settings import get_secrets
        secrets = get_secrets()

        for ex_cfg in self.config.enabled_exchanges():
            creds = secrets.credentials_for(ex_cfg.exchange)
            connector = create_connector(
                exchange_name=ex_cfg.exchange,
                symbol=ex_cfg.symbol,
                credentials=creds,
                quote_currency=ex_cfg.quote_currency,
                ccxt_options=ex_cfg.ccxt_options,
            )
            self._connectors[ex_cfg.exchange] = connector

            q: asyncio.Queue = asyncio.Queue(maxsize=5)
            self._bot_queues[ex_cfg.exchange] = q

            regime_q: asyncio.Queue = asyncio.Queue(maxsize=5)
            self._regime_queues[ex_cfg.exchange] = regime_q
            self.regime_master.add_subscriber(regime_q)

            bot = ExchangeBot(
                config=ex_cfg,
                connector=connector,
                queue=q,
                db=self.db,
                live_feed=self.live_feed,
                agg_model=self.agg_model,
                live_mode=self.live_mode and not self.config.dry_run,
                regime_queue=regime_q,
            )

            # Apply configured initial balances if present
            init_bal = self.config.initial_balances.get(ex_cfg.exchange)
            if init_bal:
                bot.inventory.set_initial_from_config(init_bal.usd, init_bal.token)

            self._bots[ex_cfg.exchange] = bot

        # Start all bots as concurrent tasks
        for name, bot in self._bots.items():
            task = asyncio.create_task(bot.start(), name=f"bot_{name}")
            self._tasks.append(task)

        # Start the global price loop
        price_task = asyncio.create_task(self._price_loop(), name="price_loop")
        self._tasks.append(price_task)

        # Start the Regime Master (Option B hierarchical architecture)
        regime_task = self.regime_master.start_task()
        self._tasks.append(regime_task)

        log.info("orchestrator_started", exchanges=list(self._bots.keys()))

        # Wait for all tasks
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Gracefully stop all bots, the price loop, and the regime master."""
        log.info("orchestrator_stopping")
        self._running = False
        for bot in self._bots.values():
            await bot.stop()
        await self.regime_master.stop()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        log.info("orchestrator_stopped")

    async def emergency_stop_all(self, reason: str = "API request") -> None:
        """Trigger Q-Switch on all exchange bots."""
        log.warning("emergency_stop_all", reason=reason)
        for bot in self._bots.values():
            bot.q_switch.trigger_manually(reason)
            await bot.order_manager.cancel_all()
        await self.live_feed.emit_emergency_stop("ALL", reason)

    # ------------------------------------------------------------------
    # Config hot-reload
    # ------------------------------------------------------------------

    def trigger_config_reload(self, new_config: BotConfig) -> None:
        """
        Called by PUT /api/config to push a new config without restarting.
        Updates spread/depth/volatility params; ignores credential changes.
        """
        log.info("config_reload_triggered")
        self.config = new_config
        # Rebuild quant models with new volatility config
        self.vol_engine = VolatilityEngine(new_config.volatility)
        self.agg_model = AggressivenessModel(new_config.volatility)
        # Update per-bot configs
        for ex_cfg in new_config.enabled_exchanges():
            bot = self._bots.get(ex_cfg.exchange)
            if bot:
                bot.config = ex_cfg
                bot.spread_engine = bot.spread_engine.__class__(ex_cfg.spread)
                bot.depth_engine = bot.depth_engine.__class__(ex_cfg.depth)
                bot.agg_model = self.agg_model
        self._config_reload_event.set()
        self._config_reload_event.clear()
        log.info("config_reload_applied")

    # ------------------------------------------------------------------
    # Global price loop
    # ------------------------------------------------------------------

    async def _price_loop(self) -> None:
        """
        Fetches tickers from all exchanges every second in parallel.
        Computes weighted global mid-price and distributes to bot queues.
        """
        while self._running:
            try:
                await self._tick_prices()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("price_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(PRICE_LOOP_INTERVAL_S)

    async def _tick_prices(self) -> None:
        weights = self.config.global_mid_weights.as_dict()

        # Fetch all tickers in parallel
        fetch_tasks = {
            name: asyncio.create_task(
                self._fetch_ticker_safe(name),
                name=f"ticker_{name}",
            )
            for name in self._connectors
        }
        results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)
        ticker_map = dict(zip(fetch_tasks.keys(), results))

        # Build weighted mid-price from successful fetches
        valid: dict[str, float] = {}
        for name, result in ticker_map.items():
            if isinstance(result, Exception):
                log.warning("ticker_fetch_failed", exchange=name, error=str(result))
            elif result is not None:
                valid[name] = result

        if not valid:
            log.error("no_valid_tickers", msg="All exchanges failed to return a price")
            return

        # Normalise weights over responding exchanges only
        active_weights = {k: weights.get(k, 0.0) for k in valid}
        normalised = normalize_weights(active_weights)
        global_mid = sum(normalised[k] * v for k, v in valid.items())

        if global_mid <= 0:
            log.error("invalid_global_mid", value=global_mid)
            return

        # Update volatility engine
        self.vol_engine.update_price(global_mid)
        vol = self.vol_engine.rolling_vol()
        zz_vol, zz_regime = self.vol_engine.zhang_zhang_vol()
        agg = self.agg_model.compute(vol)

        # Capture the latest regime state from the Regime Master
        current_regime_state = self.regime_master.current_regime_state

        state = GlobalState(
            global_mid=global_mid,
            volatility=vol,
            aggressiveness=agg,
            zz_vol=zz_vol,
            zz_regime=zz_regime,
            timestamp=now_s(),
            contributing_exchanges=list(valid.keys()),
            regime_state=current_regime_state,
        )

        async with self._state_lock:
            self._state = state

        # Push raw market data to the Regime Master for HMM update
        self.regime_master.push_state(state)

        # Distribute to all bot queues (drop if bot is behind)
        for name, q in self._bot_queues.items():
            try:
                q.put_nowait(state)
            except asyncio.QueueFull:
                log.debug("bot_queue_full", exchange=name)

    async def _fetch_ticker_safe(self, exchange_name: str) -> float | None:
        """Fetch mid-price for one exchange, returning None on error."""
        try:
            connector = self._connectors[exchange_name]
            ticker = await connector.fetch_ticker()
            return ticker.mid
        except Exception as exc:
            raise exc  # Re-raise so gather sees it as exception

    # ------------------------------------------------------------------
    # State accessors (for API)
    # ------------------------------------------------------------------

    def get_global_state(self) -> GlobalState | None:
        return self._state

    def get_all_statuses(self) -> list[dict]:
        return [bot.get_status() for bot in self._bots.values()]

    def get_bot(self, exchange: str) -> ExchangeBot | None:
        return self._bots.get(exchange)
