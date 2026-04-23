"""
Async PostgreSQL database wrapper using asyncpg.
Connects to Supabase (or any Postgres instance) via a connection pool.
All writes are async to avoid blocking the asyncio event loop.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import asyncpg

from db.migrations import SCHEMA_NAME, SCHEMA_STATEMENTS
from utils.logging import get_logger

log = get_logger("database")


class Database:
    """
    Async Postgres database for persisting orders, fills, inventory snapshots, and RL features.
    One shared instance across all exchange bots.
    Uses asyncpg connection pool for concurrent access.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._batching: bool = False
        self._batch_conn: Optional[asyncpg.Connection] = None
        self._batch_txn: Optional[asyncpg.connection.transaction.Transaction] = None

    async def connect(self) -> None:
        """Open the connection pool and run schema migrations."""

        async def _init_connection(conn: asyncpg.Connection) -> None:
            """Set search_path on every new connection so queries resolve to mm_bot schema."""
            await conn.execute(f"SET search_path TO {SCHEMA_NAME}, public")

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=2,
            max_size=10,
            init=_init_connection,
            statement_cache_size=0,
        )
        # Run schema statements individually (table names are schema-qualified in migrations)
        for stmt in SCHEMA_STATEMENTS:
            await self._pool.execute(stmt)
        # Mask password in log output
        safe_dsn = self._dsn.split("@")[-1] if "@" in self._dsn else self._dsn
        log.info("database_connected", host=safe_dsn)

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self._batch_conn is not None:
            await self.end_batch()
        if self._pool:
            await self._pool.close()
            self._pool = None
            log.info("database_disconnected")

    async def cleanup(self, order_days: int = 7, fill_days: int = 30, feature_days: int = 90) -> None:
        """Delete old rows to keep the database lean."""
        now = time.time()
        await self.execute(
            f"DELETE FROM {SCHEMA_NAME}.orders WHERE placed_at < $1", now - order_days * 86400
        )
        await self.execute(
            f"DELETE FROM {SCHEMA_NAME}.fills WHERE filled_at < $1", now - fill_days * 86400
        )
        await self.execute(
            f"DELETE FROM {SCHEMA_NAME}.rl_features WHERE timestamp < $1", now - feature_days * 86400
        )
        log.info("db_cleanup_done", order_days=order_days, fill_days=fill_days, feature_days=feature_days)

    # ------------------------------------------------------------------
    # Pool access
    # ------------------------------------------------------------------

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected. Call await db.connect() first.")
        return self._pool

    def _executor(self) -> asyncpg.Pool | asyncpg.Connection:
        """Return the batch connection if batching, otherwise the pool."""
        if self._batching and self._batch_conn is not None:
            return self._batch_conn
        return self.pool

    # ------------------------------------------------------------------
    # Convenience query methods (used by db/queries.py)
    # ------------------------------------------------------------------

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a query (INSERT/UPDATE/DELETE). Returns the command status string."""
        return await self._executor().execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Execute a query and return all rows."""
        return await self._executor().fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Execute a query and return the first row (or None)."""
        return await self._executor().fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Execute a query and return the first column of the first row."""
        return await self._executor().fetchval(query, *args)

    # ------------------------------------------------------------------
    # Batch mode (transaction-based)
    # ------------------------------------------------------------------

    @property
    def is_batching(self) -> bool:
        """True when batch mode is active (writes are wrapped in a transaction)."""
        return self._batching

    async def begin_batch(self) -> None:
        """Enter batch mode: acquire a connection and start a transaction."""
        self._batch_conn = await self.pool.acquire()
        self._batch_txn = self._batch_conn.transaction()
        await self._batch_txn.start()
        self._batching = True
        log.debug("batch_mode_started")

    async def end_batch(self) -> None:
        """Exit batch mode: commit the transaction and release the connection."""
        self._batching = False
        if self._batch_txn is not None:
            await self._batch_txn.commit()
            self._batch_txn = None
        if self._batch_conn is not None:
            await self.pool.release(self._batch_conn)
            self._batch_conn = None
        log.debug("batch_mode_ended")

    async def commit_unless_batching(self) -> None:
        """No-op for Postgres compatibility. Individual statements auto-commit outside transactions."""
        pass
