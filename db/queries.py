"""
Named query functions for all database operations.
All functions accept a Database instance and use async I/O.
"""

from __future__ import annotations

import uuid
from typing import Any

from db.database import Database
from exchange.base import Order, Fill
from utils.time_utils import now_s


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def insert_order(db: Database, order: Order) -> None:
    await db.conn.execute(
        """
        INSERT OR REPLACE INTO orders
            (id, exchange, symbol, side, price, amount, amount_usd, status, placed_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order.id, order.exchange, order.symbol, order.side,
            order.price, order.amount, order.amount_usd,
            order.status, order.timestamp, now_s(),
        ),
    )
    await db.commit_unless_batching()


async def update_order_status(db: Database, order_id: str, status: str) -> None:
    await db.conn.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_s(), order_id),
    )
    await db.commit_unless_batching()


async def get_open_orders(db: Database, exchange: str) -> list[dict]:
    async with db.conn.execute(
        "SELECT * FROM orders WHERE exchange = ? AND status = 'open' ORDER BY placed_at DESC",
        (exchange,),
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_orders(db: Database, exchange: str | None = None, limit: int = 100) -> list[dict]:
    if exchange:
        async with db.conn.execute(
            "SELECT * FROM orders WHERE exchange = ? ORDER BY placed_at DESC LIMIT ?",
            (exchange, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        async with db.conn.execute(
            "SELECT * FROM orders ORDER BY placed_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

async def insert_fill(db: Database, fill: Fill, pnl_usd: float | None = None) -> None:
    await db.conn.execute(
        """
        INSERT OR IGNORE INTO fills
            (id, order_id, exchange, symbol, side, filled_price, filled_amount,
             fee, fee_currency, filled_at, pnl_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fill.id, fill.order_id, fill.exchange, fill.symbol, fill.side,
            fill.filled_price, fill.filled_amount, fill.fee, fill.fee_currency,
            fill.timestamp, pnl_usd,
        ),
    )
    await db.commit_unless_batching()


async def get_fills(
    db: Database,
    exchange: str | None = None,
    since_ts: float | None = None,
    limit: int = 200,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    if exchange:
        conditions.append("exchange = ?")
        params.append(exchange)
    if since_ts:
        conditions.append("filled_at >= ?")
        params.append(since_ts)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    async with db.conn.execute(
        f"SELECT * FROM fills {where} ORDER BY filled_at DESC LIMIT ?", params
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Inventory snapshots
# ---------------------------------------------------------------------------

async def insert_inventory_snapshot(
    db: Database,
    exchange: str,
    usd: float,
    token: float,
    global_mid: float,
    volatility: float,
    aggressiveness: float,
    skew_factor: float,
) -> None:
    await db.conn.execute(
        """
        INSERT INTO inventory_snapshots
            (exchange, usd, token, global_mid, volatility, aggressiveness, skew_factor, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (exchange, usd, token, global_mid, volatility, aggressiveness, skew_factor, now_s()),
    )
    await db.commit_unless_batching()


# ---------------------------------------------------------------------------
# RL features
# ---------------------------------------------------------------------------

async def insert_rl_features(
    db: Database,
    vol_simple: float,
    vol_zz: float | None,
    zz_regime: str | None,
    aggressiveness: float,
    global_mid: float,
    buy_spread_l1: float | None,
    sell_spread_l1: float | None,
    skew_factor: float,
    fill_rate_1m: float,
    pnl_1h: float | None,
) -> None:
    await db.conn.execute(
        """
        INSERT INTO rl_features
            (timestamp, vol_simple, vol_zz, zz_regime, aggressiveness, global_mid,
             buy_spread_l1, sell_spread_l1, skew_factor, fill_rate_1m, pnl_1h)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_s(), vol_simple, vol_zz, zz_regime, aggressiveness, global_mid,
            buy_spread_l1, sell_spread_l1, skew_factor, fill_rate_1m, pnl_1h,
        ),
    )
    await db.commit_unless_batching()


async def get_recent_pnl(db: Database, window_s: float = 3600.0) -> float:
    """Sum of realized P&L over the last window_s seconds."""
    since = now_s() - window_s
    async with db.conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) FROM fills WHERE filled_at >= ? AND pnl_usd IS NOT NULL",
        (since,),
    ) as cursor:
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0


async def get_fill_rate(db: Database, window_s: float = 60.0) -> float:
    """Number of fills per minute over the last window_s seconds."""
    since = now_s() - window_s
    async with db.conn.execute(
        "SELECT COUNT(*) FROM fills WHERE filled_at >= ?", (since,)
    ) as cursor:
        row = await cursor.fetchone()
        count = int(row[0]) if row else 0
    return count / (window_s / 60.0)
