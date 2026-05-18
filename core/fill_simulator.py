"""
Fill Simulator — paper trading fill engine.

Simulates order fills by checking each open order against the live order book.
Uses a maker model: the bot is always the passive side, so fills occur only
when the market crosses our price level.

Fill rules:
  Buy order fills  if order_price >= best_ask AND available ask qty >= order qty (partial ok)
  Sell order fills if order_price <= best_bid AND available bid qty >= order qty (partial ok)

Liquidity is consumed greedily from the best level outward. Partial fills are
allowed when available qty < order qty.

P&L:
  Sell fills: (fill_price - current_mid) * amount   (spread capture proxy)
  Buy fills:  0 (cost basis established; realised on future sell)

Fee: maker fee applied as fraction of fill notional.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from exchange.base import Order
from exchange.ws_connector import OrderBook
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("fill_simulator")

DEFAULT_MAKER_FEE_BPS = 10.0  # 0.10% maker fee


@dataclass
class SimulatedFill:
    """A single simulated fill produced by FillSimulator.simulate()."""

    order_id: str
    exchange: str
    side: str            # "buy" | "sell"
    price: float         # fill price (= order price — maker model)
    amount: float        # token quantity filled
    fee: float           # fee in quote currency
    filled_at: float     # unix timestamp
    pnl_usd: float       # realised P&L in USD (0 for buys, spread capture for sells)
    book_timestamp: float  # timestamp of the order book used for this fill


class FillSimulator:
    """
    Simulates maker fills against a live order book snapshot.

    Instantiate once per exchange and call simulate() on every book update.
    The simulator is stateless — all context is passed in via arguments.
    """

    def __init__(self, maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS) -> None:
        """
        Args:
            maker_fee_bps: Maker fee in basis points (e.g. 10 = 0.10%).
        """
        self.maker_fee_rate = maker_fee_bps / 10_000.0

    def simulate(
        self,
        open_orders: list[Order],
        book: OrderBook,
        current_mid: float,
    ) -> list[SimulatedFill]:
        """
        Check each open order against the live book and return fills.

        Args:
            open_orders: List of currently open orders from OrderManager.
            book: Latest OrderBook snapshot from the WS connector.
            current_mid: Global mid-price at the time of this check (used for P&L).

        Returns:
            List of SimulatedFill objects for orders that would have been hit.
        """
        if not open_orders or (not book.bids and not book.asks):
            return []

        fills: list[SimulatedFill] = []

        # Work on mutable copies of the book so we consume liquidity correctly
        available_asks = list(book.asks)   # ascending: best ask first
        available_bids = list(book.bids)   # descending: best bid first

        # Sort orders: buys by price desc (best buy first), sells by price asc
        buy_orders = sorted(
            [o for o in open_orders if o.side == "buy"],
            key=lambda o: o.price,
            reverse=True,
        )
        sell_orders = sorted(
            [o for o in open_orders if o.side == "sell"],
            key=lambda o: o.price,
        )

        # Process buy orders against ask side
        for order in buy_orders:
            fill = self._try_fill_buy(order, available_asks, book.timestamp, current_mid)
            if fill:
                fills.append(fill)

        # Process sell orders against bid side
        for order in sell_orders:
            fill = self._try_fill_sell(order, available_bids, book.timestamp, current_mid)
            if fill:
                fills.append(fill)

        if fills:
            log.info(
                "simulated_fills",
                exchange=book.exchange,
                count=len(fills),
                total_qty=sum(f.amount for f in fills),
            )

        return fills

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_fill_buy(
        self,
        order: Order,
        available_asks: list[tuple[float, float]],
        book_ts: float,
        current_mid: float,
    ) -> SimulatedFill | None:
        """
        Attempt to fill a buy order against the ask side of the book.

        A buy order fills when its price >= best ask. Liquidity is consumed
        greedily from the cheapest ask level. Partial fills are supported.

        Returns a SimulatedFill or None if the order does not fill.
        """
        if not available_asks:
            return None
        best_ask_price, best_ask_qty = available_asks[0]
        if order.price < best_ask_price:
            return None  # Our bid is below the market — no fill

        # Determine fill quantity (partial fill if ask qty < order qty)
        remaining_order = order.amount
        filled_qty = 0.0
        fill_price = order.price  # Maker model: we fill at our order price

        i = 0
        while i < len(available_asks) and remaining_order > 0:
            ask_price, ask_qty = available_asks[i]
            if ask_price > order.price:
                break  # Ask level is above our price — stop
            if ask_qty <= 0:
                i += 1
                continue

            take = min(remaining_order, ask_qty)
            filled_qty += take
            remaining_order -= take

            # Consume liquidity from this level
            new_qty = ask_qty - take
            if new_qty > 1e-12:
                available_asks[i] = (ask_price, new_qty)
            else:
                available_asks.pop(i)
                # Don't increment i — next item shifts down
                continue
            i += 1

        if filled_qty <= 0:
            return None

        fee = filled_qty * fill_price * self.maker_fee_rate
        return SimulatedFill(
            order_id=order.id,
            exchange=order.exchange if hasattr(order, "exchange") else "",
            side="buy",
            price=fill_price,
            amount=filled_qty,
            fee=fee,
            filled_at=now_s(),
            pnl_usd=0.0,  # Cost basis set; realised on sell
            book_timestamp=book_ts,
        )

    def _try_fill_sell(
        self,
        order: Order,
        available_bids: list[tuple[float, float]],
        book_ts: float,
        current_mid: float,
    ) -> SimulatedFill | None:
        """
        Attempt to fill a sell order against the bid side of the book.

        A sell order fills when its price <= best bid. Liquidity is consumed
        greedily from the highest bid level. Partial fills are supported.

        P&L = (fill_price - current_mid) * amount (spread capture proxy).

        Returns a SimulatedFill or None if the order does not fill.
        """
        if not available_bids:
            return None
        best_bid_price, best_bid_qty = available_bids[0]
        if order.price > best_bid_price:
            return None  # Our ask is above the market — no fill

        remaining_order = order.amount
        filled_qty = 0.0
        fill_price = order.price  # Maker model

        i = 0
        while i < len(available_bids) and remaining_order > 0:
            bid_price, bid_qty = available_bids[i]
            if bid_price < order.price:
                break
            if bid_qty <= 0:
                i += 1
                continue

            take = min(remaining_order, bid_qty)
            filled_qty += take
            remaining_order -= take

            new_qty = bid_qty - take
            if new_qty > 1e-12:
                available_bids[i] = (bid_price, new_qty)
            else:
                available_bids.pop(i)
                continue
            i += 1

        if filled_qty <= 0:
            return None

        fee = filled_qty * fill_price * self.maker_fee_rate
        pnl = (fill_price - current_mid) * filled_qty - fee

        return SimulatedFill(
            order_id=order.id,
            exchange=order.exchange if hasattr(order, "exchange") else "",
            side="sell",
            price=fill_price,
            amount=filled_qty,
            fee=fee,
            filled_at=now_s(),
            pnl_usd=pnl,
            book_timestamp=book_ts,
        )
