"""
Heartbeat monitor — detects if the bot's main tick loop has stalled.

The exchange bot calls heartbeat.beat() on every successful tick.
A background task checks that beats arrive within the expected interval.
If max_missed_heartbeats consecutive beats are missed, it fires the Q-Switch.

Adapted from cex-mm-bot TypeScript Heartbeat class.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from config.schema import SafetyConfig
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("heartbeat")


class Heartbeat:
    """
    Monitors the health of the bot's tick loop.
    If the loop stalls, triggers an emergency stop callback.
    """

    def __init__(self, config: SafetyConfig, exchange: str):
        self.config = config
        self.exchange = exchange
        self._last_beat: float = now_s()
        self._missed: int = 0
        self._running = False
        self._task: asyncio.Task | None = None

    def beat(self) -> None:
        """Called by the exchange bot on each successful tick. Resets the missed counter."""
        self._last_beat = now_s()
        self._missed = 0

    async def start(self, on_failure: Callable[[], Awaitable[None]]) -> None:
        """
        Start the background heartbeat monitor task.

        Args:
            on_failure: async callback invoked when the heartbeat fails.
                        Typically calls q_switch.trigger_manually() and cancels orders.
        """
        self._running = True
        self._task = asyncio.create_task(self._monitor(on_failure))
        log.debug("heartbeat_started", exchange=self.exchange)

    async def stop(self) -> None:
        """Stop the heartbeat monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor(self, on_failure: Callable[[], Awaitable[None]]) -> None:
        """Background loop: checks that beats arrive within the interval."""
        while self._running:
            await asyncio.sleep(self.config.heartbeat_interval_s)
            elapsed = now_s() - self._last_beat
            if elapsed > self.config.heartbeat_interval_s * 1.5:
                self._missed += 1
                log.warning(
                    "heartbeat_missed",
                    exchange=self.exchange,
                    missed=self._missed,
                    elapsed_s=round(elapsed, 1),
                )
                if self._missed >= self.config.max_missed_heartbeats:
                    log.error(
                        "heartbeat_failure",
                        exchange=self.exchange,
                        missed=self._missed,
                    )
                    await on_failure()
                    return
            else:
                self._missed = 0
