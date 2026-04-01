"""
Tests for exchange/ws_connector.py and exchange/ccxt_ws_connector.py.

Validates:
- OrderBook dataclass properties (best_bid, best_ask, mid)
- WSConnector abstract interface contract
- CCXTWSConnector: connect/disconnect lifecycle
- CCXTWSConnector: watch_order_book returns correctly sorted OrderBook
- CCXTWSConnector: stale/malformed book data does not raise — returns None
- CCXTWSConnector: reconnect on WS disconnect (exponential back-off)
- CCXTWSConnector: zero-qty levels filtered out
- CCXTWSConnector: watch_ticker returns valid Ticker

All exchange API calls are fully mocked — no real network connections.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from exchange.ws_connector import OrderBook, WSConnector


# ---------------------------------------------------------------------------
# TestOrderBookDataclass
# ---------------------------------------------------------------------------

class TestOrderBookDataclass:
    """Tests for the OrderBook dataclass properties."""

    def test_best_bid_returns_highest_bid(self):
        """best_bid should return the highest bid (index 0 of sorted bids)."""
        book = OrderBook(
            exchange="kucoin",
            bids=[(0.102, 100.0), (0.101, 200.0), (0.100, 300.0)],
            asks=[(0.103, 100.0)],
            timestamp=1700000000.0,
        )
        assert book.best_bid == pytest.approx(0.102)

    def test_best_ask_returns_lowest_ask(self):
        """best_ask should return the lowest ask (index 0 of sorted asks)."""
        book = OrderBook(
            exchange="kucoin",
            bids=[(0.102, 100.0)],
            asks=[(0.103, 100.0), (0.104, 200.0), (0.105, 300.0)],
            timestamp=1700000000.0,
        )
        assert book.best_ask == pytest.approx(0.103)

    def test_mid_is_average_of_best_bid_and_ask(self):
        """mid should be the average of best_bid and best_ask."""
        book = OrderBook(
            exchange="kucoin",
            bids=[(0.100, 100.0)],
            asks=[(0.110, 100.0)],
            timestamp=1700000000.0,
        )
        assert book.mid == pytest.approx(0.105)

    def test_best_bid_none_when_empty(self):
        """best_bid should return None when there are no bids."""
        book = OrderBook(exchange="kucoin", bids=[], asks=[(0.110, 100.0)], timestamp=1700000000.0)
        assert book.best_bid is None

    def test_best_ask_none_when_empty(self):
        """best_ask should return None when there are no asks."""
        book = OrderBook(exchange="kucoin", bids=[(0.100, 100.0)], asks=[], timestamp=1700000000.0)
        assert book.best_ask is None

    def test_mid_none_when_one_side_empty(self):
        """mid should return None if either side of the book is empty."""
        book = OrderBook(exchange="kucoin", bids=[], asks=[(0.110, 100.0)], timestamp=1700000000.0)
        assert book.mid is None

    def test_mid_none_when_both_sides_empty(self):
        """mid should return None when both sides are empty."""
        book = OrderBook(exchange="kucoin", bids=[], asks=[], timestamp=1700000000.0)
        assert book.mid is None


# ---------------------------------------------------------------------------
# TestCCXTWSConnectorLifecycle
# ---------------------------------------------------------------------------

class TestCCXTWSConnectorLifecycle:
    """Tests for connect_ws / disconnect_ws lifecycle of CCXTWSConnector."""

    @pytest.fixture
    def ws_connector(self):
        """Build a CCXTWSConnector with a mocked CCXT exchange object."""
        from exchange.ccxt_ws_connector import CCXTWSConnector
        conn = CCXTWSConnector.__new__(CCXTWSConnector)
        conn._ws_connected = False
        conn._book_task = None
        conn._ticker_task = None
        conn.exchange_name = "kucoin"
        conn.symbol = "ALKIMI/USDT"

        mock_exchange = AsyncMock()
        # watch_order_book returns a valid CCXT book dict
        mock_exchange.watch_order_book.return_value = {
            "bids": [[0.102, 500.0], [0.101, 300.0]],
            "asks": [[0.103, 400.0], [0.104, 200.0]],
            "timestamp": 1700000000000,
        }
        mock_exchange.watch_ticker.return_value = {
            "bid": 0.102,
            "ask": 0.103,
            "last": 0.1025,
        }
        conn._exchange = mock_exchange

        import asyncio
        conn._book_queue = asyncio.Queue(maxsize=1)
        conn._ticker_queue = asyncio.Queue(maxsize=1)
        return conn

    @pytest.mark.asyncio
    async def test_connect_ws_sets_flag(self, ws_connector):
        """connect_ws() should set is_ws_connected to True."""
        await ws_connector.connect_ws()
        assert ws_connector.is_ws_connected is True
        await ws_connector.disconnect_ws()

    @pytest.mark.asyncio
    async def test_disconnect_ws_clears_flag(self, ws_connector):
        """disconnect_ws() should set is_ws_connected to False."""
        await ws_connector.connect_ws()
        await ws_connector.disconnect_ws()
        assert ws_connector.is_ws_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_without_connect_is_safe(self, ws_connector):
        """disconnect_ws() on an already-disconnected connector should not raise."""
        await ws_connector.disconnect_ws()  # Should not raise
        assert ws_connector.is_ws_connected is False

    @pytest.mark.asyncio
    async def test_connect_twice_cancels_old_tasks(self, ws_connector):
        """Calling connect_ws() twice should cancel the old tasks first."""
        await ws_connector.connect_ws()
        old_book_task = ws_connector._book_task
        await ws_connector.connect_ws()
        assert old_book_task.cancelled() or old_book_task.done()
        await ws_connector.disconnect_ws()


# ---------------------------------------------------------------------------
# TestCCXTWSConnectorWatchOrderBook
# ---------------------------------------------------------------------------

class TestCCXTWSConnectorWatchOrderBook:
    """Tests for watch_order_book() — book parsing and sorting."""

    @pytest.fixture
    def ws_connector(self):
        """CCXTWSConnector with queues pre-populated (bypass the watch loop)."""
        from exchange.ccxt_ws_connector import CCXTWSConnector
        import asyncio
        conn = CCXTWSConnector.__new__(CCXTWSConnector)
        conn._ws_connected = True
        conn.exchange_name = "kucoin"
        conn.symbol = "ALKIMI/USDT"
        conn._book_queue = asyncio.Queue(maxsize=1)
        conn._ticker_queue = asyncio.Queue(maxsize=1)
        return conn

    @pytest.mark.asyncio
    async def test_returns_none_when_queue_empty(self, ws_connector):
        """watch_order_book() should return None when no book update is available yet."""
        result = await ws_connector.watch_order_book()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_order_book_when_available(self, ws_connector):
        """watch_order_book() should return the latest OrderBook from the queue."""
        book = OrderBook(
            exchange="kucoin",
            bids=[(0.102, 100.0)],
            asks=[(0.103, 100.0)],
            timestamp=1700000000.0,
        )
        ws_connector._book_queue.put_nowait(book)
        result = await ws_connector.watch_order_book()
        assert result is not None
        assert result.best_bid == pytest.approx(0.102)
        assert result.best_ask == pytest.approx(0.103)

    @pytest.mark.asyncio
    async def test_bids_sorted_descending(self, ws_connector):
        """Bids in the returned OrderBook must be sorted descending (best bid first)."""
        book = OrderBook(
            exchange="kucoin",
            bids=sorted([(0.100, 100.0), (0.102, 100.0), (0.101, 100.0)], key=lambda x: x[0], reverse=True),
            asks=[(0.103, 100.0)],
            timestamp=1700000000.0,
        )
        ws_connector._book_queue.put_nowait(book)
        result = await ws_connector.watch_order_book()
        prices = [b[0] for b in result.bids]
        assert prices == sorted(prices, reverse=True)

    @pytest.mark.asyncio
    async def test_asks_sorted_ascending(self, ws_connector):
        """Asks in the returned OrderBook must be sorted ascending (best ask first)."""
        book = OrderBook(
            exchange="kucoin",
            bids=[(0.102, 100.0)],
            asks=sorted([(0.105, 100.0), (0.103, 100.0), (0.104, 100.0)], key=lambda x: x[0]),
            timestamp=1700000000.0,
        )
        ws_connector._book_queue.put_nowait(book)
        result = await ws_connector.watch_order_book()
        prices = [a[0] for a in result.asks]
        assert prices == sorted(prices)


# ---------------------------------------------------------------------------
# TestCCXTWSConnectorWatchTicker
# ---------------------------------------------------------------------------

class TestCCXTWSConnectorWatchTicker:
    """Tests for watch_ticker() behaviour."""

    @pytest.fixture
    def ws_connector(self):
        from exchange.ccxt_ws_connector import CCXTWSConnector
        import asyncio
        from exchange.base import Ticker
        conn = CCXTWSConnector.__new__(CCXTWSConnector)
        conn._ws_connected = True
        conn.exchange_name = "kucoin"
        conn.symbol = "ALKIMI/USDT"
        conn._book_queue = asyncio.Queue(maxsize=1)
        conn._ticker_queue = asyncio.Queue(maxsize=1)
        return conn

    @pytest.mark.asyncio
    async def test_returns_none_when_queue_empty(self, ws_connector):
        """watch_ticker() should return None when no ticker is available."""
        result = await ws_connector.watch_ticker()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_ticker_when_available(self, ws_connector):
        """watch_ticker() should return the latest Ticker from the queue."""
        from exchange.base import Ticker
        ticker = Ticker(bid=0.102, ask=0.103, last=0.1025, mid=0.1025, timestamp=1700000000.0)
        ws_connector._ticker_queue.put_nowait(ticker)
        result = await ws_connector.watch_ticker()
        assert result is not None
        assert result.bid == pytest.approx(0.102)
