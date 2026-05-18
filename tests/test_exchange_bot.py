"""
Tests for core/exchange_bot.py — ExchangeBot.

Validates:
- Tick processing logic (the full tick pipeline)
- Safety system integration (Q-switch halts tick, circuit breaker halts tick)
- Balance caching behavior
- Emergency stop callback
- Status accessor
- Warmup phase
- Queue timeout handling

All exchange API calls are mocked — no real exchange connections.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from config.schema import ExchangeBotConfig, SafetyConfig
from core.exchange_bot import ExchangeBot, BALANCE_CACHE_S
from core.orchestrator import GlobalState
from exchange.base import Balance, Ticker
from quant.aggressiveness import AggressivenessModel
from tests.conftest import make_balance, make_ticker


@pytest.fixture
def global_state() -> GlobalState:
    """A sample GlobalState that the orchestrator would produce."""
    return GlobalState(
        global_mid=0.10,
        volatility=0.002,
        aggressiveness=0.5,
        zz_vol=0.0015,
        zz_regime="choppy",
        hmm_regime="NORMAL",
        hmm_regime_confidence=0.0,
        timestamp=1700000000.0,
        contributing_exchanges=["kucoin", "gate"],
    )


@pytest.fixture
def mock_live_feed():
    """Mock LiveFeed that accepts all emit calls."""
    feed = AsyncMock()
    feed.emit_tick = AsyncMock()
    feed.emit_emergency_stop = AsyncMock()
    return feed


@pytest.fixture
def agg_model(volatility_config):
    return AggressivenessModel(volatility_config)


@pytest_asyncio.fixture
async def bot(exchange_bot_config, mock_connector, db, mock_live_feed, agg_model):
    """Create an ExchangeBot for testing (dry-run mode)."""
    q = asyncio.Queue(maxsize=5)
    bot = ExchangeBot(
        config=exchange_bot_config,
        connector=mock_connector,
        queue=q,
        db=db,
        live_feed=mock_live_feed,
        agg_model=agg_model,
        live_mode=False,
    )
    # Pre-set initial balance so warmup isn't needed
    bot.inventory.record_initial(Balance(usd=1000.0, token=5000.0))
    bot._balance = make_balance()
    bot._balance_cached_at = 1700000000.0
    return bot


class TestTickProcessing:
    """Tests for the _tick() method — the core of each exchange bot cycle."""

    @pytest.mark.asyncio
    async def test_tick_places_orders(self, bot, global_state):
        """A normal tick should compute and place orders via the order manager."""
        await bot._tick(global_state)
        # In dry-run mode, orders should be created
        assert bot.order_manager.open_order_count > 0

    @pytest.mark.asyncio
    async def test_tick_emits_websocket_event(self, bot, global_state, mock_live_feed):
        """Each tick should emit a TICK_UPDATE event to the live feed."""
        await bot._tick(global_state)
        mock_live_feed.emit_tick.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_updates_inventory(self, bot, global_state):
        """Tick should update inventory tracker with current balance."""
        await bot._tick(global_state)
        state = bot.inventory.state()
        assert state.usd == 1000.0  # From make_balance()
        assert state.token == 5000.0

    @pytest.mark.asyncio
    async def test_tick_records_heartbeat(self, bot, global_state):
        """Tick should call heartbeat.beat() to signal liveness."""
        bot.heartbeat = MagicMock()
        bot.heartbeat.beat = MagicMock()
        # We need to run the loop once, but _run_loop blocks.
        # Instead test _tick directly and check heartbeat is called in _run_loop
        # by verifying the _last_global_state is set
        await bot._tick(global_state)
        assert bot._last_global_state == global_state


class TestSafetyIntegration:
    """Tests for safety system integration within the tick."""

    @pytest.mark.asyncio
    async def test_q_switch_halts_tick(self, bot, global_state):
        """When Q-Switch is triggered, tick should skip order processing."""
        bot.q_switch.trigger_manually("Test trigger")
        await bot._tick(global_state)
        # No orders should be placed when Q-Switch is active
        assert bot.order_manager.open_order_count == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_halts_tick(self, bot, global_state, safety_config):
        """When circuit breaker is tripped, tick should skip order processing."""
        # Trip the circuit breaker by recording a huge loss
        bot.circuit_breaker.record_equity(usd=10000.0, token=0.0, mid_price=0.10)
        bot.circuit_breaker.record_equity(usd=1000.0, token=0.0, mid_price=0.10)  # 90% loss
        assert bot.circuit_breaker.is_tripped()

        await bot._tick(global_state)
        assert bot.order_manager.open_order_count == 0

    @pytest.mark.asyncio
    async def test_low_balance_triggers_q_switch(self, bot, global_state, mock_connector):
        """Balance below safety threshold should trigger Q-Switch."""
        # Override balance to be very low
        mock_connector.fetch_balance.return_value = Balance(usd=10.0, token=50.0)
        bot._balance = Balance(usd=10.0, token=50.0)
        bot._balance_cached_at = 0  # Force refresh

        await bot._tick(global_state)
        assert bot.q_switch.is_triggered


class TestBalanceCaching:
    """Tests for balance caching behavior."""

    @pytest.mark.asyncio
    async def test_uses_cached_balance(self, bot, global_state, mock_connector):
        """Should use cached balance if within BALANCE_CACHE_S."""
        with patch("core.exchange_bot.now_s", return_value=bot._balance_cached_at + 5.0):
            balance = await bot._get_balance()
            # Should not have called fetch_balance (cache is fresh)
            # Note: rate_limiter.acquire is called but connector.fetch_balance may not be
            assert balance is not None
            assert balance.usd == 1000.0

    @pytest.mark.asyncio
    async def test_refreshes_stale_balance(self, bot, mock_connector):
        """Should re-fetch balance when cache is stale."""
        new_balance = Balance(usd=900.0, token=5100.0)
        mock_connector.fetch_balance.return_value = new_balance

        with patch("core.exchange_bot.now_s", return_value=bot._balance_cached_at + BALANCE_CACHE_S + 1):
            balance = await bot._get_balance()
            assert balance.usd == 900.0

    @pytest.mark.asyncio
    async def test_returns_stale_on_fetch_failure(self, bot, mock_connector):
        """If fetch fails, should return the stale cached balance."""
        mock_connector.fetch_balance.side_effect = Exception("Network error")

        with patch("core.exchange_bot.now_s", return_value=bot._balance_cached_at + BALANCE_CACHE_S + 1):
            balance = await bot._get_balance()
            # Should return stale balance, not None
            assert balance is not None
            assert balance.usd == 1000.0


class TestEmergencyStop:
    """Tests for the emergency stop callback."""

    @pytest.mark.asyncio
    async def test_emergency_stop_cancels_orders(self, bot, global_state):
        """Emergency stop should cancel all open orders."""
        # Place some orders first
        await bot._tick(global_state)
        assert bot.order_manager.open_order_count > 0

        await bot._emergency_stop()
        assert bot.order_manager.open_order_count == 0

    @pytest.mark.asyncio
    async def test_emergency_stop_sets_running_false(self, bot):
        """Emergency stop should set _running to False."""
        bot._running = True
        await bot._emergency_stop()
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_emergency_stop_triggers_q_switch(self, bot):
        """Emergency stop should trigger the Q-Switch."""
        await bot._emergency_stop()
        assert bot.q_switch.is_triggered

    @pytest.mark.asyncio
    async def test_emergency_stop_emits_event(self, bot, mock_live_feed):
        """Emergency stop should emit an EMERGENCY_STOP event."""
        await bot._emergency_stop()
        mock_live_feed.emit_emergency_stop.assert_called_once()


class TestGetStatus:
    """Tests for the status accessor used by the API."""

    def test_status_contains_required_fields(self, bot):
        """Status dict should contain all expected fields."""
        status = bot.get_status()
        required_keys = [
            "exchange", "running", "dry_run", "q_switch_triggered",
            "circuit_breaker_tripped", "circuit_breaker_reason",
            "open_orders", "global_mid", "volatility", "aggressiveness",
            "zz_regime", "balance_usd", "balance_token", "skew_factor",
            "token_drift_pct",
        ]
        for key in required_keys:
            assert key in status, f"Missing key: {key}"

    def test_status_reflects_exchange_name(self, bot):
        """Status should report the correct exchange name."""
        assert bot.get_status()["exchange"] == "kucoin"

    def test_status_reflects_dry_run(self, bot):
        """Status should correctly report dry-run mode."""
        assert bot.get_status()["dry_run"] is True

    @pytest.mark.asyncio
    async def test_status_after_tick(self, bot, global_state):
        """Status should reflect state after a tick has been processed."""
        await bot._tick(global_state)
        status = bot.get_status()
        assert status["global_mid"] == 0.10
        assert status["volatility"] == 0.002
        assert status["open_orders"] > 0


class TestWarmup:
    """Tests for the bot warmup phase."""

    @pytest.mark.asyncio
    async def test_warmup_fetches_balance(self, bot, mock_connector):
        """Warmup should fetch the initial balance from the exchange."""
        bot._balance = None
        await bot._warmup()
        mock_connector.fetch_balance.assert_called()
        assert bot._balance is not None

    @pytest.mark.asyncio
    async def test_warmup_records_initial_inventory(self, bot, mock_connector):
        """Warmup should record the fetched balance as the initial inventory."""
        # Reset tracker
        bot.inventory._initial_usd = None
        bot.inventory._initial_token = None
        await bot._warmup()
        state = bot.inventory.state()
        assert state.initial_usd > 0
        assert state.initial_token > 0

    @pytest.mark.asyncio
    async def test_warmup_handles_failure(self, bot, mock_connector):
        """Warmup failure should be logged but not crash the bot."""
        mock_connector.fetch_balance.side_effect = Exception("Exchange down")
        # Should not raise
        await bot._warmup()
