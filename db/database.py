"""
Async SQLite database wrapper using aiosqlite.
All writes are async to avoid blocking the asyncio event loop.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import aiosqlite

from db.migrations import SCHEMA
from utils.logging import get_logger

log = get_logger("database")


class Database:
    """
    Async SQLite database for persisting orders, fills, inventory snapshots, and RL features.
    One shared instance across all exchange bots.
    """

    def __init__(self, db_path: str = "data/mm_bot.db"):
        self._path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._batching: bool = False

    async def connect(self) -> None:
        """Open the database connection and run schema migrations."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        # WAL mode for better concurrent read/write performance
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("database_connected", path=str(self._path.resolve()))

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            log.info("database_disconnected")

    async def cleanup(self, order_days: int = 7, fill_days: int = 30, feature_days: int = 90) -> None:
        """Delete old rows to keep the database lean."""
        import time
        now = time.time()
        await self.conn.execute(
            "DELETE FROM orders WHERE placed_at < ?", (now - order_days * 86400,)
        )
        await self.conn.execute(
            "DELETE FROM fills WHERE filled_at < ?", (now - fill_days * 86400,)
        )
        await self.conn.execute(
            "DELETE FROM rl_features WHERE timestamp < ?", (now - feature_days * 86400,)
        )
        await self.conn.commit()
        log.info("db_cleanup_done", order_days=order_days, fill_days=fill_days, feature_days=feature_days)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call await db.connect() first.")
        return self._conn

    @property
    def is_batching(self) -> bool:
        """True when batch mode is active (commits are deferred)."""
        return self._batching

    def begin_batch(self) -> None:
        """Enter batch mode: subsequent write operations skip individual commits."""
        self._batching = True
        log.debug("batch_mode_started")

    async def end_batch(self) -> None:
        """Exit batch mode and commit all deferred writes."""
        self._batching = False
        if self._conn:
            await self._conn.commit()
        log.debug("batch_mode_ended")

    async def commit_unless_batching(self) -> None:
        """Commit if not in batch mode. Used by query functions."""
        if not self._batching and self._conn:
            await self._conn.commit()
