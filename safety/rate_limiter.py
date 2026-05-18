"""
Async token-bucket rate limiter.
Prevents hitting exchange API rate limits by throttling outbound requests.
CCXT has its own rate limiter; this is an additional application-level guard.
"""

from __future__ import annotations

import asyncio
import time

from utils.logging import get_logger

log = get_logger("rate_limiter")


class RateLimiter:
    """
    Async token-bucket rate limiter.
    Allows up to max_per_second requests per second, with burst tolerance.

    Usage:
        await rate_limiter.acquire()   # blocks if over limit
    """

    def __init__(self, max_per_second: int = 8, exchange: str = ""):
        self._max = max_per_second
        self._tokens = float(max_per_second)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._exchange = exchange

    async def acquire(self) -> None:
        """Acquire one token, waiting if the bucket is empty."""
        async with self._lock:
            await self._refill()
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._max
                log.debug("rate_limiter_wait", exchange=self._exchange, wait_s=round(wait, 3))
                await asyncio.sleep(wait)
                await self._refill()
            self._tokens -= 1.0

    async def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self._max), self._tokens + elapsed * self._max)
        self._last_refill = now
