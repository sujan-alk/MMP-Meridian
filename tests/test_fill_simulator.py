"""
Tests for core/fill_simulator.py — FillSimulator.

Validates:
- Buy fill logic: order price >= best ask triggers fill
- Sell fill logic: order price <= best bid triggers fill
- No fill when price conditions are not met
- Partial fills when available book qty < order qty
- Greedy multi-level liquidity consumption
- Fee calculation (maker fee applied to fill notional)
- P&L: zero for buys, spread-capture for sells
- Empty inputs: no crash, no fills
- Zero-qty book levels are skipped
- Book timestamp is preserved in SimulatedFill
- Multiple orders resolved in a single simulate() call
"""

from __future__ import annotations

import pytest

from core.fill_simulator import DEFAULT_MAKER_FEE_BPS, FillSimulator, SimulatedFill
from exchange.ws_connector import OrderBook
from tests.conftest import make_order


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simulator() -> FillSimulator:
    """FillSimulator with 10 bps (0.10%) maker fee."""
    return FillSimulator(maker_fee_bps=10.0)


def make_book(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    timestamp: float = 1700000000.0,
    exchange: str = "kucoin",
) -> OrderBook:
    """Helper to create an OrderBook with sensible defaults."""
    return OrderBook(
        exchange=exchange,
        bids=sorted(bids or [], key=lambda x: x[0], reverse=True),
        asks=sorted(asks or [], key=lambda x: x[0]),
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# TestFillBuyOrders
# ---------------------------------------------------------------------------

class TestFillBuyOrders:
    """Tests for buy order fill logic against the ask side of the book."""

    def test_buy_at_ask_fills(self, simulator):
        """Buy order at exactly the best ask price should fill."""
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].side == "buy"
        assert fills[0].amount == pytest.approx(100.0)

    def test_buy_above_ask_fills(self, simulator):
        """Buy order priced above the best ask should fill (aggressor crosses spread)."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1

    def test_buy_below_ask_does_not_fill(self, simulator):
        """Buy order priced below the best ask should not fill (we are passive)."""
        order = make_order(side="buy", price=0.105, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 0

    def test_buy_fill_price_equals_order_price(self, simulator):
        """Fill price should be the order price (maker model — not the ask price)."""
        order = make_order(side="buy", price=0.112, amount=50.0)
        book = make_book(asks=[(0.110, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(0.112)

    def test_buy_fill_order_id_preserved(self, simulator):
        """The fill should reference the originating order_id."""
        order = make_order(id="test-order-42", side="buy", price=0.115, amount=10.0)
        book = make_book(asks=[(0.110, 50.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].order_id == "test-order-42"


# ---------------------------------------------------------------------------
# TestFillSellOrders
# ---------------------------------------------------------------------------

class TestFillSellOrders:
    """Tests for sell order fill logic against the bid side of the book."""

    def test_sell_at_bid_fills(self, simulator):
        """Sell order at exactly the best bid price should fill."""
        order = make_order(side="sell", price=0.100, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].side == "sell"
        assert fills[0].amount == pytest.approx(100.0)

    def test_sell_below_bid_fills(self, simulator):
        """Sell order priced below the best bid should fill (market crosses our ask)."""
        order = make_order(side="sell", price=0.095, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1

    def test_sell_above_bid_does_not_fill(self, simulator):
        """Sell order priced above the best bid should not fill."""
        order = make_order(side="sell", price=0.110, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 0

    def test_sell_fill_price_equals_order_price(self, simulator):
        """Sell fill price should equal the order price (maker model)."""
        order = make_order(side="sell", price=0.098, amount=50.0)
        book = make_book(bids=[(0.100, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].price == pytest.approx(0.098)


# ---------------------------------------------------------------------------
# TestPartialFills
# ---------------------------------------------------------------------------

class TestPartialFills:
    """Tests for partial fill behaviour when book qty < order qty."""

    def test_partial_buy_fill_when_ask_qty_less_than_order(self, simulator):
        """Buy order should partially fill if the ask level has less qty than needed."""
        order = make_order(side="buy", price=0.115, amount=500.0)
        book = make_book(asks=[(0.110, 100.0)])  # Only 100 available
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].amount == pytest.approx(100.0)  # Partial fill

    def test_partial_sell_fill_when_bid_qty_less_than_order(self, simulator):
        """Sell order should partially fill if the bid level has less qty than needed."""
        order = make_order(side="sell", price=0.095, amount=500.0)
        book = make_book(bids=[(0.100, 150.0)])  # Only 150 available
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].amount == pytest.approx(150.0)

    def test_multi_level_buy_fill_consumes_multiple_ask_levels(self, simulator):
        """Buy order should consume across multiple ask levels greedily."""
        order = make_order(side="buy", price=0.115, amount=250.0)
        book = make_book(asks=[(0.110, 100.0), (0.112, 100.0), (0.114, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].amount == pytest.approx(250.0)

    def test_multi_level_sell_fill_consumes_multiple_bid_levels(self, simulator):
        """Sell order should consume across multiple bid levels greedily."""
        order = make_order(side="sell", price=0.095, amount=250.0)
        book = make_book(bids=[(0.100, 100.0), (0.099, 100.0), (0.098, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].amount == pytest.approx(250.0)

    def test_fill_stops_at_level_outside_order_price(self, simulator):
        """Multi-level fill must not consume levels priced outside the order price."""
        order = make_order(side="buy", price=0.111, amount=300.0)
        # Level 3 ask (0.113) is above our order price — should not be consumed
        book = make_book(asks=[(0.110, 100.0), (0.111, 100.0), (0.113, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].amount == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# TestFeesAndPnl
# ---------------------------------------------------------------------------

class TestFeesAndPnl:
    """Tests for fee calculation and P&L accounting."""

    def test_fee_applied_to_buy_fill(self, simulator):
        """Buy fill fee = filled_qty * fill_price * maker_fee_rate."""
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        expected_fee = 100.0 * 0.110 * (10.0 / 10_000.0)
        assert fills[0].fee == pytest.approx(expected_fee)

    def test_fee_applied_to_sell_fill(self, simulator):
        """Sell fill fee = filled_qty * fill_price * maker_fee_rate."""
        order = make_order(side="sell", price=0.100, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.098)
        expected_fee = 100.0 * 0.100 * (10.0 / 10_000.0)
        assert fills[0].fee == pytest.approx(expected_fee)

    def test_buy_pnl_is_zero(self, simulator):
        """Buy fills should have zero P&L (cost basis set; realised on sell)."""
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].pnl_usd == pytest.approx(0.0)

    def test_sell_pnl_positive_when_above_mid(self, simulator):
        """Sell fill P&L should be positive when fill_price > current_mid (spread capture)."""
        order = make_order(side="sell", price=0.100, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])
        fills = simulator.simulate([order], book, current_mid=0.095)
        # pnl = (0.100 - 0.095) * 100 - fee
        assert fills[0].pnl_usd > 0

    def test_zero_fee_with_zero_bps(self):
        """FillSimulator with 0 bps should produce zero fees."""
        sim = FillSimulator(maker_fee_bps=0.0)
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])
        fills = sim.simulate([order], book, current_mid=0.105)
        assert fills[0].fee == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestBookConsumption
# ---------------------------------------------------------------------------

class TestBookConsumption:
    """Tests for correct liquidity consumption from the order book."""

    def test_liquidity_consumed_best_first(self, simulator):
        """Multiple buy orders should consume the cheapest ask levels first."""
        orders = [
            make_order(id="b1", side="buy", price=0.115, amount=100.0),
            make_order(id="b2", side="buy", price=0.113, amount=100.0),
        ]
        book = make_book(asks=[(0.110, 100.0), (0.112, 100.0)])
        fills = simulator.simulate(orders, book, current_mid=0.105)
        # Both orders should fill against available ask levels
        assert len(fills) == 2
        total_filled = sum(f.amount for f in fills)
        assert total_filled == pytest.approx(200.0)

    def test_zero_qty_levels_skipped(self, simulator):
        """Zero-qty levels in the book should be ignored (no fill, no crash)."""
        order = make_order(side="buy", price=0.115, amount=100.0)
        book = make_book(asks=[(0.110, 0.0), (0.111, 200.0)])  # First level has 0 qty
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert len(fills) == 1
        assert fills[0].amount == pytest.approx(100.0)

    def test_book_timestamp_preserved(self, simulator):
        """The book's timestamp should be carried through to each SimulatedFill."""
        order = make_order(side="buy", price=0.115, amount=50.0)
        book = make_book(asks=[(0.110, 100.0)], timestamp=1700099999.0)
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].book_timestamp == pytest.approx(1700099999.0)


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case and defensive tests."""

    def test_empty_order_list_returns_no_fills(self, simulator):
        """An empty order list should return an empty fills list without error."""
        book = make_book(asks=[(0.110, 200.0)], bids=[(0.100, 200.0)])
        fills = simulator.simulate([], book, current_mid=0.105)
        assert fills == []

    def test_empty_book_returns_no_fills(self, simulator):
        """An empty order book (no bids, no asks) should return no fills."""
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book()
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills == []

    def test_empty_asks_no_buy_fill(self, simulator):
        """Empty ask side should produce no buy fills even with valid orders."""
        order = make_order(side="buy", price=0.110, amount=100.0)
        book = make_book(bids=[(0.100, 200.0)])  # bids only
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills == []

    def test_empty_bids_no_sell_fill(self, simulator):
        """Empty bid side should produce no sell fills even with valid orders."""
        order = make_order(side="sell", price=0.100, amount=100.0)
        book = make_book(asks=[(0.110, 200.0)])  # asks only
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills == []

    def test_multiple_orders_multiple_fills(self, simulator):
        """Multiple eligible orders should each produce a fill in one simulate() call."""
        orders = [
            make_order(id="b1", side="buy", price=0.115, amount=50.0),
            make_order(id="s1", side="sell", price=0.095, amount=50.0),
        ]
        book = make_book(asks=[(0.110, 100.0)], bids=[(0.100, 100.0)])
        fills = simulator.simulate(orders, book, current_mid=0.105)
        sides = {f.side for f in fills}
        assert "buy" in sides
        assert "sell" in sides

    def test_fill_amount_cannot_exceed_order_amount(self, simulator):
        """A fill amount should never exceed the original order amount."""
        order = make_order(side="buy", price=0.115, amount=50.0)
        book = make_book(asks=[(0.110, 10000.0)])  # Massive ask liquidity
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert fills[0].amount <= order.amount + 1e-9

    def test_simulated_fill_is_dataclass_instance(self, simulator):
        """simulate() should return SimulatedFill instances, not plain dicts."""
        order = make_order(side="buy", price=0.115, amount=50.0)
        book = make_book(asks=[(0.110, 100.0)])
        fills = simulator.simulate([order], book, current_mid=0.105)
        assert isinstance(fills[0], SimulatedFill)
