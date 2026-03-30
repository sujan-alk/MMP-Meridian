"""
Order Manager — diff-and-repost logic.

Rather than cancel-all on every tick (expensive: 2N API calls per tick across 4 exchanges),
the OrderManager diffs current open orders against the desired new grid and only:
  - Cancels orders that are no longer in the desired grid (price moved too far)
  - Places orders for levels that are new or repriced

Price buckets: an open order is considered "in the right place" if its price
is within PRICE_TOLERANCE_PCT of the desired level. This prevents churn when
the global mid ticks slightly.

Also tracks open orders in memory to reduce API calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from config.schema import ExchangeBotConfig
from db.database import Database
from db import insert_order, update_order_status
from exchange.base import BaseConnector, Order
from safety.rate_limiter import RateLimiter
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("order_manager")

# Price tolerance: if existing order is within this % of desired price, leave it in place
PRICE_TOLERANCE_PCT = 0.05  # 0.05% = 5 basis points


@dataclass
class OrderGrid:
    """The desired order grid computed by the quant model."""
    buy_prices: list[float]
    buy_amounts: list[float]   # token amounts
    sell_prices: list[float]
    sell_amounts: list[float]  # token amounts
    global_mid: float
    aggressiveness: float


class OrderManager:
    """
    Manages the open order grid for a single exchange.
    Handles diff-and-repost, dry-run mode, and DB persistence.
    """

    def __init__(
        self,
        connector: BaseConnector,
        config: ExchangeBotConfig,
        db: Database,
        rate_limiter: RateLimiter,
        live_mode: bool = False,
    ):
        self.connector = connector
        self.config = config
        self.db = db
        self.rate_limiter = rate_limiter
        self.live_mode = live_mode
        self.dry_run = not live_mode  # overridden by global dry_run setting
        self._open_orders: dict[str, Order] = {}  # id → Order (in-memory cache)
        self._last_grid: OrderGrid | None = None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def diff_and_repost(self, grid: OrderGrid) -> list[Order]:
        """
        Compare the desired grid against the current open orders.
        Cancel stale orders, place new ones.

        Returns the list of newly placed orders.
        """
        # Sync in-memory cache with exchange
        await self._sync_open_orders()

        to_cancel = self._find_stale_orders(grid)
        to_place = self._find_missing_levels(grid)

        # Cancel stale orders first
        cancelled = 0
        for order in to_cancel:
            await self._cancel_one(order)
            cancelled += 1

        if cancelled:
            log.debug(
                "orders_cancelled",
                exchange=self.config.exchange,
                count=cancelled,
            )

        # Place new orders
        placed: list[Order] = []
        for side, price, token_amount in to_place:
            order = await self._place_one(side, price, token_amount)
            if order:
                placed.append(order)

        if placed:
            log.info(
                "orders_placed",
                exchange=self.config.exchange,
                count=len(placed),
                dry_run=self.dry_run,
            )

        self._last_grid = grid
        return placed

    async def cancel_all(self) -> None:
        """Cancel all open orders. Used on emergency stop or shutdown."""
        log.warning("cancel_all_orders", exchange=self.config.exchange)
        if not self.dry_run:
            await self.rate_limiter.acquire()
            await self.connector.cancel_all_orders()
        # Update DB status for all tracked open orders
        for order_id in list(self._open_orders.keys()):
            await update_order_status(self.db, order_id, "canceled")
        self._open_orders.clear()

    # ------------------------------------------------------------------
    # Order comparison logic
    # ------------------------------------------------------------------

    def _find_stale_orders(self, grid: OrderGrid) -> list[Order]:
        """
        Returns open orders that have no matching level in the new grid.
        An order 'matches' a grid level if its price is within PRICE_TOLERANCE_PCT.
        """
        stale: list[Order] = []
        all_desired_prices = grid.buy_prices + grid.sell_prices

        for order in self._open_orders.values():
            matched = any(
                abs(order.price - desired) / max(desired, 1e-8) * 100 <= PRICE_TOLERANCE_PCT
                for desired in all_desired_prices
            )
            if not matched:
                stale.append(order)
        return stale

    def _find_missing_levels(self, grid: OrderGrid) -> list[tuple[str, float, float]]:
        """
        Returns (side, price, token_amount) tuples for levels in the grid
        that don't have a matching open order.
        """
        missing: list[tuple[str, float, float]] = []
        open_prices = {o.price: o for o in self._open_orders.values()}

        for price, token_amount in zip(grid.buy_prices, grid.buy_amounts):
            if not self._has_order_near(price, open_prices):
                missing.append(("buy", price, token_amount))

        for price, token_amount in zip(grid.sell_prices, grid.sell_amounts):
            if not self._has_order_near(price, open_prices):
                missing.append(("sell", price, token_amount))

        return missing

    @staticmethod
    def _has_order_near(target_price: float, open_prices: dict[float, Order]) -> bool:
        for price in open_prices:
            if abs(price - target_price) / max(target_price, 1e-8) * 100 <= PRICE_TOLERANCE_PCT:
                return True
        return False

    # ------------------------------------------------------------------
    # Exchange operations
    # ------------------------------------------------------------------

    async def _sync_open_orders(self) -> None:
        """Refresh the in-memory open order cache from the exchange."""
        await self.rate_limiter.acquire()
        if self.dry_run:
            return  # Nothing to sync in dry-run mode
        try:
            live_orders = await self.connector.fetch_open_orders()
            live_map = {o.id: o for o in live_orders}

            # Remove any orders that are no longer open
            stale_ids = [oid for oid in self._open_orders if oid not in live_map]
            for oid in stale_ids:
                del self._open_orders[oid]
                await update_order_status(self.db, oid, "filled")

            # Add any orders we don't know about (placed externally / on restart)
            for oid, order in live_map.items():
                if oid not in self._open_orders:
                    self._open_orders[oid] = order
        except Exception as exc:
            log.warning("sync_open_orders_failed", exchange=self.config.exchange, error=str(exc))

    async def _cancel_one(self, order: Order) -> None:
        await self.rate_limiter.acquire()
        if not self.dry_run:
            await self.connector.cancel_order(order.id)
        self._open_orders.pop(order.id, None)
        await update_order_status(self.db, order.id, "canceled")

    async def _place_one(self, side: str, price: float, token_amount: float) -> Order | None:
        if token_amount <= 0 or price <= 0:
            return None
        await self.rate_limiter.acquire()

        if self.dry_run:
            # Simulate order creation without hitting the exchange
            fake_order = Order(
                id=f"DRY-{uuid.uuid4().hex[:8]}",
                exchange=self.config.exchange,
                symbol=self.config.symbol,
                side=side,
                price=price,
                amount=token_amount,
                amount_usd=price * token_amount,
                status="open",
                timestamp=now_s(),
            )
            self._open_orders[fake_order.id] = fake_order
            await insert_order(self.db, fake_order)
            return fake_order

        try:
            order = await self.connector.create_limit_order(side, price, token_amount)
            self._open_orders[order.id] = order
            await insert_order(self.db, order)
            return order
        except Exception as exc:
            log.error(
                "place_order_failed",
                exchange=self.config.exchange,
                side=side,
                price=price,
                token_amount=token_amount,
                error=str(exc),
            )
            return None

    @property
    def open_order_count(self) -> int:
        return len(self._open_orders)

    @property
    def open_orders(self) -> list[Order]:
        return list(self._open_orders.values())
