"""
Tests for exchange/cpp_connector.py — Python async wrappers around the C++ .so.

Validates:
- .so import and _CPP_AVAILABLE flag
- All 4 connector classes instantiate correctly with various credential shapes
- All 10 BaseConnector methods are present and are coroutines on every class
- All 5 data-conversion helpers (_to_ticker, _to_candle, _to_balance, _to_order, _to_fill)
  produce the correct exchange.base dataclasses with all fields populated
- Async executor bridge (_run) correctly dispatches to thread pool and returns results
- connect() sets _connected=True; exception during connect leaves _connected=False
- disconnect() clears _connected=False even when the C++ call raises
- fetch_fills None→-1.0 sentinel translation
- fetch_candles timeframe/limit argument forwarding
- fetch_open_orders list conversion
- cancel_order / cancel_all_orders pass through correctly
- reconnect() calls disconnect then connect
- ImportError raised on instantiation when _CPP_AVAILABLE is False
- Shared _EXECUTOR is a ThreadPoolExecutor with 8 workers
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from exchange.base import Balance, Candle, Fill, Order, Ticker
from exchange.cpp_connector import (
    _CPP_AVAILABLE,
    _EXECUTOR,
    CppGateConnector,
    CppKrakenConnector,
    CppKuCoinConnector,
    CppMexcConnector,
    _CppBaseWrapper,
    _to_balance,
    _to_candle,
    _to_fill,
    _to_order,
    _to_ticker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KUCOIN_CREDS = {"api_key": "kkey", "api_secret": "ksecret", "passphrase": "kpass"}
_GATE_CREDS   = {"api_key": "gkey", "api_secret": "gsecret"}
_MEXC_CREDS   = {"api_key": "mkey", "api_secret": "msecret"}
_KRAKEN_CREDS = {"api_key": "rkey", "api_secret": "rsecret"}

ALL_10_METHODS = [
    "connect", "disconnect", "fetch_ticker", "fetch_candles", "fetch_balance",
    "create_limit_order", "cancel_order", "cancel_all_orders",
    "fetch_open_orders", "fetch_fills",
]


def _make_cpp_ticker(bid=0.10, ask=0.11, mid=0.105, last=0.105, ts=1700000000.0):
    ns = SimpleNamespace(bid=bid, ask=ask, mid=mid, last=last, timestamp=ts)
    return ns


def _make_cpp_candle(ts=1700000000.0, open=0.10, high=0.12,
                     low=0.09, close=0.11, volume=50000.0):
    return SimpleNamespace(timestamp=ts, open=open, high=high,
                           low=low, close=close, volume=volume)


def _make_cpp_balance(usd=1000.0, token=5000.0, quote_currency="USDT"):
    return SimpleNamespace(usd=usd, token=token, quote_currency=quote_currency)


def _make_cpp_order(
    id="o1", exchange="kucoin", symbol="ALKIMI/USDT", side="buy",
    price=0.10, amount=100.0, amount_usd=10.0, status="open",
    timestamp=1700000000.0, filled_amount=0.0, filled_price=0.0,
    fee=0.001, fee_currency="USDT",
):
    return SimpleNamespace(
        id=id, exchange=exchange, symbol=symbol, side=side,
        price=price, amount=amount, amount_usd=amount_usd, status=status,
        timestamp=timestamp, filled_amount=filled_amount, filled_price=filled_price,
        fee=fee, fee_currency=fee_currency,
    )


def _make_cpp_fill(
    id="f1", order_id="o1", exchange="kucoin", symbol="ALKIMI/USDT",
    side="buy", filled_price=0.10, filled_amount=100.0, fee=0.001,
    fee_currency="USDT", timestamp=1700000000.0, pnl_usd=0.5,
):
    return SimpleNamespace(
        id=id, order_id=order_id, exchange=exchange, symbol=symbol,
        side=side, filled_price=filled_price, filled_amount=filled_amount,
        fee=fee, fee_currency=fee_currency, timestamp=timestamp, pnl_usd=pnl_usd,
    )


# ---------------------------------------------------------------------------
# TestModuleImport
# ---------------------------------------------------------------------------

class TestModuleImport:
    def test_cpp_available(self):
        assert _CPP_AVAILABLE is True, (
            "C++ .so not found. Run: cd exchange/cpp/build && cmake .. && make"
        )

    def test_so_exposes_version(self):
        import alkimi_cpp_connectors as cpp
        assert hasattr(cpp, "__version__")
        assert isinstance(cpp.__version__, str)

    def test_so_exposes_all_connector_classes(self):
        import alkimi_cpp_connectors as cpp
        for cls_name in ("KuCoinConnector", "GateConnector", "MexcConnector", "KrakenConnector"):
            assert hasattr(cpp, cls_name), f"Missing C++ class: {cls_name}"

    def test_executor_is_thread_pool(self):
        from concurrent.futures import ThreadPoolExecutor
        assert isinstance(_EXECUTOR, ThreadPoolExecutor)

    def test_executor_max_workers(self):
        assert _EXECUTOR._max_workers == 8


# ---------------------------------------------------------------------------
# TestInstantiation — all 4 connector classes
# ---------------------------------------------------------------------------

class TestCppKuCoinConnectorInstantiation:
    def test_basic_creation(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn is not None

    def test_exchange_name(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn.exchange_name == "kucoin"

    def test_symbol(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn.symbol == "ALKIMI/USDT"

    def test_not_connected_at_init(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn.is_connected is False

    def test_cpp_conn_set(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert conn._cpp_conn is not None

    def test_missing_passphrase_defaults_empty(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", {"api_key": "k", "api_secret": "s"})
        assert conn._cpp_conn is not None

    def test_all_10_methods_present(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        missing = [m for m in ALL_10_METHODS if not hasattr(conn, m)]
        assert missing == []

    def test_all_10_methods_are_coroutines(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        for name in ALL_10_METHODS:
            assert inspect.iscoroutinefunction(getattr(conn, name)), (
                f"{name} is not a coroutine function"
            )


class TestCppGateConnectorInstantiation:
    def test_basic_creation(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        assert conn is not None

    def test_exchange_name(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        assert conn.exchange_name == "gate"

    def test_not_connected_at_init(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        assert conn.is_connected is False

    def test_all_10_methods_present(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        assert [m for m in ALL_10_METHODS if not hasattr(conn, m)] == []

    def test_all_10_methods_are_coroutines(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        for name in ALL_10_METHODS:
            assert inspect.iscoroutinefunction(getattr(conn, name))


class TestCppMexcConnectorInstantiation:
    def test_basic_creation(self):
        conn = CppMexcConnector("ALKIMI/USDT", _MEXC_CREDS)
        assert conn is not None

    def test_exchange_name(self):
        conn = CppMexcConnector("ALKIMI/USDT", _MEXC_CREDS)
        assert conn.exchange_name == "mexc"

    def test_default_quote_usdt(self):
        conn = CppMexcConnector("ALKIMI/USDT", _MEXC_CREDS)
        assert conn._cpp_conn is not None

    def test_all_10_methods_present(self):
        conn = CppMexcConnector("ALKIMI/USDT", _MEXC_CREDS)
        assert [m for m in ALL_10_METHODS if not hasattr(conn, m)] == []


class TestCppKrakenConnectorInstantiation:
    def test_basic_creation(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS, quote_currency="USD")
        assert conn is not None

    def test_exchange_name(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert conn.exchange_name == "kraken"

    def test_default_quote_usd(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert conn._cpp_conn is not None

    def test_symbol(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert conn.symbol == "ALKIMI/USD"

    def test_not_connected_at_init(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert conn.is_connected is False

    def test_all_10_methods_present(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert [m for m in ALL_10_METHODS if not hasattr(conn, m)] == []

    def test_all_10_methods_are_coroutines(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        for name in ALL_10_METHODS:
            assert inspect.iscoroutinefunction(getattr(conn, name))


# ---------------------------------------------------------------------------
# TestDataConversions
# ---------------------------------------------------------------------------

class TestDataConversions:
    # --- _to_ticker ---
    def test_to_ticker_returns_ticker_instance(self):
        result = _to_ticker(_make_cpp_ticker())
        assert isinstance(result, Ticker)

    def test_to_ticker_bid(self):
        assert _to_ticker(_make_cpp_ticker(bid=0.15)).bid == 0.15

    def test_to_ticker_ask(self):
        assert _to_ticker(_make_cpp_ticker(ask=0.16)).ask == 0.16

    def test_to_ticker_mid(self):
        assert _to_ticker(_make_cpp_ticker(mid=0.155)).mid == 0.155

    def test_to_ticker_last(self):
        assert _to_ticker(_make_cpp_ticker(last=0.14)).last == 0.14

    def test_to_ticker_timestamp(self):
        assert _to_ticker(_make_cpp_ticker(ts=1234567890.0)).timestamp == 1234567890.0

    # --- _to_candle ---
    def test_to_candle_returns_candle_instance(self):
        assert isinstance(_to_candle(_make_cpp_candle()), Candle)

    def test_to_candle_all_fields(self):
        c = _to_candle(_make_cpp_candle(
            ts=1700000060.0, open=0.10, high=0.12, low=0.09, close=0.11, volume=42000.0
        ))
        assert c.timestamp == 1700000060.0
        assert c.open == 0.10
        assert c.high == 0.12
        assert c.low == 0.09
        assert c.close == 0.11
        assert c.volume == 42000.0

    # --- _to_balance ---
    def test_to_balance_returns_balance_instance(self):
        assert isinstance(_to_balance(_make_cpp_balance()), Balance)

    def test_to_balance_usd(self):
        assert _to_balance(_make_cpp_balance(usd=2500.0)).usd == 2500.0

    def test_to_balance_token(self):
        assert _to_balance(_make_cpp_balance(token=9999.0)).token == 9999.0

    def test_to_balance_quote_currency(self):
        assert _to_balance(_make_cpp_balance(quote_currency="USD")).quote_currency == "USD"

    # --- _to_order ---
    def test_to_order_returns_order_instance(self):
        assert isinstance(_to_order(_make_cpp_order()), Order)

    def test_to_order_id(self):
        assert _to_order(_make_cpp_order(id="abc123")).id == "abc123"

    def test_to_order_exchange(self):
        assert _to_order(_make_cpp_order(exchange="gate")).exchange == "gate"

    def test_to_order_side(self):
        assert _to_order(_make_cpp_order(side="sell")).side == "sell"

    def test_to_order_price(self):
        assert _to_order(_make_cpp_order(price=0.0099)).price == 0.0099

    def test_to_order_amount(self):
        assert _to_order(_make_cpp_order(amount=5000.0)).amount == 5000.0

    def test_to_order_amount_usd(self):
        assert _to_order(_make_cpp_order(amount_usd=49.5)).amount_usd == 49.5

    def test_to_order_status(self):
        assert _to_order(_make_cpp_order(status="filled")).status == "filled"

    def test_to_order_filled_amount(self):
        assert _to_order(_make_cpp_order(filled_amount=50.0)).filled_amount == 50.0

    def test_to_order_filled_price(self):
        assert _to_order(_make_cpp_order(filled_price=0.0098)).filled_price == 0.0098

    def test_to_order_fee(self):
        assert _to_order(_make_cpp_order(fee=0.002)).fee == 0.002

    def test_to_order_fee_currency(self):
        assert _to_order(_make_cpp_order(fee_currency="KCS")).fee_currency == "KCS"

    # --- _to_fill ---
    def test_to_fill_returns_fill_instance(self):
        assert isinstance(_to_fill(_make_cpp_fill()), Fill)

    def test_to_fill_id(self):
        assert _to_fill(_make_cpp_fill(id="fill42")).id == "fill42"

    def test_to_fill_order_id(self):
        assert _to_fill(_make_cpp_fill(order_id="order99")).order_id == "order99"

    def test_to_fill_exchange(self):
        assert _to_fill(_make_cpp_fill(exchange="mexc")).exchange == "mexc"

    def test_to_fill_side(self):
        assert _to_fill(_make_cpp_fill(side="sell")).side == "sell"

    def test_to_fill_filled_price(self):
        assert _to_fill(_make_cpp_fill(filled_price=0.0105)).filled_price == 0.0105

    def test_to_fill_filled_amount(self):
        assert _to_fill(_make_cpp_fill(filled_amount=200.0)).filled_amount == 200.0

    def test_to_fill_pnl_usd(self):
        assert _to_fill(_make_cpp_fill(pnl_usd=1.25)).pnl_usd == 1.25

    def test_to_fill_fee(self):
        assert _to_fill(_make_cpp_fill(fee=0.003)).fee == 0.003

    def test_to_fill_timestamp(self):
        assert _to_fill(_make_cpp_fill(timestamp=1600000000.0)).timestamp == 1600000000.0


# ---------------------------------------------------------------------------
# TestAsyncBridge — verifies the executor bridge with mock C++ objects
# ---------------------------------------------------------------------------

class TestAsyncBridge:
    """Uses a mock _cpp_conn to verify _run dispatches correctly."""

    def _make_wrapper(self) -> _CppBaseWrapper:
        """Return a _CppBaseWrapper with a MagicMock as the C++ object."""
        wrapper = object.__new__(CppKuCoinConnector)
        _CppBaseWrapper.__init__(wrapper, "kucoin", "ALKIMI/USDT")
        wrapper._cpp_conn = MagicMock()
        return wrapper

    @pytest.mark.asyncio
    async def test_connect_sets_connected_flag(self):
        w = self._make_wrapper()
        w._cpp_conn.connect.return_value = None
        assert w.is_connected is False
        await w.connect()
        assert w.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_calls_cpp_connect(self):
        w = self._make_wrapper()
        await w.connect()
        w._cpp_conn.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_exception_leaves_not_connected(self):
        w = self._make_wrapper()
        w._cpp_conn.connect.side_effect = RuntimeError("WS handshake failed")
        with pytest.raises(RuntimeError):
            await w.connect()
        assert w.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_clears_connected_flag(self):
        w = self._make_wrapper()
        w._connected = True
        await w.disconnect()
        assert w.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_clears_flag_even_on_exception(self):
        w = self._make_wrapper()
        w._connected = True
        w._cpp_conn.disconnect.side_effect = RuntimeError("already disconnected")
        await w.disconnect()  # Must not raise
        assert w.is_connected is False

    @pytest.mark.asyncio
    async def test_fetch_ticker_returns_ticker(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_ticker.return_value = _make_cpp_ticker(bid=0.20, ask=0.21)
        result = await w.fetch_ticker()
        assert isinstance(result, Ticker)
        assert result.bid == 0.20
        assert result.ask == 0.21

    @pytest.mark.asyncio
    async def test_fetch_candles_returns_list_of_candle(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_candles.return_value = [
            _make_cpp_candle(close=0.11),
            _make_cpp_candle(close=0.12),
        ]
        result = await w.fetch_candles("1m", 2)
        assert len(result) == 2
        assert all(isinstance(c, Candle) for c in result)
        assert result[1].close == 0.12

    @pytest.mark.asyncio
    async def test_fetch_candles_forwards_timeframe_and_limit(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_candles.return_value = []
        await w.fetch_candles("1h", 30)
        w._cpp_conn.fetch_candles.assert_called_once_with("1h", 30)

    @pytest.mark.asyncio
    async def test_fetch_balance_returns_balance(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_balance.return_value = _make_cpp_balance(usd=500.0, token=9000.0)
        result = await w.fetch_balance()
        assert isinstance(result, Balance)
        assert result.usd == 500.0
        assert result.token == 9000.0

    @pytest.mark.asyncio
    async def test_create_limit_order_returns_order(self):
        w = self._make_wrapper()
        w._cpp_conn.create_limit_order.return_value = _make_cpp_order(
            id="new123", side="buy", price=0.005, amount=1000.0
        )
        result = await w.create_limit_order("buy", 0.005, 1000.0)
        assert isinstance(result, Order)
        assert result.id == "new123"
        assert result.side == "buy"

    @pytest.mark.asyncio
    async def test_create_limit_order_forwards_args(self):
        w = self._make_wrapper()
        w._cpp_conn.create_limit_order.return_value = _make_cpp_order()
        await w.create_limit_order("sell", 0.009, 500.0)
        w._cpp_conn.create_limit_order.assert_called_once_with("sell", 0.009, 500.0)

    @pytest.mark.asyncio
    async def test_cancel_order_forwards_id(self):
        w = self._make_wrapper()
        await w.cancel_order("order-xyz")
        w._cpp_conn.cancel_order.assert_called_once_with("order-xyz")

    @pytest.mark.asyncio
    async def test_cancel_all_orders_called(self):
        w = self._make_wrapper()
        await w.cancel_all_orders()
        w._cpp_conn.cancel_all_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_open_orders_returns_list(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_open_orders.return_value = [
            _make_cpp_order(id="o1"), _make_cpp_order(id="o2")
        ]
        result = await w.fetch_open_orders()
        assert len(result) == 2
        assert all(isinstance(o, Order) for o in result)
        assert result[0].id == "o1"

    @pytest.mark.asyncio
    async def test_fetch_fills_returns_list(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_fills.return_value = [_make_cpp_fill(id="f1")]
        result = await w.fetch_fills(since_ts=1700000000.0, limit=50)
        assert len(result) == 1
        assert isinstance(result[0], Fill)
        assert result[0].id == "f1"

    @pytest.mark.asyncio
    async def test_fetch_fills_none_maps_to_minus_one(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_fills.return_value = []
        await w.fetch_fills(since_ts=None, limit=100)
        w._cpp_conn.fetch_fills.assert_called_once_with(-1.0, 100)

    @pytest.mark.asyncio
    async def test_fetch_fills_since_ts_forwarded(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_fills.return_value = []
        await w.fetch_fills(since_ts=1700000000.0, limit=25)
        w._cpp_conn.fetch_fills.assert_called_once_with(1700000000.0, 25)

    @pytest.mark.asyncio
    async def test_reconnect_calls_disconnect_then_connect(self):
        w = self._make_wrapper()
        w._connected = True
        call_order = []
        async def mock_disconnect():
            call_order.append("disconnect")
            w._connected = False
        async def mock_connect():
            call_order.append("connect")
            w._connected = True
        w.disconnect = mock_disconnect
        w.connect   = mock_connect
        await w.reconnect()
        assert call_order[0] == "disconnect"
        assert call_order[1] == "connect"
        assert w.is_connected is True

    @pytest.mark.asyncio
    async def test_fetch_empty_open_orders_returns_empty_list(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_open_orders.return_value = []
        result = await w.fetch_open_orders()
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_empty_fills_returns_empty_list(self):
        w = self._make_wrapper()
        w._cpp_conn.fetch_fills.return_value = []
        result = await w.fetch_fills()
        assert result == []


# ---------------------------------------------------------------------------
# TestImportErrorGuard
# ---------------------------------------------------------------------------

class TestImportErrorGuard:
    def test_kucoin_raises_import_error_when_unavailable(self):
        with patch("exchange.cpp_connector._CPP_AVAILABLE", False):
            with pytest.raises(ImportError, match="C\\+\\+ connectors not built"):
                CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)

    def test_gate_raises_import_error_when_unavailable(self):
        with patch("exchange.cpp_connector._CPP_AVAILABLE", False):
            with pytest.raises(ImportError):
                CppGateConnector("ALKIMI/USDT", _GATE_CREDS)

    def test_mexc_raises_import_error_when_unavailable(self):
        with patch("exchange.cpp_connector._CPP_AVAILABLE", False):
            with pytest.raises(ImportError):
                CppMexcConnector("ALKIMI/USDT", _MEXC_CREDS)

    def test_kraken_raises_import_error_when_unavailable(self):
        with patch("exchange.cpp_connector._CPP_AVAILABLE", False):
            with pytest.raises(ImportError):
                CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)


# ---------------------------------------------------------------------------
# TestRepr
# ---------------------------------------------------------------------------

class TestRepr:
    def test_kucoin_repr(self):
        conn = CppKuCoinConnector("ALKIMI/USDT", _KUCOIN_CREDS)
        assert "CppKuCoinConnector" in repr(conn)
        assert "kucoin" in repr(conn)

    def test_gate_repr(self):
        conn = CppGateConnector("ALKIMI/USDT", _GATE_CREDS)
        assert "gate" in repr(conn)

    def test_kraken_repr(self):
        conn = CppKrakenConnector("ALKIMI/USD", _KRAKEN_CREDS)
        assert "kraken" in repr(conn)
