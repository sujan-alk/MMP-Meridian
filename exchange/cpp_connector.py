"""
cpp_connector.py — Python async wrappers around the C++ connector shared library.

Each class inherits from BaseConnector and delegates every call to the
underlying C++ object via asyncio.get_running_loop().run_in_executor().
This keeps the asyncio event loop non-blocking while C++ does the network work.

Data conversion:
  C++ returns Ticker/Balance/Order/Fill structs (from connector.h).
  This module converts them into the Python dataclasses from exchange/base.py
  so the rest of the bot sees exactly the same types as with CCXTConnector.

Import strategy:
  If the compiled .so is not found (e.g. not yet built), a warning is logged
  and the module raises ImportError — factory.py catches this and falls back
  to CCXTConnector automatically.

Usage (via factory.py only — do not import directly):
  connector = CppKuCoinConnector(symbol, credentials, quote_currency)
  await connector.connect()
  ticker = await connector.fetch_ticker()   # returns exchange.base.Ticker
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from exchange.base import (
    BaseConnector,
    Balance,
    Candle,
    Fill,
    Order,
    Ticker,
)
from utils.logging import get_logger

log = get_logger("cpp_connector")

# ---------------------------------------------------------------------------
# Import the compiled C++ shared library
# ---------------------------------------------------------------------------
try:
    import alkimi_cpp_connectors as _cpp
    _CPP_AVAILABLE = True
    log.info("cpp_connectors_loaded", version=getattr(_cpp, "__version__", "unknown"))
except ImportError as e:
    _cpp = None  # type: ignore[assignment]
    _CPP_AVAILABLE = False
    log.warning("cpp_connectors_unavailable", error=str(e),
                msg="C++ connectors not built. Run: cd exchange/cpp/build && cmake .. && make")


# Thread pool for running blocking C++ calls without blocking the asyncio loop.
# One shared pool across all connector instances.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cpp_connector")


# ---------------------------------------------------------------------------
# Type conversion helpers — C++ struct → Python dataclass
# ---------------------------------------------------------------------------

def _to_ticker(cpp_ticker) -> Ticker:
    return Ticker(
        bid=cpp_ticker.bid,
        ask=cpp_ticker.ask,
        mid=cpp_ticker.mid,
        last=cpp_ticker.last,
        timestamp=cpp_ticker.timestamp,
    )


def _to_candle(cpp_candle) -> Candle:
    return Candle(
        timestamp=cpp_candle.timestamp,
        open=cpp_candle.open,
        high=cpp_candle.high,
        low=cpp_candle.low,
        close=cpp_candle.close,
        volume=cpp_candle.volume,
    )


def _to_balance(cpp_balance) -> Balance:
    return Balance(
        usd=cpp_balance.usd,
        token=cpp_balance.token,
        quote_currency=cpp_balance.quote_currency,
    )


def _to_order(cpp_order) -> Order:
    return Order(
        id=cpp_order.id,
        exchange=cpp_order.exchange,
        symbol=cpp_order.symbol,
        side=cpp_order.side,
        price=cpp_order.price,
        amount=cpp_order.amount,
        amount_usd=cpp_order.amount_usd,
        status=cpp_order.status,
        timestamp=cpp_order.timestamp,
        filled_amount=cpp_order.filled_amount,
        filled_price=cpp_order.filled_price,
        fee=cpp_order.fee,
        fee_currency=cpp_order.fee_currency,
    )


def _to_fill(cpp_fill) -> Fill:
    return Fill(
        id=cpp_fill.id,
        order_id=cpp_fill.order_id,
        exchange=cpp_fill.exchange,
        symbol=cpp_fill.symbol,
        side=cpp_fill.side,
        filled_price=cpp_fill.filled_price,
        filled_amount=cpp_fill.filled_amount,
        fee=cpp_fill.fee,
        fee_currency=cpp_fill.fee_currency,
        timestamp=cpp_fill.timestamp,
        pnl_usd=cpp_fill.pnl_usd,
    )


# ---------------------------------------------------------------------------
# Base async wrapper
# ---------------------------------------------------------------------------

class _CppBaseWrapper(BaseConnector):
    """
    Internal base for all C++ connector wrappers.
    Subclasses set self._cpp_conn to the C++ connector object in __init__.
    """

    def __init__(self, exchange_name: str, symbol: str):
        super().__init__(exchange_name, symbol)
        self._cpp_conn = None  # Set by subclass

    async def _run(self, fn, *args):
        """Run a blocking C++ call in the thread pool without blocking asyncio."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, fn, *args)

    # ------------------------------------------------------------------
    # BaseConnector interface — async wrappers around C++ sync calls
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        log.info("cpp_connector_connecting",
                 exchange=self.exchange_name, symbol=self.symbol)
        try:
            await self._run(self._cpp_conn.connect)
        except Exception as exc:
            log.error("cpp_connector_connect_failed",
                      exchange=self.exchange_name, error=str(exc))
            self._connected = False
            raise
        self._connected = True
        log.info("cpp_connector_connected", exchange=self.exchange_name)

    async def disconnect(self) -> None:
        log.info("cpp_connector_disconnecting", exchange=self.exchange_name)
        try:
            await self._run(self._cpp_conn.disconnect)
        except Exception as exc:
            log.warning("cpp_connector_disconnect_error",
                        exchange=self.exchange_name, error=str(exc))
        finally:
            self._connected = False

    async def fetch_ticker(self) -> Ticker:
        cpp_ticker = await self._run(self._cpp_conn.fetch_ticker)
        return _to_ticker(cpp_ticker)

    async def fetch_candles(self, timeframe: str = "1m", limit: int = 15) -> list[Candle]:
        cpp_candles = await self._run(self._cpp_conn.fetch_candles, timeframe, limit)
        return [_to_candle(c) for c in cpp_candles]

    async def fetch_balance(self) -> Balance:
        cpp_balance = await self._run(self._cpp_conn.fetch_balance)
        return _to_balance(cpp_balance)

    async def create_limit_order(self, side: str, price: float, amount: float) -> Order:
        cpp_order = await self._run(self._cpp_conn.create_limit_order, side, price, amount)
        return _to_order(cpp_order)

    async def cancel_order(self, order_id: str) -> None:
        await self._run(self._cpp_conn.cancel_order, order_id)

    async def cancel_all_orders(self) -> None:
        await self._run(self._cpp_conn.cancel_all_orders)

    async def fetch_open_orders(self) -> list[Order]:
        cpp_orders = await self._run(self._cpp_conn.fetch_open_orders)
        return [_to_order(o) for o in cpp_orders]

    async def fetch_fills(self, since_ts: float | None = None, limit: int = 100) -> list[Fill]:
        # C++ uses -1.0 sentinel for "no lower bound"; Python API uses None
        since = since_ts if since_ts is not None else -1.0
        cpp_fills = await self._run(self._cpp_conn.fetch_fills, since, limit)
        return [_to_fill(f) for f in cpp_fills]

    async def reconnect(self) -> None:
        await self.disconnect()
        await asyncio.sleep(2.0)
        await self.connect()


