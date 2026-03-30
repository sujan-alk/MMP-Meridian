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

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call await db.connect() first.")
        return self._conn
