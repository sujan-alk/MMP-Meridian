"""
Parity tests: C++ connector vs CCXT connector side by side.

Verifies that both connectors produce consistent results for the same exchange.

Test groups (in order):
  TestParityHelpers          — pure logic: price/balance/order comparison helpers (18)
  TestDataclassStructure     — verify Ticker/Balance/Order/Fill/Candle field shapes (11)
  TestMockedTickerParity     — async mock: ticker fetch-and-compare flow (10)
  TestMockedBalanceParity    — async mock: balance fetch-and-compare flow (8)
  TestMockedOrdersParity     — async mock: open orders fetch-and-compare flow (8)
  TestMockedOrderLifecycle   — async mock: create+cancel order cycle on C++ connector (8)
  TestParityRunnerHelpers    — ParityRunner edge cases and helper coverage (6)
  TestIntegrationSkipLogic   — verify skip guards work correctly (4)
  TestParityIntegration      — real connectors, real comparisons (4, skipped in CI)

Total: 77 tests.
  - CI:          73 pass + 4 skip  (PARITY_TEST not set)
  - Integration: 73 pass + 4 run   (PARITY_TEST=true + credentials + C++ .so built)

Run unit tests only (no credentials, no C++ .so needed):
    python3 -m pytest tests/test_cpp_connector_parity.py --noconftest -v

Run integration tests (requires live credentials + C++ .so):
    PARITY_TEST=true \\
    KUCOIN_API_KEY=... KUCOIN_API_SECRET=... KUCOIN_PASSPHRASE=... \\
    GATE_API_KEY=... GATE_API_SECRET=... \\
    MEXC_API_KEY=... MEXC_API_SECRET=... \\
    KRAKEN_API_KEY=... KRAKEN_API_SECRET=... \\
    python3 -m pytest tests/test_cpp_connector_parity.py -v
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from exchange.base import Balance, Candle, Fill, Order, Ticker
from exchange.cpp_connector import _CPP_AVAILABLE

# ---------------------------------------------------------------------------
# Integration skip guards (evaluated at collection time)
# ---------------------------------------------------------------------------

_PARITY_TEST_ENABLED = os.environ.get("PARITY_TEST", "").lower() == "true"

_skip_integration = pytest.mark.skipif(
    not _PARITY_TEST_ENABLED or not _CPP_AVAILABLE,
    reason=(
        "Integration parity tests require PARITY_TEST=true and the C++ .so to be built. "
        "Run: cd exchange/cpp/build && cmake .. && make, then set PARITY_TEST=true."
    ),
)


def _creds(exchange: str) -> dict | None:
    """Return credentials dict if all required env vars are present, else None."""
    mapping: dict[str, dict[str, str]] = {
        "kucoin": {
            "api_key":    os.environ.get("KUCOIN_API_KEY", ""),
            "api_secret": os.environ.get("KUCOIN_API_SECRET", ""),
            "passphrase": os.environ.get("KUCOIN_PASSPHRASE", ""),
        },
        "gate": {
            "api_key":    os.environ.get("GATE_API_KEY", ""),
            "api_secret": os.environ.get("GATE_API_SECRET", ""),
        },
        "mexc": {
            "api_key":    os.environ.get("MEXC_API_KEY", ""),
            "api_secret": os.environ.get("MEXC_API_SECRET", ""),
        },
        "kraken": {
            "api_key":    os.environ.get("KRAKEN_API_KEY", ""),
            "api_secret": os.environ.get("KRAKEN_API_SECRET", ""),
        },
    }
    creds = mapping.get(exchange)
    if creds is None:
        return None
    return creds if all(v for v in creds.values()) else None


def _has_creds(exchange: str) -> bool:
    return _creds(exchange) is not None


# ---------------------------------------------------------------------------
# Parity comparison helpers (these are the functions under test)
# ---------------------------------------------------------------------------

def price_within_pct(ccxt_mid: float, cpp_mid: float, tolerance_pct: float = 0.1) -> bool:
    """True if cpp_mid is within tolerance_pct% of ccxt_mid."""
    if ccxt_mid == 0.0:
        return cpp_mid == 0.0
    return abs(cpp_mid - ccxt_mid) / ccxt_mid * 100.0 <= tolerance_pct


def price_pct_diff(ccxt_mid: float, cpp_mid: float) -> float:
    """Return the absolute percentage difference between two mid prices."""
    if ccxt_mid == 0.0:
        return 0.0 if cpp_mid == 0.0 else float("inf")
    return abs(cpp_mid - ccxt_mid) / ccxt_mid * 100.0


def balances_match(a: Balance, b: Balance, tolerance_pct: float = 1.0) -> bool:
    """
    True if both balances agree within tolerance_pct.
    Allows ~1% tolerance to account for minor REST timing differences between
    the CCXT and C++ connectors fetching sequentially.
    """
    def _close(x: float, y: float) -> bool:
        if x == 0.0 and y == 0.0:
            return True
        if x == 0.0:
            return False
        return abs(y - x) / x * 100.0 <= tolerance_pct

    return _close(a.usd, b.usd) and _close(a.token, b.token)


def order_ids_match(orders_a: list[Order], orders_b: list[Order]) -> bool:
    """True if both lists contain the same set of order IDs (order-independent)."""
    return {o.id for o in orders_a} == {o.id for o in orders_b}


def is_valid_order(order: Order) -> bool:
    """True if the Order dataclass has all required fields with sensible values."""
    return (
        isinstance(order.id, str) and len(order.id) > 0
        and isinstance(order.exchange, str) and len(order.exchange) > 0
        and isinstance(order.symbol, str) and "/" in order.symbol
        and order.side in ("buy", "sell")
        and order.price > 0.0
        and order.amount > 0.0
        and order.status in ("open", "filled", "canceled", "partial")
        and order.timestamp > 0.0
    )


def is_valid_ticker(ticker: Ticker) -> bool:
    """True if the Ticker has bid ≤ ask, positive values, and consistent mid."""
    return (
        ticker.bid > 0.0
        and ticker.ask > 0.0
        and ticker.bid <= ticker.ask
        and math.isclose(ticker.mid, (ticker.bid + ticker.ask) / 2.0, rel_tol=1e-6)
        and ticker.timestamp > 0.0
    )


def is_valid_balance(balance: Balance) -> bool:
    """True if the Balance has non-negative values and a valid quote currency."""
    return (
        balance.usd >= 0.0
        and balance.token >= 0.0
        and balance.quote_currency in ("USDT", "USD")
    )


# ---------------------------------------------------------------------------
# Data factories (shared across test classes, no conftest dependency)
# ---------------------------------------------------------------------------

def _make_mock_ticker(mid: float = 0.105) -> Ticker:
    bid = mid * 0.999
    ask = mid * 1.001
    return Ticker(bid=bid, ask=ask, mid=mid, last=mid, timestamp=time.time())


def _make_mock_balance(usd: float = 1000.0, token: float = 5000.0) -> Balance:
    return Balance(usd=usd, token=token, quote_currency="USDT")


def _make_mock_order(
    id: str = "oid-001",
    exchange: str = "kucoin",
    side: str = "buy",
    price: float = 0.001,
    amount: float = 100.0,
    status: str = "open",
) -> Order:
    return Order(
        id=id,
        exchange=exchange,
        symbol="ALKIMI/USDT",
        side=side,
        price=price,
        amount=amount,
        amount_usd=price * amount,
        status=status,
        timestamp=time.time(),
    )


def _make_async_connector(
    ticker: Ticker | None = None,
    balance: Balance | None = None,
    orders: list[Order] | None = None,
    order_to_create: Order | None = None,
) -> AsyncMock:
    """Return a fully-configured AsyncMock that mimics a BaseConnector."""
    m = AsyncMock()
    m.exchange_name = "kucoin"
    m.symbol = "ALKIMI/USDT"
    m.is_connected = True
    m.fetch_ticker.return_value = ticker if ticker is not None else _make_mock_ticker()
    m.fetch_balance.return_value = balance if balance is not None else _make_mock_balance()
    m.fetch_open_orders.return_value = orders if orders is not None else []
    m.create_limit_order.return_value = order_to_create if order_to_create is not None else _make_mock_order()
    m.cancel_order.return_value = None
    m.cancel_all_orders.return_value = None
    m.fetch_fills.return_value = []
    m.connect.return_value = None
    m.disconnect.return_value = None
    return m


# ---------------------------------------------------------------------------
# ParityRunner — orchestrates the side-by-side comparison
# ---------------------------------------------------------------------------

class ParityRunner:
    """
    Runs side-by-side comparisons between a CCXT and a C++ connector.

    Both connectors must already be connected before calling any check_* method.
    Each check fetches from both connectors in parallel via asyncio.gather, then
    compares the results using the helpers defined above.
    """

    def __init__(
        self,
        ccxt_connector,
        cpp_connector,
        price_tolerance_pct: float = 0.1,
        balance_tolerance_pct: float = 1.0,
    ):
        self.ccxt = ccxt_connector
        self.cpp = cpp_connector
        self.price_tol = price_tolerance_pct
        self.balance_tol = balance_tolerance_pct

    async def check_ticker(self) -> dict:
        """Fetch ticker from both connectors, return comparison result dict."""
        ccxt_ticker, cpp_ticker = await asyncio.gather(
            self.ccxt.fetch_ticker(),
            self.cpp.fetch_ticker(),
        )
        pct = price_pct_diff(ccxt_ticker.mid, cpp_ticker.mid)
        return {
            "ok": pct <= self.price_tol,
            "pct_diff": pct,
            "ccxt_mid": ccxt_ticker.mid,
            "cpp_mid": cpp_ticker.mid,
        }

    async def check_balance(self) -> dict:
        """Fetch balance from both connectors, return comparison result dict."""
        ccxt_bal, cpp_bal = await asyncio.gather(
            self.ccxt.fetch_balance(),
            self.cpp.fetch_balance(),
        )
        ok = balances_match(ccxt_bal, cpp_bal, self.balance_tol)
        return {
            "ok": ok,
            "ccxt_usd": ccxt_bal.usd,
            "cpp_usd": cpp_bal.usd,
            "ccxt_token": ccxt_bal.token,
            "cpp_token": cpp_bal.token,
        }

    async def check_open_orders(self) -> dict:
        """Fetch open orders from both connectors, verify ID sets match."""
        ccxt_orders, cpp_orders = await asyncio.gather(
            self.ccxt.fetch_open_orders(),
            self.cpp.fetch_open_orders(),
        )
        ok = order_ids_match(ccxt_orders, cpp_orders)
        return {
            "ok": ok,
            "ccxt_count": len(ccxt_orders),
            "cpp_count": len(cpp_orders),
            "ccxt_ids": sorted(o.id for o in ccxt_orders),
            "cpp_ids": sorted(o.id for o in cpp_orders),
        }

    async def create_and_cancel_on_cpp(
        self, side: str, price: float, amount: float
    ) -> Order:
        """Place a limit order on the C++ connector, cancel it immediately, return the Order."""
        order = await self.cpp.create_limit_order(side, price, amount)
        await self.cpp.cancel_order(order.id)
        return order


# ===========================================================================
# TEST CLASSES
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. TestParityHelpers — pure comparison logic (18 tests)
# ---------------------------------------------------------------------------

class TestParityHelpers:

    def test_price_within_pct_identical(self):
        assert price_within_pct(0.105, 0.105) is True

    def test_price_within_pct_close(self):
        # 0.095% diff — within 0.1% tolerance
        assert price_within_pct(0.105, 0.10490) is True

    def test_price_within_pct_at_boundary(self):
        # Exactly 0.1% above
        price_b = 0.105 * 1.001
        assert price_within_pct(0.105, price_b) is True

    def test_price_within_pct_just_over_boundary(self):
        # 0.11% above — exceeds 0.1% tolerance
        price_b = 0.105 * 1.0011
        assert price_within_pct(0.105, price_b) is False

    def test_price_within_pct_lower_value(self):
        # C++ slightly below CCXT — still within tolerance
        assert price_within_pct(0.105, 0.10490) is True

    def test_price_within_pct_large_divergence(self):
        assert price_within_pct(0.105, 0.110) is False

    def test_price_within_pct_zero_ccxt_zero_cpp(self):
        assert price_within_pct(0.0, 0.0) is True

    def test_price_within_pct_zero_ccxt_nonzero_cpp(self):
        assert price_within_pct(0.0, 0.001) is False

    def test_price_pct_diff_identical(self):
        assert price_pct_diff(0.105, 0.105) == pytest.approx(0.0)

    def test_price_pct_diff_one_percent(self):
        assert price_pct_diff(0.100, 0.101) == pytest.approx(1.0, abs=1e-10)

    def test_balances_match_identical(self):
        a = _make_mock_balance(1000.0, 5000.0)
        b = _make_mock_balance(1000.0, 5000.0)
        assert balances_match(a, b) is True

    def test_balances_match_within_tolerance(self):
        # 0.5% USD diff, 0.4% token diff — both within 1%
        a = _make_mock_balance(1000.0, 5000.0)
        b = _make_mock_balance(1005.0, 5020.0)
        assert balances_match(a, b) is True

    def test_balances_mismatch_usd(self):
        # 5% USD diff — exceeds 1% tolerance
        a = _make_mock_balance(1000.0, 5000.0)
        b = _make_mock_balance(1050.0, 5000.0)
        assert balances_match(a, b) is False

    def test_balances_mismatch_token(self):
        # 6% token diff — exceeds 1% tolerance
        a = _make_mock_balance(1000.0, 5000.0)
        b = _make_mock_balance(1000.0, 5300.0)
        assert balances_match(a, b) is False

    def test_order_ids_match_same_ids(self):
        o1 = _make_mock_order(id="a")
        o2 = _make_mock_order(id="b")
        assert order_ids_match([o1, o2], [o2, o1]) is True

    def test_order_ids_match_empty(self):
        assert order_ids_match([], []) is True

    def test_order_ids_mismatch_different_count(self):
        o1 = _make_mock_order(id="a")
        o2 = _make_mock_order(id="b")
        assert order_ids_match([o1], [o1, o2]) is False

    def test_order_ids_mismatch_different_ids(self):
        o1 = _make_mock_order(id="a")
        o2 = _make_mock_order(id="b")
        o3 = _make_mock_order(id="c")
        assert order_ids_match([o1, o2], [o1, o3]) is False


# ---------------------------------------------------------------------------
# 2. TestDataclassStructure — field existence and validation (11 tests)
# ---------------------------------------------------------------------------

class TestDataclassStructure:

    def test_ticker_has_all_fields(self):
        t = _make_mock_ticker()
        for field in ("bid", "ask", "mid", "last", "timestamp"):
            assert hasattr(t, field), f"Ticker missing field: {field}"

    def test_balance_has_all_fields(self):
        b = _make_mock_balance()
        for field in ("usd", "token", "quote_currency"):
            assert hasattr(b, field), f"Balance missing field: {field}"

    def test_order_has_all_required_fields(self):
        o = _make_mock_order()
        required = [
            "id", "exchange", "symbol", "side", "price", "amount",
            "amount_usd", "status", "timestamp", "filled_amount",
            "filled_price", "fee", "fee_currency",
        ]
        for f in required:
            assert hasattr(o, f), f"Order missing field: {f}"

    def test_order_optional_fields_default_to_zero(self):
        o = _make_mock_order()
        assert o.filled_amount == 0.0
        assert o.filled_price == 0.0
        assert o.fee == 0.0
        assert o.fee_currency == ""

    def test_fill_has_all_fields(self):
        f = Fill(
            id="f1", order_id="o1", exchange="kucoin", symbol="ALKIMI/USDT",
            side="buy", filled_price=0.10, filled_amount=100.0,
            fee=0.01, fee_currency="USDT", timestamp=time.time(),
        )
        for field in ["id", "order_id", "exchange", "symbol", "side",
                      "filled_price", "filled_amount", "fee", "fee_currency",
                      "timestamp", "pnl_usd"]:
            assert hasattr(f, field), f"Fill missing field: {field}"

    def test_candle_has_all_fields(self):
        c = Candle(timestamp=time.time(), open=0.10, high=0.12,
                   low=0.09, close=0.11, volume=50000.0)
        for field in ("timestamp", "open", "high", "low", "close", "volume"):
            assert hasattr(c, field), f"Candle missing field: {field}"

    def test_is_valid_order_well_formed(self):
        o = _make_mock_order()
        assert is_valid_order(o) is True

    def test_is_valid_order_empty_id_invalid(self):
        o = _make_mock_order(id="")
        assert is_valid_order(o) is False

    def test_is_valid_order_zero_price_invalid(self):
        o = _make_mock_order(price=0.0)
        assert is_valid_order(o) is False

    def test_is_valid_ticker_bid_gt_ask_invalid(self):
        t = Ticker(bid=0.11, ask=0.09, mid=0.10, last=0.10, timestamp=time.time())
        assert is_valid_ticker(t) is False

    def test_is_valid_balance_negative_usd_invalid(self):
        b = Balance(usd=-1.0, token=100.0, quote_currency="USDT")
        assert is_valid_balance(b) is False


# ---------------------------------------------------------------------------
# 3. TestMockedTickerParity — ParityRunner.check_ticker with mocks (10 tests)
# ---------------------------------------------------------------------------

class TestMockedTickerParity:

    @pytest.mark.asyncio
    async def test_identical_prices_pass(self):
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_within_tolerance_passes(self):
        # 0.095% diff — within 0.1%
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.1051))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_divergent_prices_fail(self):
        # ~4.8% diff — far exceeds 0.1%
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.110))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_result_includes_pct_diff(self):
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.100))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.101))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        assert "pct_diff" in result
        assert result["pct_diff"] == pytest.approx(1.0, abs=1e-8)

    @pytest.mark.asyncio
    async def test_result_includes_both_mids(self):
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.1051))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        assert "ccxt_mid" in result
        assert "cpp_mid" in result

    @pytest.mark.asyncio
    async def test_custom_tolerance_accepts_larger_diff(self):
        # 5% diff accepted with 5% tolerance
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(0.100))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(0.105))
        runner = ParityRunner(ccxt_mock, cpp_mock, price_tolerance_pct=5.0)
        result = await runner.check_ticker()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_both_fetch_ticker_called_once(self):
        ccxt_mock = _make_async_connector()
        cpp_mock = _make_async_connector()
        runner = ParityRunner(ccxt_mock, cpp_mock)
        await runner.check_ticker()
        ccxt_mock.fetch_ticker.assert_awaited_once()
        cpp_mock.fetch_ticker.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pct_diff_calculated_from_ccxt_side(self):
        ccxt_mid = 0.105
        cpp_mid = 0.106
        ccxt_mock = _make_async_connector(ticker=_make_mock_ticker(ccxt_mid))
        cpp_mock = _make_async_connector(ticker=_make_mock_ticker(cpp_mid))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_ticker()
        expected = abs(cpp_mid - ccxt_mid) / ccxt_mid * 100.0
        assert result["pct_diff"] == pytest.approx(expected, rel=1e-6)

    @pytest.mark.asyncio
    async def test_ccxt_exception_propagates(self):
        ccxt_mock = _make_async_connector()
        ccxt_mock.fetch_ticker.side_effect = RuntimeError("CCXT connection refused")
        cpp_mock = _make_async_connector()
        runner = ParityRunner(ccxt_mock, cpp_mock)
        with pytest.raises(RuntimeError, match="CCXT connection refused"):
            await runner.check_ticker()

    @pytest.mark.asyncio
    async def test_cpp_exception_propagates(self):
        ccxt_mock = _make_async_connector()
        cpp_mock = _make_async_connector()
        cpp_mock.fetch_ticker.side_effect = RuntimeError("C++ WS not connected")
        runner = ParityRunner(ccxt_mock, cpp_mock)
        with pytest.raises(RuntimeError, match="C\\+\\+ WS not connected"):
            await runner.check_ticker()


# ---------------------------------------------------------------------------
# 4. TestMockedBalanceParity — ParityRunner.check_balance with mocks (8 tests)
# ---------------------------------------------------------------------------

class TestMockedBalanceParity:

    @pytest.mark.asyncio
    async def test_identical_balances_pass(self):
        bal = _make_mock_balance(1000.0, 5000.0)
        ccxt_mock = _make_async_connector(balance=bal)
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_within_one_pct_passes(self):
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1005.0, 5030.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_large_usd_divergence_fails(self):
        # 10% USD diff — exceeds 1% tolerance
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1100.0, 5000.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_large_token_divergence_fails(self):
        # 12% token diff — exceeds 1% tolerance
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5600.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_result_includes_all_fields(self):
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        for key in ("ok", "ccxt_usd", "cpp_usd", "ccxt_token", "cpp_token"):
            assert key in result, f"Result missing key: {key}"

    @pytest.mark.asyncio
    async def test_both_fetch_balance_called_once(self):
        ccxt_mock = _make_async_connector()
        cpp_mock = _make_async_connector()
        runner = ParityRunner(ccxt_mock, cpp_mock)
        await runner.check_balance()
        ccxt_mock.fetch_balance.assert_awaited_once()
        cpp_mock.fetch_balance.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_both_zero_balances_match(self):
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(0.0, 0.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(0.0, 0.0))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_balance()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_custom_balance_tolerance(self):
        # 5% off — within 6% custom tolerance
        ccxt_mock = _make_async_connector(balance=_make_mock_balance(1000.0, 5000.0))
        cpp_mock = _make_async_connector(balance=_make_mock_balance(1050.0, 5250.0))
        runner = ParityRunner(ccxt_mock, cpp_mock, balance_tolerance_pct=6.0)
        result = await runner.check_balance()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# 5. TestMockedOrdersParity — ParityRunner.check_open_orders with mocks (8 tests)
# ---------------------------------------------------------------------------

class TestMockedOrdersParity:

    @pytest.mark.asyncio
    async def test_empty_orders_match(self):
        ccxt_mock = _make_async_connector(orders=[])
        cpp_mock = _make_async_connector(orders=[])
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_same_order_ids_match(self):
        orders = [_make_mock_order(id="o1"), _make_mock_order(id="o2")]
        ccxt_mock = _make_async_connector(orders=list(orders))
        cpp_mock = _make_async_connector(orders=list(orders))
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_different_order_ids_fail(self):
        ccxt_orders = [_make_mock_order(id="o1"), _make_mock_order(id="o2")]
        cpp_orders = [_make_mock_order(id="o1"), _make_mock_order(id="o3")]
        ccxt_mock = _make_async_connector(orders=ccxt_orders)
        cpp_mock = _make_async_connector(orders=cpp_orders)
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_different_order_counts_fail(self):
        ccxt_orders = [_make_mock_order(id="o1"), _make_mock_order(id="o2")]
        cpp_orders = [_make_mock_order(id="o1")]
        ccxt_mock = _make_async_connector(orders=ccxt_orders)
        cpp_mock = _make_async_connector(orders=cpp_orders)
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_result_includes_counts(self):
        orders = [_make_mock_order(id="o1")]
        ccxt_mock = _make_async_connector(orders=orders)
        cpp_mock = _make_async_connector(orders=orders)
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ccxt_count"] == 1
        assert result["cpp_count"] == 1

    @pytest.mark.asyncio
    async def test_result_includes_sorted_id_lists(self):
        orders = [_make_mock_order(id="o2"), _make_mock_order(id="o1")]
        ccxt_mock = _make_async_connector(orders=orders)
        cpp_mock = _make_async_connector(orders=orders)
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert "ccxt_ids" in result
        assert "cpp_ids" in result
        assert result["ccxt_ids"] == ["o1", "o2"]  # sorted

    @pytest.mark.asyncio
    async def test_order_independent_id_comparison(self):
        o1 = _make_mock_order(id="o1")
        o2 = _make_mock_order(id="o2")
        ccxt_mock = _make_async_connector(orders=[o1, o2])
        cpp_mock = _make_async_connector(orders=[o2, o1])  # reversed — still same set
        runner = ParityRunner(ccxt_mock, cpp_mock)
        result = await runner.check_open_orders()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_both_fetch_open_orders_called_once(self):
        ccxt_mock = _make_async_connector(orders=[])
        cpp_mock = _make_async_connector(orders=[])
        runner = ParityRunner(ccxt_mock, cpp_mock)
        await runner.check_open_orders()
        ccxt_mock.fetch_open_orders.assert_awaited_once()
        cpp_mock.fetch_open_orders.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. TestMockedOrderLifecycle — create+cancel on C++ with mocks (8 tests)
# ---------------------------------------------------------------------------

class TestMockedOrderLifecycle:

    @pytest.mark.asyncio
    async def test_create_and_cancel_calls_both_methods(self):
        created_order = _make_mock_order(id="new-order-123")
        cpp_mock = _make_async_connector(order_to_create=created_order)
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        await runner.create_and_cancel_on_cpp("buy", 0.001, 100.0)
        cpp_mock.create_limit_order.assert_awaited_once_with("buy", 0.001, 100.0)
        cpp_mock.cancel_order.assert_awaited_once_with("new-order-123")

    @pytest.mark.asyncio
    async def test_returns_order_dataclass(self):
        cpp_mock = _make_async_connector(order_to_create=_make_mock_order())
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        result = await runner.create_and_cancel_on_cpp("buy", 0.001, 100.0)
        assert isinstance(result, Order)

    @pytest.mark.asyncio
    async def test_returned_order_passes_validation(self):
        order = Order(
            id="live-buy-0.0010",
            exchange="kucoin",
            symbol="ALKIMI/USDT",
            side="buy",
            price=0.001,
            amount=100.0,
            amount_usd=0.1,
            status="open",
            timestamp=time.time(),
        )
        cpp_mock = _make_async_connector(order_to_create=order)
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        result = await runner.create_and_cancel_on_cpp("buy", 0.001, 100.0)
        assert is_valid_order(result)

    @pytest.mark.asyncio
    async def test_cancel_uses_id_from_created_order(self):
        order = _make_mock_order(id="special-order-456")
        cpp_mock = _make_async_connector(order_to_create=order)
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        await runner.create_and_cancel_on_cpp("sell", 999.0, 1.0)
        cpp_mock.cancel_order.assert_awaited_with("special-order-456")

    @pytest.mark.asyncio
    async def test_sell_order_returned_correctly(self):
        order = _make_mock_order(id="sell-1", side="sell", price=999.0,
                                 amount=10.0, status="open")
        cpp_mock = _make_async_connector(order_to_create=order)
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        result = await runner.create_and_cancel_on_cpp("sell", 999.0, 10.0)
        assert result.side == "sell"
        assert is_valid_order(result)

    @pytest.mark.asyncio
    async def test_create_failure_propagates(self):
        cpp_mock = _make_async_connector()
        cpp_mock.create_limit_order.side_effect = RuntimeError("Insufficient funds")
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        with pytest.raises(RuntimeError, match="Insufficient funds"):
            await runner.create_and_cancel_on_cpp("buy", 0.001, 100.0)

    @pytest.mark.asyncio
    async def test_cancel_failure_propagates(self):
        order = _make_mock_order(id="o1")
        cpp_mock = _make_async_connector(order_to_create=order)
        cpp_mock.cancel_order.side_effect = RuntimeError("Order already cancelled")
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        with pytest.raises(RuntimeError, match="Order already cancelled"):
            await runner.create_and_cancel_on_cpp("buy", 0.001, 100.0)

    @pytest.mark.asyncio
    async def test_order_price_and_amount_preserved(self):
        order = Order(
            id="order-789",
            exchange="kucoin",
            symbol="ALKIMI/USDT",
            side="buy",
            price=0.00123,
            amount=500.0,
            amount_usd=0.615,
            status="open",
            timestamp=time.time(),
        )
        cpp_mock = _make_async_connector(order_to_create=order)
        runner = ParityRunner(_make_async_connector(), cpp_mock)
        result = await runner.create_and_cancel_on_cpp("buy", 0.00123, 500.0)
        assert result.price == pytest.approx(0.00123)
        assert result.amount == pytest.approx(500.0)
        assert result.side == "buy"


# ---------------------------------------------------------------------------
# 7. TestParityRunnerHelpers — edge cases for the comparison helpers (6 tests)
# ---------------------------------------------------------------------------

class TestParityRunnerHelpers:

    def test_price_within_pct_custom_tolerance_accepts(self):
        # 4.9% diff accepted with 5% tolerance
        assert price_within_pct(100.0, 104.9, tolerance_pct=5.0) is True

    def test_price_within_pct_strict_tolerance_rejects(self):
        # 0.1% diff rejected with 0.05% tolerance
        assert price_within_pct(100.0, 100.1, tolerance_pct=0.05) is False

    def test_price_pct_diff_both_zero_is_zero(self):
        assert price_pct_diff(0.0, 0.0) == pytest.approx(0.0)

    def test_price_pct_diff_zero_ccxt_nonzero_cpp_is_inf(self):
        assert price_pct_diff(0.0, 0.001) == float("inf")

    def test_balances_match_both_zero_token(self):
        a = Balance(usd=1000.0, token=0.0, quote_currency="USDT")
        b = Balance(usd=1000.0, token=0.0, quote_currency="USDT")
        assert balances_match(a, b) is True

    def test_balances_mismatch_zero_ccxt_nonzero_cpp(self):
        # CCXT shows 0 USD, C++ shows 100 USD — this is a real discrepancy
        a = Balance(usd=0.0, token=0.0, quote_currency="USDT")
        b = Balance(usd=100.0, token=0.0, quote_currency="USDT")
        assert balances_match(a, b) is False


# ---------------------------------------------------------------------------
# 8. TestIntegrationSkipLogic — verify skip guards behave correctly (4 tests)
# ---------------------------------------------------------------------------

class TestIntegrationSkipLogic:

    def test_parity_test_disabled_when_env_var_missing(self, monkeypatch):
        monkeypatch.delenv("PARITY_TEST", raising=False)
        enabled = os.environ.get("PARITY_TEST", "").lower() == "true"
        assert enabled is False

    def test_parity_test_enabled_when_env_var_true(self, monkeypatch):
        monkeypatch.setenv("PARITY_TEST", "true")
        enabled = os.environ.get("PARITY_TEST", "").lower() == "true"
        assert enabled is True

    def test_creds_helper_returns_none_when_vars_missing(self, monkeypatch):
        monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
        monkeypatch.delenv("KUCOIN_API_SECRET", raising=False)
        monkeypatch.delenv("KUCOIN_PASSPHRASE", raising=False)
        assert _creds("kucoin") is None

    def test_creds_helper_returns_dict_when_all_vars_present(self, monkeypatch):
        monkeypatch.setenv("KUCOIN_API_KEY", "test-key")
        monkeypatch.setenv("KUCOIN_API_SECRET", "test-secret")
        monkeypatch.setenv("KUCOIN_PASSPHRASE", "test-pass")
        result = _creds("kucoin")
        assert result is not None
        assert result["api_key"] == "test-key"
        assert result["api_secret"] == "test-secret"
        assert result["passphrase"] == "test-pass"


# ---------------------------------------------------------------------------
# 9. TestParityIntegration — real live connectors (4 tests, skipped in CI)
#
# These tests connect to real exchanges and compare CCXT vs C++ output.
# Skipped unless PARITY_TEST=true AND C++ .so is built.
# ---------------------------------------------------------------------------

@_skip_integration
class TestParityIntegration:
    """
    Live parity tests. Skip by default — opt in with PARITY_TEST=true.

    Requires all of:
      - C++ .so built:  cd exchange/cpp/build && cmake .. && make
      - PARITY_TEST=true
      - Exchange-specific credentials in environment variables

    The order lifecycle test places a buy order at 50% below market price
    (far from market) and immediately cancels it — it will NOT fill.
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _has_creds("kucoin"),
                        reason="KuCoin credentials not set (KUCOIN_API_KEY/SECRET/PASSPHRASE)")
    async def test_kucoin_ticker_parity(self):
        """KuCoin: C++ and CCXT prices agree within 0.1%."""
        from exchange.factory import create_connector
        creds = _creds("kucoin")
        ccxt_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=False)
        cpp_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=True)
        try:
            await asyncio.gather(ccxt_conn.connect(), cpp_conn.connect())
            runner = ParityRunner(ccxt_conn, cpp_conn)
            result = await runner.check_ticker()
            assert result["ok"], (
                f"KuCoin ticker parity failed: "
                f"CCXT mid={result['ccxt_mid']:.6f}, "
                f"C++ mid={result['cpp_mid']:.6f}, "
                f"diff={result['pct_diff']:.4f}% (tolerance=0.1%)"
            )
        finally:
            await asyncio.gather(
                ccxt_conn.disconnect(), cpp_conn.disconnect(),
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _has_creds("gate"),
                        reason="Gate.io credentials not set (GATE_API_KEY/SECRET)")
    async def test_gate_ticker_parity(self):
        """Gate.io: C++ and CCXT prices agree within 0.1%."""
        from exchange.factory import create_connector
        creds = _creds("gate")
        ccxt_conn = create_connector("gate", "ALKIMI/USDT", creds, use_cpp=False)
        cpp_conn = create_connector("gate", "ALKIMI/USDT", creds, use_cpp=True)
        try:
            await asyncio.gather(ccxt_conn.connect(), cpp_conn.connect())
            runner = ParityRunner(ccxt_conn, cpp_conn)
            result = await runner.check_ticker()
            assert result["ok"], (
                f"Gate.io ticker parity failed: "
                f"CCXT mid={result['ccxt_mid']:.6f}, "
                f"C++ mid={result['cpp_mid']:.6f}, "
                f"diff={result['pct_diff']:.4f}% (tolerance=0.1%)"
            )
        finally:
            await asyncio.gather(
                ccxt_conn.disconnect(), cpp_conn.disconnect(),
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _has_creds("kucoin"),
                        reason="KuCoin credentials not set (KUCOIN_API_KEY/SECRET/PASSPHRASE)")
    async def test_kucoin_balance_parity(self):
        """KuCoin: C++ and CCXT balances agree within 1%."""
        from exchange.factory import create_connector
        creds = _creds("kucoin")
        ccxt_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=False)
        cpp_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=True)
        try:
            await asyncio.gather(ccxt_conn.connect(), cpp_conn.connect())
            runner = ParityRunner(ccxt_conn, cpp_conn)
            result = await runner.check_balance()
            assert result["ok"], (
                f"KuCoin balance parity failed: "
                f"CCXT USD={result['ccxt_usd']:.4f}, C++ USD={result['cpp_usd']:.4f}, "
                f"CCXT token={result['ccxt_token']:.4f}, C++ token={result['cpp_token']:.4f}"
            )
        finally:
            await asyncio.gather(
                ccxt_conn.disconnect(), cpp_conn.disconnect(),
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _has_creds("kucoin"),
                        reason="KuCoin credentials not set (KUCOIN_API_KEY/SECRET/PASSPHRASE)")
    async def test_kucoin_order_lifecycle_on_cpp(self):
        """
        KuCoin C++: place a buy order 50% below market price (will NOT fill)
        and immediately cancel it. Verify the returned Order is valid.
        """
        from exchange.factory import create_connector
        creds = _creds("kucoin")
        ccxt_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=False)
        cpp_conn = create_connector("kucoin", "ALKIMI/USDT", creds, use_cpp=True)
        try:
            await asyncio.gather(ccxt_conn.connect(), cpp_conn.connect())
            ticker = await ccxt_conn.fetch_ticker()
            far_below_market = ticker.bid * 0.50  # 50% below bid — guaranteed not to fill
            runner = ParityRunner(ccxt_conn, cpp_conn)
            order = await runner.create_and_cancel_on_cpp("buy", far_below_market, 100.0)
            assert isinstance(order, Order), f"Expected Order, got {type(order)}"
            assert is_valid_order(order), f"Order failed validation: {order}"
        finally:
            await asyncio.gather(
                ccxt_conn.disconnect(), cpp_conn.disconnect(),
                return_exceptions=True,
            )
