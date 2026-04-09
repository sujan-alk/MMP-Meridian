"""
PaperTrader — coordinates WS book feed → FillSimulator → DB + LiveFeed.

One PaperTrader runs per exchange when use_websocket=True. It:
1. Polls the WS connector for new order book snapshots (non-blocking).
2. Passes the book + open orders to FillSimulator.simulate().
3. Persists SimulatedFills to the database via insert_fill().
4. Emits order_filled WebSocket events to all connected dashboard clients.
5. Tracks aggregate fill stats (total fills, fill rate per minute).

Lifecycle: start() → runs until stop(). Caller must await start() in a task.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from core.fill_simulator import FillSimulator, SimulatedFill
from db.database import Database
from db.queries import insert_fill
from exchange.base import Fill
from exchange.ws_connector import OrderBook, WSConnector
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("paper_trader")

POLL_INTERVAL_S = 0.25        # Check for new book updates 4x per second
FILL_RATE_WINDOW_S = 60.0     # Rolling window for fill rate calculation


@dataclass
class PaperTradingStats:
    """Aggregate statistics for the paper trading session."""

    total_fills: int = 0
    total_qty: float = 0.0
    total_pnl_usd: float = 0.0
    total_fees: float = 0.0
    fill_timestamps: deque = field(default_factory=lambda: deque(maxlen=1000))

    def fill_rate_1m(self) -> float:
        """Return the number of fills in the last 60 seconds."""
        cutoff = now_s() - FILL_RATE_WINDOW_S
        return sum(1 for ts in self.fill_timestamps if ts >= cutoff)


class PaperTrader:
    """
    Per-exchange paper trading coordinator.

    Wires a WSConnector's live order book into the FillSimulator and
    persists/broadcasts resulting fills.
    """

    def __init__(
        self,
        exchange: str,
        ws_connector: WSConnector,
        db: Database,
        live_feed,  # api.websocket.LiveFeed (avoid circular import)
        maker_fee_bps: float = 10.0,
    ) -> None:
        """
        Args:
            exchange: Exchange name (e.g. "kucoin").
            ws_connector: A connected WSConnector instance.
            db: Async database handle.
            live_feed: LiveFeed instance for broadcasting events.
            maker_fee_bps: Maker fee in basis points (default 0.10%).
        """
        self.exchange = exchange
        self.ws_connector = ws_connector
        self.db = db
        self.live_feed = live_feed
        self.simulator = FillSimulator(maker_fee_bps=maker_fee_bps)
        self.stats = PaperTradingStats()
        self._running = False
        self._open_orders_ref: list = []  # Updated by ExchangeBot each tick

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Connect the WS feed and begin polling for book updates.
        Runs until stop() is called.
        """
        log.info("paper_trader_starting", exchange=self.exchange)
        await self.ws_connector.connect_ws()
        self._running = True
        log.info("paper_trader_started", exchange=self.exchange)
        await self._poll_loop()

    async def stop(self) -> None:
        """Gracefully stop the paper trader and disconnect the WS feed."""
        log.info("paper_trader_stopping", exchange=self.exchange)
        self._running = False
        await self.ws_connector.disconnect_ws()
        log.info("paper_trader_stopped", exchange=self.exchange)

    # ------------------------------------------------------------------
    # Public: called by ExchangeBot each tick
    # ------------------------------------------------------------------

    async def on_book_update(
        self,
        book: OrderBook,
        open_orders: list,
        current_mid: float,
    ) -> list[SimulatedFill]:
        """
        Process one order book snapshot against the current open orders.

        Args:
            book: Latest OrderBook snapshot.
            open_orders: Current open orders from OrderManager.
            current_mid: Global mid-price for P&L calculation.

        Returns:
            List of SimulatedFill objects (may be empty).
        """
        fills = self.simulator.simulate(open_orders, book, current_mid)

        for fill in fills:
            await self._persist_fill(fill)
            await self._emit_fill_event(fill)
            self._update_stats(fill)

        return fills

    def set_open_orders(self, orders: list) -> None:
        """
        Update the reference to current open orders.
        Called by ExchangeBot after each order grid repost.
        """
        self._open_orders_ref = orders

    # ------------------------------------------------------------------
    # Stats accessor
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return current paper trading stats as a plain dict for the API."""
        return {
            "exchange": self.exchange,
            "total_fills": self.stats.total_fills,
            "total_qty": self.stats.total_qty,
            "total_pnl_usd": round(self.stats.total_pnl_usd, 6),
            "total_fees": round(self.stats.total_fees, 6),
            "fill_rate_1m": self.stats.fill_rate_1m(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Poll the WS connector for new book snapshots at POLL_INTERVAL_S.
        When a new book arrives, process it against the current open orders.
        """
        while self._running:
            try:
                book = await self.ws_connector.watch_order_book()
                if book is not None and self._open_orders_ref:
                    # We don't have current_mid here — use book mid as proxy
                    mid = book.mid or 0.0
                    await self.on_book_update(book, self._open_orders_ref, mid)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("paper_trader_poll_error", exchange=self.exchange, error=str(exc))
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _persist_fill(self, sim_fill: SimulatedFill) -> None:
        """Convert a SimulatedFill to a db Fill and persist it."""
        db_fill = Fill(
            id=f"sim_{sim_fill.order_id}_{int(sim_fill.filled_at * 1000)}",
            order_id=sim_fill.order_id,
            exchange=self.exchange,
            symbol="ALKIMI/USDT",
            side=sim_fill.side,
            filled_price=sim_fill.price,
            filled_amount=sim_fill.amount,
            fee=sim_fill.fee,
            fee_currency="USDT",
            timestamp=sim_fill.filled_at,
            pnl_usd=sim_fill.pnl_usd,
        )
        try:
            await insert_fill(self.db, db_fill, pnl_usd=sim_fill.pnl_usd)
        except Exception as exc:
            log.warning("paper_trader_persist_fill_failed", error=str(exc))

    async def _emit_fill_event(self, sim_fill: SimulatedFill) -> None:
        """Broadcast an order_filled event to WebSocket clients."""
        try:
            await self.live_feed.emit(
                "order_filled",
                {
                    "exchange": self.exchange,
                    "order_id": sim_fill.order_id,
                    "side": sim_fill.side,
                    "price": sim_fill.price,
                    "amount": sim_fill.amount,
                    "fee": sim_fill.fee,
                    "pnl_usd": sim_fill.pnl_usd,
                    "simulated": True,
                },
            )
        except Exception as exc:
            log.warning("paper_trader_emit_failed", error=str(exc))

    def _update_stats(self, fill: SimulatedFill) -> None:
        """Update rolling stats after a fill."""
        self.stats.total_fills += 1
        self.stats.total_qty += fill.amount
        self.stats.total_pnl_usd += fill.pnl_usd
        self.stats.total_fees += fill.fee
        self.stats.fill_timestamps.append(fill.filled_at)
