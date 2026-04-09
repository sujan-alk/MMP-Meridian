"""
Tests for core/paper_trader.py — PaperTrader.

Validates:
- on_book_update() calls FillSimulator with correct arguments
- Fills are persisted to the database
- Fills emit order_filled events to the live feed
- No fills when open_orders is empty
- Multiple consecutive book updates accumulate fills in DB
- Stats are updated after each fill
- get_stats() returns expected keys
- PaperTrader stop() disconnects the WS connector
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.fill_simulator import SimulatedFill
from core.paper_trader import PaperTrader
from exchange.ws_connector import OrderBook
from tests.conftest import make_order


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ws_connector():
    """Mock WSConnector that returns an order book on request."""
    connector = AsyncMock()
    connector.exchange_name = "kucoin"
    connector.symbol = "ALKIMI/USDT"
    connector.is_ws_connected = True
    connector.connect_ws.return_value = None
    connector.disconnect_ws.return_value = None
    # watch_order_book returns None by default (no pending book)
    connector.watch_order_book.return_value = None
    return connector


@pytest.fixture
def mock_live_feed():
    """Mock LiveFeed that accepts emit() calls."""
    feed = AsyncMock()
    feed.emit = AsyncMock(return_value=None)
    return feed


@pytest_asyncio.fixture
async def paper_trader(db, mock_ws_connector, mock_live_feed):
    """PaperTrader wired with mock connector, real in-memory DB, and mock feed."""
    trader = PaperTrader(
        exchange="kucoin",
        ws_connector=mock_ws_connector,
        db=db,
        live_feed=mock_live_feed,
        maker_fee_bps=10.0,
    )
    return trader


def make_book(
    bids=None,
    asks=None,
    timestamp=1700000000.0,
) -> OrderBook:
    """Helper to create an OrderBook for testing."""
    return OrderBook(
        exchange="kucoin",
        bids=sorted(bids or [], key=lambda x: x[0], reverse=True),
        asks=sorted(asks or [], key=lambda x: x[0]),
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# TestOnBookUpdate
# ---------------------------------------------------------------------------

class TestOnBookUpdate:
    """Tests for PaperTrader.on_book_update() — the core processing method."""

    @pytest.mark.asyncio
    async def test_no_fills_when_no_orders(self, paper_trader):
        """on_book_update with empty order list should produce no fills."""
        book = make_book(asks=[(0.110, 500.0)], bids=[(0.100, 500.0)])
        fills = await paper_trader.on_book_update(book, open_orders=[], current_mid=0.105)
        assert fills == []

    @pytest.mark.asyncio
    async def test_buy_fill_on_matching_book(self, paper_trader):
        """on_book_update should produce a buy fill when order price >= best ask."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].side == "buy"

    @pytest.mark.asyncio
    async def test_sell_fill_on_matching_book(self, paper_trader):
        """on_book_update should produce a sell fill when order price <= best bid."""
        order = make_order(side="sell", price=0.095, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].side == "sell"

    @pytest.mark.asyncio
    async def test_no_fill_when_book_does_not_cross(self, paper_trader):
        """on_book_update should return no fills when no order prices cross the book."""
        order = make_order(side="buy", price=0.100, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])  # Ask > order price → no fill
        fills = await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert fills == []


# ---------------------------------------------------------------------------
# TestFillPersistence
# ---------------------------------------------------------------------------

class TestFillPersistence:
    """Tests that fills are correctly persisted to the database."""

    @pytest.mark.asyncio
    async def test_fill_persisted_to_db(self, paper_trader, db):
        """A simulated fill should be written to the fills table."""
        order = make_order(id="test-buy-1", side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert len(fills) == 1

        # Query the DB directly
        rows = await db.fetch("SELECT * FROM mm_bot.fills WHERE order_id = $1", "test-buy-1")
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_multiple_book_updates_accumulate_fills(self, paper_trader, db):
        """Multiple on_book_update calls should accumulate separate fill records."""
        order1 = make_order(id="buy-1", side="buy", price=0.115, amount=50.0)
        order2 = make_order(id="buy-2", side="buy", price=0.115, amount=50.0)

        book = make_book(asks=[(0.110, 200.0)])

        await paper_trader.on_book_update(book, [order1], current_mid=0.105)
        await paper_trader.on_book_update(book, [order2], current_mid=0.105)

        count = await db.fetchval("SELECT COUNT(*) FROM mm_bot.fills")
        assert count >= 2


# ---------------------------------------------------------------------------
# TestLiveFeedEmission
# ---------------------------------------------------------------------------

class TestLiveFeedEmission:
    """Tests that fills trigger WebSocket events on the live feed."""

    @pytest.mark.asyncio
    async def test_fill_emits_order_filled_event(self, paper_trader, mock_live_feed):
        """Each fill should trigger live_feed.emit('order_filled', ...)."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        await paper_trader.on_book_update(book, [order], current_mid=0.105)
        mock_live_feed.emit.assert_called_once()
        call_args = mock_live_feed.emit.call_args
        assert call_args[0][0] == "order_filled"

    @pytest.mark.asyncio
    async def test_no_emit_when_no_fill(self, paper_trader, mock_live_feed):
        """No live feed event should be emitted when no orders fill."""
        order = make_order(side="buy", price=0.100, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])  # No fill
        await paper_trader.on_book_update(book, [order], current_mid=0.105)
        mock_live_feed.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_emitted_event_includes_simulated_flag(self, paper_trader, mock_live_feed):
        """The emitted event payload should include simulated=True."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        await paper_trader.on_book_update(book, [order], current_mid=0.105)
        payload = mock_live_feed.emit.call_args[0][1]
        assert payload.get("simulated") is True


# ---------------------------------------------------------------------------
# TestStats
# ---------------------------------------------------------------------------

class TestStats:
    """Tests for PaperTradingStats accumulation and get_stats() output."""

    @pytest.mark.asyncio
    async def test_stats_incremented_after_fill(self, paper_trader):
        """total_fills should increment by 1 for each fill."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert paper_trader.stats.total_fills == 1

    @pytest.mark.asyncio
    async def test_stats_qty_accumulated(self, paper_trader):
        """total_qty should accumulate filled amounts across calls."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        await paper_trader.on_book_update(book, [order], current_mid=0.105)
        assert paper_trader.stats.total_qty == pytest.approx(100.0)

    def test_get_stats_returns_expected_keys(self, paper_trader):
        """get_stats() should return a dict with all required fields."""
        stats = paper_trader.get_stats()
        required = {"exchange", "total_fills", "total_qty", "total_pnl_usd", "total_fees", "fill_rate_1m"}
        assert required.issubset(stats.keys())

    def test_get_stats_exchange_correct(self, paper_trader):
        """get_stats() exchange field should match the trader's exchange."""
        assert paper_trader.get_stats()["exchange"] == "kucoin"


# ---------------------------------------------------------------------------
# TestLifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    """Tests for PaperTrader start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_disconnects_ws(self, paper_trader, mock_ws_connector):
        """stop() should call disconnect_ws() on the WS connector."""
        paper_trader._running = True
        await paper_trader.stop()
        mock_ws_connector.disconnect_ws.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, paper_trader):
        """stop() should set _running to False."""
        paper_trader._running = True
        await paper_trader.stop()
        assert paper_trader._running is False

    def test_set_open_orders_updates_reference(self, paper_trader):
        """set_open_orders() should update the internal open orders reference."""
        orders = [make_order(side="buy", price=0.110, amount=100.0)]
        paper_trader.set_open_orders(orders)
        assert paper_trader._open_orders_ref == orders
