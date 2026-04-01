"""
WebSocket connector interface.

Extends BaseConnector with live order book streaming via watch_order_book()
and watch_ticker(). Separate WS lifecycle (connect_ws/disconnect_ws) from
REST lifecycle so both can coexist on the same connector.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from exchange.base import BaseConnector, Ticker


@dataclass
class OrderBook:
    """Live order book snapshot from an exchange."""

    exchange: str
    bids: list[tuple[float, float]]  # (price, qty) sorted descending (best bid first)
    asks: list[tuple[float, float]]  # (price, qty) sorted ascending (best ask first)
    timestamp: float

    @property
    def best_bid(self) -> float | None:
        """Best (highest) bid price, or None if book is empty."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """Best (lowest) ask price, or None if book is empty."""
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        """Mid-price between best bid and best ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None


class WSConnector(BaseConnector):
    """
    Abstract base for connectors that support WebSocket order book streaming.

    REST methods are inherited from BaseConnector. Subclasses implement
    the three WS-specific abstract methods below.
    """

    def __init__(self, exchange_name: str, symbol: str) -> None:
        super().__init__(exchange_name, symbol)
        self._ws_connected = False

    @property
    def is_ws_connected(self) -> bool:
        """True if the WebSocket connection is alive."""
        return self._ws_connected

    @abstractmethod
    async def connect_ws(self) -> None:
        """
        Open the WebSocket connection and start background subscription loops.
        Must be called before watch_order_book() or watch_ticker() are usable.
        """

    @abstractmethod
    async def disconnect_ws(self) -> None:
        """
        Gracefully close the WebSocket connection and cancel subscription tasks.
        Safe to call even if never connected.
        """

    @abstractmethod
    async def watch_order_book(self) -> OrderBook | None:
        """
        Return the latest order book snapshot for self.symbol.

        Returns None if the book is not yet available or if a malformed update
        was received (errors are logged internally, never raised).

        Bids are sorted descending (best bid first).
        Asks are sorted ascending (best ask first).
        """

    @abstractmethod
    async def watch_ticker(self) -> Ticker | None:
        """
        Return the latest ticker snapshot for self.symbol.
        Returns None if not yet available.
        """
