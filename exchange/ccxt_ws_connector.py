"""
CCXT WebSocket connector.

Extends CCXTConnector with live order book and ticker streaming via
ccxt.async_support's watch_order_book() and watch_ticker() methods.

Architecture:
- A background asyncio task runs the CCXT watch loop continuously.
- Each update is pushed into an asyncio.Queue (maxsize=1, drop old on full).
- watch_order_book() / watch_ticker() drain the queue non-blocking.
- On WS disconnect: exponential back-off reconnect up to MAX_RETRIES.
"""

from __future__ import annotations

import asyncio

import ccxt.pro as ccxt_pro
import ccxt.async_support as ccxt

from exchange.base import Ticker
from exchange.ccxt_connector import CCXTConnector
from exchange.ws_connector import OrderBook, WSConnector
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("ccxt_ws_connector")

_MAX_RETRIES = 5
_BASE_DELAY_S = 1.0
_QUEUE_MAXSIZE = 1  # We only care about the latest snapshot


class CCXTWSConnector(CCXTConnector, WSConnector):
    """
    CCXT-backed connector with WebSocket order book and ticker streaming.

    Inherits all REST methods from CCXTConnector and adds WS streaming
    via connect_ws() / disconnect_ws() / watch_order_book() / watch_ticker().
    """

    def __init__(
        self,
        exchange_name: str,
        symbol: str,
        credentials: dict,
        quote_currency: str = "USDT",
        ccxt_options: dict | None = None,
    ) -> None:
        CCXTConnector.__init__(
            self,
            exchange_name=exchange_name,
            symbol=symbol,
            credentials=credentials,
            quote_currency=quote_currency,
            ccxt_options=ccxt_options,
        )
        # WSConnector sets self._ws_connected = False
        WSConnector.__init__(self, exchange_name=exchange_name, symbol=symbol)

        # Create a ccxt.pro exchange instance for WebSocket streaming
        pro_class = getattr(ccxt_pro, exchange_name, None)
        if pro_class is None:
            raise ValueError(f"ccxt.pro does not support exchange: {exchange_name}")
        init_params: dict = {
            "apiKey": credentials.get("api_key", ""),
            "secret": credentials.get("api_secret", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if credentials.get("passphrase"):
            init_params["password"] = credentials["passphrase"]
        if ccxt_options:
            init_params["options"].update(ccxt_options)
        self._pro_exchange = pro_class(init_params)

        self._book_queue: asyncio.Queue[OrderBook] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._ticker_queue: asyncio.Queue[Ticker] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._book_task: asyncio.Task | None = None
        self._ticker_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # WS Lifecycle
    # ------------------------------------------------------------------

    async def connect_ws(self) -> None:
        """
        Start background WS watch loops for order book and ticker.
        Safe to call multiple times — existing tasks are cancelled first.
        """
        await self.disconnect_ws()
        log.info("ccxt_ws_connecting", exchange=self.exchange_name, symbol=self.symbol)
        self._book_task = asyncio.create_task(
            self._watch_book_loop(), name=f"ws_book_{self.exchange_name}"
        )
        self._ticker_task = asyncio.create_task(
            self._watch_ticker_loop(), name=f"ws_ticker_{self.exchange_name}"
        )
        self._ws_connected = True
        log.info("ccxt_ws_connected", exchange=self.exchange_name)

    async def disconnect_ws(self) -> None:
        """
        Cancel WS watch tasks and mark connector as disconnected.
        """
        if self._book_task and not self._book_task.done():
            self._book_task.cancel()
            try:
                await self._book_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ticker_task and not self._ticker_task.done():
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_connected = False
        try:
            await self._pro_exchange.close()
        except Exception:
            pass
        log.info("ccxt_ws_disconnected", exchange=self.exchange_name)

    # ------------------------------------------------------------------
    # Public watch methods
    # ------------------------------------------------------------------

    async def watch_order_book(self) -> OrderBook | None:
        """
        Return the latest order book snapshot, or None if none available yet.
        Non-blocking — returns immediately.
        """
        try:
            return self._book_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def watch_ticker(self) -> Ticker | None:
        """
        Return the latest ticker snapshot, or None if none available yet.
        Non-blocking — returns immediately.
        """
        try:
            return self._ticker_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------
    # Internal watch loops
    # ------------------------------------------------------------------

    async def _watch_book_loop(self) -> None:
        """
        Continuously watch the order book via CCXT and push updates to the queue.
        Reconnects with exponential back-off on WS errors.
        """
        retries = 0
        while True:
            try:
                book_data = await self._pro_exchange.watch_order_book(self.symbol)
                retries = 0  # Reset on success

                bids = [(float(p), float(q)) for p, q in (book_data.get("bids") or []) if q > 0]
                asks = [(float(p), float(q)) for p, q in (book_data.get("asks") or []) if q > 0]

                # Ensure correct sort order
                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                book = OrderBook(
                    exchange=self.exchange_name,
                    bids=bids,
                    asks=asks,
                    timestamp=float(book_data.get("timestamp") or now_s() * 1000) / 1000.0,
                )

                # Overwrite stale entry; drop if consumer is behind
                if self._book_queue.full():
                    try:
                        self._book_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await self._book_queue.put(book)

            except asyncio.CancelledError:
                break
            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    log.error("ccxt_ws_book_max_retries", exchange=self.exchange_name, error=str(exc))
                    self._ws_connected = False
                    break
                delay = _BASE_DELAY_S * (2 ** (retries - 1))
                log.warning(
                    "ccxt_ws_book_reconnect",
                    exchange=self.exchange_name,
                    attempt=retries,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                log.warning("ccxt_ws_book_error", exchange=self.exchange_name, error=str(exc))
                await asyncio.sleep(1.0)

    async def _watch_ticker_loop(self) -> None:
        """
        Continuously watch the ticker via CCXT and push updates to the queue.
        Reconnects with exponential back-off on WS errors.
        """
        retries = 0
        while True:
            try:
                ticker_data = await self._pro_exchange.watch_ticker(self.symbol)
                retries = 0

                bid = float(ticker_data.get("bid") or 0)
                ask = float(ticker_data.get("ask") or 0)
                last = float(ticker_data.get("last") or 0)
                mid = (bid + ask) / 2.0 if bid and ask else last

                ts = float(ticker_data.get("timestamp") or now_s() * 1000) / 1000.0
                ticker = Ticker(bid=bid, ask=ask, last=last, mid=mid, timestamp=ts)

                if self._ticker_queue.full():
                    try:
                        self._ticker_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await self._ticker_queue.put(ticker)

            except asyncio.CancelledError:
                break
            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    log.error("ccxt_ws_ticker_max_retries", exchange=self.exchange_name, error=str(exc))
                    break
                delay = _BASE_DELAY_S * (2 ** (retries - 1))
                log.warning(
                    "ccxt_ws_ticker_reconnect",
                    exchange=self.exchange_name,
                    attempt=retries,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                log.warning("ccxt_ws_ticker_error", exchange=self.exchange_name, error=str(exc))
                await asyncio.sleep(1.0)
