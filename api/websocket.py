"""
WebSocket live feed — broadcasts real-time bot events to connected clients.
Adapted from Cash-Town-Trading-Bot/api/websocket.py patterns.

Supports:
- Multiple concurrent WebSocket clients
- Typed event system (EventType enum)
- Async broadcast without blocking the bot tick loop
"""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Any

from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("live_feed")


class EventType(str, Enum):
    TICK_UPDATE = "tick_update"
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELED = "order_canceled"
    INVENTORY_UPDATE = "inventory_update"
    AGGRESSIVENESS_CHANGE = "aggressiveness_change"
    EMERGENCY_STOP = "emergency_stop"
    HEARTBEAT = "heartbeat"
    CONFIG_RELOADED = "config_reloaded"
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"


class LiveFeed:
    """
    Manages connected WebSocket clients and broadcasts events.
    All emit_* methods are async but fire-and-forget to avoid blocking callers.
    """

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    async def subscribe(self) -> asyncio.Queue:
        """Register a new client and return its message queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._clients.add(q)
        log.debug("ws_client_subscribed", total=len(self._clients))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a client queue."""
        async with self._lock:
            self._clients.discard(q)
        log.debug("ws_client_unsubscribed", total=len(self._clients))

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(self, event_type: EventType, data: Any) -> None:
        """Send an event to all connected clients. Silently drops slow clients."""
        if not self._clients:
            return
        message = json.dumps({
            "event": event_type.value,
            "data": data,
            "timestamp": now_s(),
        })
        dead: list[asyncio.Queue] = []
        async with self._lock:
            for q in self._clients:
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)

    async def emit(self, event_name: str, data: Any) -> None:
        """Generic emit: accepts a string event name and broadcasts to all clients."""
        try:
            event_type = EventType(event_name)
        except ValueError:
            # Unknown event type — broadcast raw
            event_type = None
        if event_type:
            await self.broadcast(event_type, data)
        else:
            # Fallback: broadcast with raw string
            if not self._clients:
                return
            import json as _json
            message = _json.dumps({"event": event_name, "data": data, "timestamp": now_s()})
            async with self._lock:
                for q in self._clients:
                    try:
                        q.put_nowait(message)
                    except asyncio.QueueFull:
                        pass

    # ------------------------------------------------------------------
    # Typed emit helpers
    # ------------------------------------------------------------------

    async def emit_tick(
        self,
        exchange: str,
        global_mid: float,
        volatility: float,
        aggressiveness: float,
        skew_factor: float,
        open_orders: int,
        placed_count: int,
        regime: str | None = None,
        hmm_regime: str | None = None,
        hmm_regime_confidence: float | None = None,
        buy_prices: list[float] | None = None,
        sell_prices: list[float] | None = None,
        buy_amounts: list[float] | None = None,
        sell_amounts: list[float] | None = None,
    ) -> None:
        payload: dict = {
            "exchange": exchange,
            "global_mid": global_mid,
            "volatility": round(volatility, 6),
            "aggressiveness": round(aggressiveness, 4),
            "skew_factor": round(skew_factor, 4),
            "open_orders": open_orders,
            "placed_count": placed_count,
        }
        if regime is not None:
            payload["regime"] = regime
        if hmm_regime is not None:
            payload["hmm_regime"] = hmm_regime
            payload["hmm_regime_confidence"] = round(hmm_regime_confidence or 0.0, 4)
        if buy_prices is not None and buy_amounts is not None:
            payload["intended_buy_orders"] = [
                {"price": round(p, 6), "usd": round(a, 2)}
                for p, a in zip(buy_prices, buy_amounts)
            ]
        if sell_prices is not None and sell_amounts is not None:
            payload["intended_sell_orders"] = [
                {"price": round(p, 6), "usd": round(a, 2)}
                for p, a in zip(sell_prices, sell_amounts)
            ]
        await self.broadcast(EventType.TICK_UPDATE, payload)

    async def emit_emergency_stop(self, exchange: str, reason: str) -> None:
        await self.broadcast(EventType.EMERGENCY_STOP, {
            "exchange": exchange,
            "reason": reason,
        })

    async def emit_config_reloaded(self) -> None:
        await self.broadcast(EventType.CONFIG_RELOADED, {"msg": "Bot config updated"})

    async def emit_bot_started(self, exchange: str) -> None:
        await self.broadcast(EventType.BOT_STARTED, {"exchange": exchange})

    async def emit_bot_stopped(self, exchange: str) -> None:
        await self.broadcast(EventType.BOT_STOPPED, {"exchange": exchange})

    @property
    def client_count(self) -> int:
        return len(self._clients)
