"""
Named query functions for all database operations.
All functions accept a Database instance and use async I/O via asyncpg.

All table references use the mm_bot schema prefix to work correctly
with Supabase's connection pooler (PgBouncer in transaction mode),
which does not persist SET search_path across queries.
"""

from __future__ import annotations

from typing import Any

from db.database import Database
from db.migrations import SCHEMA_NAME as S
from exchange.base import Order, Fill
from utils.time_utils import now_s


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def insert_order(db: Database, order: Order) -> None:
    await db.execute(
        f"""
        INSERT INTO {S}.orders
            (id, exchange, symbol, side, price, amount, amount_usd, status, placed_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (id) DO UPDATE SET
            exchange = $2, symbol = $3, side = $4, price = $5,
            amount = $6, amount_usd = $7, status = $8,
            placed_at = $9, updated_at = $10
        """,
        order.id, order.exchange, order.symbol, order.side,
        order.price, order.amount, order.amount_usd,
        order.status, order.timestamp, now_s(),
    )
    await db.commit_unless_batching()


async def update_order_status(db: Database, order_id: str, status: str) -> None:
    await db.execute(
        f"UPDATE {S}.orders SET status = $1, updated_at = $2 WHERE id = $3",
        status, now_s(), order_id,
    )
    await db.commit_unless_batching()


async def get_open_orders(db: Database, exchange: str) -> list[dict]:
    rows = await db.fetch(
        f"SELECT * FROM {S}.orders WHERE exchange = $1 AND status = 'open' ORDER BY placed_at DESC",
        exchange,
    )
    return [dict(row) for row in rows]


async def get_orders(db: Database, exchange: str | None = None, limit: int = 100) -> list[dict]:
    if exchange:
        rows = await db.fetch(
            f"SELECT * FROM {S}.orders WHERE exchange = $1 ORDER BY placed_at DESC LIMIT $2",
            exchange, limit,
        )
    else:
        rows = await db.fetch(
            f"SELECT * FROM {S}.orders ORDER BY placed_at DESC LIMIT $1",
            limit,
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

async def insert_fill(db: Database, fill: Fill, pnl_usd: float | None = None) -> None:
    await db.execute(
        f"""
        INSERT INTO {S}.fills
            (id, order_id, exchange, symbol, side, filled_price, filled_amount,
             fee, fee_currency, filled_at, pnl_usd)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (id) DO NOTHING
        """,
        fill.id, fill.order_id, fill.exchange, fill.symbol, fill.side,
        fill.filled_price, fill.filled_amount, fill.fee, fill.fee_currency,
        fill.timestamp, pnl_usd,
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
    counter = 0
    if exchange:
        counter += 1
        conditions.append(f"exchange = ${counter}")
        params.append(exchange)
    if since_ts:
        counter += 1
        conditions.append(f"filled_at >= ${counter}")
        params.append(since_ts)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    counter += 1
    query = f"SELECT * FROM {S}.fills {where} ORDER BY filled_at DESC LIMIT ${counter}"
    params.append(limit)
    rows = await db.fetch(query, *params)
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
    await db.execute(
        f"""
        INSERT INTO {S}.inventory_snapshots
            (exchange, usd, token, global_mid, volatility, aggressiveness, skew_factor, snapshot_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        exchange, usd, token, global_mid, volatility, aggressiveness, skew_factor, now_s(),
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
    hmm_regime: str | None = None,
    hmm_confidence: float | None = None,
) -> None:
    await db.execute(
        f"""
        INSERT INTO {S}.rl_features
            (timestamp, vol_simple, vol_zz, zz_regime, hmm_regime, hmm_confidence,
             aggressiveness, global_mid,
             buy_spread_l1, sell_spread_l1, skew_factor, fill_rate_1m, pnl_1h)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """,
        now_s(), vol_simple, vol_zz, zz_regime, hmm_regime, hmm_confidence,
        aggressiveness, global_mid,
        buy_spread_l1, sell_spread_l1, skew_factor, fill_rate_1m, pnl_1h,
    )
    await db.commit_unless_batching()


async def get_recent_pnl(db: Database, window_s: float = 3600.0) -> float:
    """Sum of realized P&L over the last window_s seconds."""
    since = now_s() - window_s
    val = await db.fetchval(
        f"SELECT COALESCE(SUM(pnl_usd), 0) FROM {S}.fills WHERE filled_at >= $1 AND pnl_usd IS NOT NULL",
        since,
    )
    return float(val) if val is not None else 0.0


async def get_fill_rate(db: Database, window_s: float = 60.0) -> float:
    """Number of fills per minute over the last window_s seconds."""
    since = now_s() - window_s
    count = await db.fetchval(
        f"SELECT COUNT(*) FROM {S}.fills WHERE filled_at >= $1",
        since,
    )
    return int(count) / (window_s / 60.0) if count else 0.0
