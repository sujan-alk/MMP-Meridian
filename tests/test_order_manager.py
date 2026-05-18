"""
Tests for core/order_manager.py — OrderManager.

Validates:
- Diff-and-repost logic (only cancels stale, only places missing)
- Price tolerance matching (5bps threshold)
- Dry-run mode (fake orders, no exchange API calls)
- Live mode (actual connector calls)
- Cancel-all functionality
- Edge cases: zero amount, zero price, empty grid
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config.schema import ExchangeBotConfig
from core.order_manager import OrderGrid, OrderManager, PRICE_TOLERANCE_PCT
from db.database import Database
from exchange.base import Balance, Order
from tests.conftest import make_order


def make_grid(
    buy_prices=None,
    buy_amounts=None,
    sell_prices=None,
    sell_amounts=None,
    global_mid=0.10,
    aggressiveness=0.5,
) -> OrderGrid:
    """Helper to create an OrderGrid with defaults."""
    return OrderGrid(
        buy_prices=buy_prices or [0.099, 0.098, 0.097],
        buy_amounts=buy_amounts or [100.0, 100.0, 100.0],
        sell_prices=sell_prices or [0.101, 0.102, 0.103],
        sell_amounts=sell_amounts or [100.0, 100.0, 100.0],
        global_mid=global_mid,
        aggressiveness=aggressiveness,
    )


@pytest_asyncio.fixture
async def order_manager(mock_connector, exchange_bot_config, db, mock_rate_limiter):
    """Create an OrderManager in dry-run mode."""
    om = OrderManager(
        connector=mock_connector,
        config=exchange_bot_config,
        db=db,
        rate_limiter=mock_rate_limiter,
        live_mode=False,
    )
    return om


@pytest_asyncio.fixture
async def live_order_manager(mock_connector, exchange_bot_config, db, mock_rate_limiter):
    """Create an OrderManager in live mode."""
    om = OrderManager(
        connector=mock_connector,
        config=exchange_bot_config,
        db=db,
        rate_limiter=mock_rate_limiter,
        live_mode=True,
    )
    return om


class TestDryRunMode:
    """Tests for OrderManager in dry-run (no real exchange calls)."""

    @pytest.mark.asyncio
    async def test_dry_run_creates_fake_orders(self, order_manager):
        """In dry-run mode, orders should be created with DRY- prefix."""
        grid = make_grid()
        placed = await order_manager.diff_and_repost(grid)
        assert len(placed) == 6  # 3 buy + 3 sell
        for order in placed:
            assert order.id.startswith("DRY-")

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_exchange(self, order_manager, mock_connector):
        """Dry-run should not call any exchange API methods for order placement."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        mock_connector.create_limit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_tracks_open_orders(self, order_manager):
        """Dry-run orders should be tracked in the in-memory cache."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        assert order_manager.open_order_count == 6

    @pytest.mark.asyncio
    async def test_dry_run_writes_to_db(self, order_manager, db):
        """Dry-run orders should be persisted to the database."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        count = await db.fetchval("SELECT COUNT(*) FROM mm_bot.orders")
        assert count == 6


class TestDiffAndRepost:
    """Tests for the diff logic — what gets cancelled and what gets placed."""

    @pytest.mark.asyncio
    async def test_identical_grid_no_changes(self, order_manager):
        """If the grid hasn't changed, no orders should be cancelled or placed."""
        grid = make_grid()
        # First call places all orders
        await order_manager.diff_and_repost(grid)
        assert order_manager.open_order_count == 6

        # Second call with same grid — should be no new placements
        placed = await order_manager.diff_and_repost(grid)
        assert len(placed) == 0
        assert order_manager.open_order_count == 6

    @pytest.mark.asyncio
    async def test_shifted_grid_cancels_stale_and_places_new(self, order_manager):
        """When prices shift, stale orders are cancelled and new ones placed."""
        grid1 = make_grid(buy_prices=[0.099, 0.098, 0.097], sell_prices=[0.101, 0.102, 0.103])
        await order_manager.diff_and_repost(grid1)
        assert order_manager.open_order_count == 6

        # Shift prices significantly (beyond 5bps tolerance)
        grid2 = make_grid(buy_prices=[0.095, 0.094, 0.093], sell_prices=[0.105, 0.106, 0.107])
        placed = await order_manager.diff_and_repost(grid2)
        # All 6 old should be cancelled (no match within 5bps), 6 new placed
        assert len(placed) == 6
        assert order_manager.open_order_count == 6

    @pytest.mark.asyncio
    async def test_small_price_change_within_tolerance(self, order_manager):
        """Price changes within 5bps should not trigger cancel/replace."""
        grid1 = make_grid(buy_prices=[0.10000], buy_amounts=[100.0],
                          sell_prices=[0.11000], sell_amounts=[100.0])
        await order_manager.diff_and_repost(grid1)
        assert order_manager.open_order_count == 2

        # Shift by 2bps (within 5bps tolerance)
        grid2 = make_grid(buy_prices=[0.10002], buy_amounts=[100.0],
                          sell_prices=[0.11002], sell_amounts=[100.0])
        placed = await order_manager.diff_and_repost(grid2)
        # Orders should be considered "matching" — no new placements
        assert len(placed) == 0

    @pytest.mark.asyncio
    async def test_zero_amount_not_placed(self, order_manager):
        """Orders with zero amount should be skipped."""
        grid = make_grid(buy_prices=[0.099], buy_amounts=[0.0],
                         sell_prices=[0.101], sell_amounts=[100.0])
        placed = await order_manager.diff_and_repost(grid)
        assert len(placed) == 1  # Only the sell order

    @pytest.mark.asyncio
    async def test_zero_price_not_placed(self, order_manager):
        """Orders with zero price should be skipped."""
        grid = make_grid(buy_prices=[0.0], buy_amounts=[100.0],
                         sell_prices=[0.101], sell_amounts=[100.0])
        placed = await order_manager.diff_and_repost(grid)
        assert len(placed) == 1  # Only the sell order


