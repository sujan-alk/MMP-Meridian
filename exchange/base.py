"""
Abstract exchange connector interface.
This is the C++ swap boundary — all callers depend only on BaseConnector.
Only exchange/factory.py imports concrete implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class Ticker:
    bid: float
    ask: float
    mid: float
    last: float
    timestamp: float   # Unix seconds


@dataclass
class Candle:
    timestamp: float   # Unix seconds (candle open time)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Balance:
    usd: float          # Quote currency (USDT or USD)
    token: float        # Base token (ALKIMI)
    quote_currency: str = "USDT"


@dataclass
class Order:
    id: str
    exchange: str
    symbol: str
    side: str           # "buy" | "sell"
    price: float
    amount: float       # Token amount
    amount_usd: float   # USD-equivalent at placement price
    status: str         # "open" | "filled" | "canceled" | "partial"
    timestamp: float    # Unix seconds
    filled_amount: float = 0.0
    filled_price: float = 0.0
    fee: float = 0.0
    fee_currency: str = ""


@dataclass
class Fill:
    """A completed (partial or full) fill on an order."""
    id: str
    order_id: str
    exchange: str
    symbol: str
    side: str
    filled_price: float
    filled_amount: float
    fee: float
    fee_currency: str
    timestamp: float


# ---------------------------------------------------------------------------
# Abstract connector
# ---------------------------------------------------------------------------

class BaseConnector(ABC):
    """
    Abstract market connector. Implement this for each exchange.
    All logic above this is exchange-agnostic.

    To swap in C++ connectors: implement this ABC in a Python wrapper
    that calls the C++ shared library, then update exchange/factory.py.
    """

    def __init__(self, exchange_name: str, symbol: str):
        self.exchange_name = exchange_name
        self.symbol = symbol
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> None:
        """Initialise the connection (load markets, authenticate, etc.)"""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close all connections and cancel orders if needed."""

    @abstractmethod
    async def fetch_ticker(self) -> Ticker:
        """Return the current best bid/ask/mid for self.symbol."""

    @abstractmethod
    async def fetch_candles(self, timeframe: str = "1m", limit: int = 15) -> list[Candle]:
        """
        Return the last `limit` closed OHLCV candles.
        timeframe: CCXT-compatible string ("1m", "5m", "1h", etc.)
        """

    @abstractmethod
    async def fetch_balance(self) -> Balance:
        """Return current ALKIMI token + quote (USDT/USD) balance."""

    @abstractmethod
    async def create_limit_order(self, side: str, price: float, amount: float) -> Order:
        """
        Place a limit order.
        side: "buy" | "sell"
        price: limit price in quote currency
        amount: token amount
        Returns the created Order.
        """

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None:
        """Cancel a single open order by ID."""

    @abstractmethod
    async def cancel_all_orders(self) -> None:
        """Cancel ALL open orders for self.symbol. Used on emergency stop."""

    @abstractmethod
    async def fetch_open_orders(self) -> list[Order]:
        """Return all currently open orders for self.symbol."""

    @abstractmethod
    async def fetch_fills(self, since_ts: float | None = None, limit: int = 100) -> list[Fill]:
        """
        Return recent fills (executed trades) since the given Unix timestamp.
        Returns an empty list if the exchange does not support this endpoint.
        since_ts: Unix seconds; fetch only fills at or after this time.
        """

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} exchange={self.exchange_name} symbol={self.symbol}>"