# ---------------------------------------------------------------------------
# Per-exchange wrappers
# ---------------------------------------------------------------------------

class CppKuCoinConnector(_CppBaseWrapper):
    """Async wrapper around the C++ KuCoinConnector."""

    def __init__(self, symbol: str, credentials: dict, quote_currency: str = "USDT"):
        super().__init__("kucoin", symbol)
        if not _CPP_AVAILABLE:
            raise ImportError("C++ connectors not built. See exchange/cpp/README.")
        self._cpp_conn = _cpp.KuCoinConnector(
            symbol=symbol,
            api_key=credentials.get("api_key", ""),
            api_secret=credentials.get("api_secret", ""),
            passphrase=credentials.get("passphrase", ""),
            quote_currency=quote_currency,
        )
        log.info("cpp_kucoin_connector_created", symbol=symbol)


class CppGateConnector(_CppBaseWrapper):
    """Async wrapper around the C++ GateConnector."""

    def __init__(self, symbol: str, credentials: dict, quote_currency: str = "USDT"):
        super().__init__("gate", symbol)
        if not _CPP_AVAILABLE:
            raise ImportError("C++ connectors not built. See exchange/cpp/README.")
        self._cpp_conn = _cpp.GateConnector(
            symbol=symbol,
            api_key=credentials.get("api_key", ""),
            api_secret=credentials.get("api_secret", ""),
            quote_currency=quote_currency,
        )
        log.info("cpp_gate_connector_created", symbol=symbol)


class CppMexcConnector(_CppBaseWrapper):
    """Async wrapper around the C++ MexcConnector."""

    def __init__(self, symbol: str, credentials: dict, quote_currency: str = "USDT"):
        super().__init__("mexc", symbol)
        if not _CPP_AVAILABLE:
            raise ImportError("C++ connectors not built. See exchange/cpp/README.")
        self._cpp_conn = _cpp.MexcConnector(
            symbol=symbol,
            api_key=credentials.get("api_key", ""),
            api_secret=credentials.get("api_secret", ""),
            quote_currency=quote_currency,
        )
        log.info("cpp_mexc_connector_created", symbol=symbol)


class CppKrakenConnector(_CppBaseWrapper):
    """Async wrapper around the C++ KrakenConnector."""

    def __init__(self, symbol: str, credentials: dict, quote_currency: str = "USD"):
        super().__init__("kraken", symbol)
        if not _CPP_AVAILABLE:
            raise ImportError("C++ connectors not built. See exchange/cpp/README.")
        self._cpp_conn = _cpp.KrakenConnector(
            symbol=symbol,
            api_key=credentials.get("api_key", ""),
            api_secret=credentials.get("api_secret", ""),
            quote_currency=quote_currency,
        )
        log.info("cpp_kraken_connector_created", symbol=symbol)