class TestCancelAll:
    """Tests for cancel_all() used during emergency stop or shutdown."""

    @pytest.mark.asyncio
    async def test_cancel_all_clears_open_orders(self, order_manager):
        """cancel_all should remove all tracked orders."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        assert order_manager.open_order_count == 6

        await order_manager.cancel_all()
        assert order_manager.open_order_count == 0

    @pytest.mark.asyncio
    async def test_cancel_all_updates_db_status(self, order_manager, db):
        """cancel_all should update all order statuses to 'canceled' in DB."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        await order_manager.cancel_all()

        count = await db.fetchval("SELECT COUNT(*) FROM mm_bot.orders WHERE status = 'canceled'")
        assert count == 6

    @pytest.mark.asyncio
    async def test_cancel_all_dry_run_no_exchange_call(self, order_manager, mock_connector):
        """cancel_all in dry-run should not hit the exchange."""
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        await order_manager.cancel_all()
        mock_connector.cancel_all_orders.assert_not_called()


class TestLiveMode:
    """Tests for live mode (actual exchange API calls)."""

    @pytest.mark.asyncio
    async def test_live_mode_calls_exchange(self, live_order_manager, mock_connector):
        """Live mode should call create_limit_order on the connector."""
        grid = make_grid(buy_prices=[0.099], buy_amounts=[100.0],
                         sell_prices=[0.101], sell_amounts=[100.0])
        await live_order_manager.diff_and_repost(grid)
        assert mock_connector.create_limit_order.call_count == 2

    @pytest.mark.asyncio
    async def test_live_mode_syncs_open_orders(self, live_order_manager, mock_connector):
        """Live mode should call fetch_open_orders to sync state."""
        grid = make_grid()
        await live_order_manager.diff_and_repost(grid)
        mock_connector.fetch_open_orders.assert_called()

    @pytest.mark.asyncio
    async def test_live_cancel_all_calls_exchange(self, live_order_manager, mock_connector):
        """cancel_all in live mode should call the exchange cancel endpoint."""
        await live_order_manager.cancel_all()
        mock_connector.cancel_all_orders.assert_called_once()


class TestPriceTolerance:
    """Tests for the _has_order_near static method and tolerance logic."""

    def test_within_tolerance(self):
        """Orders within 3bps should be considered matching."""
        open_prices = {0.10000: make_order(price=0.10000)}
        # 2bps off: 0.10000 * 0.0002 = 0.00002
        assert OrderManager._has_order_near(0.10002, open_prices)

    def test_outside_tolerance(self):
        """Orders beyond 3bps should not match."""
        open_prices = {0.10000: make_order(price=0.10000)}
        # 5bps off: 0.10000 * 0.0005 = 0.00005
        assert not OrderManager._has_order_near(0.10005, open_prices)

    def test_exact_match(self):
        """Exact price match should always be within tolerance."""
        open_prices = {0.10000: make_order(price=0.10000)}
        assert OrderManager._has_order_near(0.10000, open_prices)

    def test_empty_open_orders(self):
        """No open orders means nothing matches."""
        assert not OrderManager._has_order_near(0.10000, {})


class TestOpenOrderProperties:
    """Tests for order tracking properties."""

    @pytest.mark.asyncio
    async def test_open_order_count(self, order_manager):
        """open_order_count should reflect tracked orders."""
        assert order_manager.open_order_count == 0
        grid = make_grid()
        await order_manager.diff_and_repost(grid)
        assert order_manager.open_order_count == 6

    @pytest.mark.asyncio
    async def test_open_orders_list(self, order_manager):
        """open_orders property should return a list of Order objects."""
        grid = make_grid(buy_prices=[0.099], buy_amounts=[100.0],
                         sell_prices=[0.101], sell_amounts=[100.0])
        await order_manager.diff_and_repost(grid)
        orders = order_manager.open_orders
        assert len(orders) == 2
        sides = {o.side for o in orders}
        assert "buy" in sides
        assert "sell" in sides
