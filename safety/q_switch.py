"""
Q-Switch — Emergency stop mechanism.

Triggered when:
1. Balance falls below the configured safety threshold.
2. Manually triggered via the API (POST /api/control/emergency_stop).

On trigger:
- Sets an asyncio.Event so all waiting coroutines are notified
- Cancels all open orders on the affected exchange
- Optionally sends a webhook alert (Discord / Slack)
- Requires manual reset via API to resume

Pattern adapted from cex-mm-bot TypeScript implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from exchange.base import Balance
from config.schema import SafetyConfig
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("q_switch")


@dataclass
class QSwitchEvent:
    exchange: str
    reason: str
    triggered_at: float
    balance_usd: float
    balance_token: float


class QSwitch:
    """
    Per-exchange emergency stop.
    Once triggered, the bot stops placing orders until manually reset.
    """

    def __init__(self, config: SafetyConfig, exchange: str):
        self.config = config
        self.exchange = exchange
        self._triggered = False
        self._trigger_event = asyncio.Event()
        self._last_event: QSwitchEvent | None = None

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    @property
    def triggered_event(self) -> asyncio.Event:
        """asyncio.Event that is set when the Q-Switch fires."""
        return self._trigger_event

    def check(self, balance: Balance) -> bool:
        """
        Check if the Q-Switch should fire based on current balance.
        Returns True if the bot should halt (already triggered or just triggered).
        """
        if self._triggered:
            return True

        reason = None
        if balance.usd < self.config.min_balance_usd:
            reason = f"USD balance {balance.usd:.2f} < threshold {self.config.min_balance_usd}"
        elif balance.token < self.config.min_balance_token:
            reason = f"Token balance {balance.token:.4f} < threshold {self.config.min_balance_token}"

        if reason:
            self._fire(reason, balance)
            return True

        return False

    def trigger_manually(self, reason: str = "Manual trigger via API") -> None:
        """Force-trigger the Q-Switch without a balance check."""
        if not self._triggered:
            self._fire(reason, None)

    def reset(self) -> None:
        """
        Clear the Q-Switch. Only available via the API.
        Bot will resume normal operation on the next tick.
        """
        if self._triggered:
            log.info("q_switch_reset", exchange=self.exchange)
            self._triggered = False
            self._trigger_event.clear()
            self._last_event = None

    def _fire(self, reason: str, balance: Balance | None) -> None:
        self._triggered = True
        self._trigger_event.set()
        event = QSwitchEvent(
            exchange=self.exchange,
            reason=reason,
            triggered_at=now_s(),
            balance_usd=balance.usd if balance else 0.0,
            balance_token=balance.token if balance else 0.0,
        )
        self._last_event = event
        log.error(
            "q_switch_triggered",
            exchange=self.exchange,
            reason=reason,
            balance_usd=event.balance_usd,
            balance_token=event.balance_token,
        )
        # Fire-and-forget the webhook alert
        if self.config.alert_webhook_url:
            asyncio.create_task(self._send_alert(event))

    async def _send_alert(self, event: QSwitchEvent) -> None:
        """Send a Slack/Discord webhook notification."""
        payload = {
            "text": (
                f":rotating_light: *Q-Switch Triggered* — `{event.exchange}`\n"
                f"Reason: {event.reason}\n"
                f"Balance: {event.balance_usd:.2f} USD / {event.balance_token:.4f} ALKIMI"
            )
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(self.config.alert_webhook_url, json=payload)  # type: ignore[arg-type]
        except Exception as exc:
            log.warning("q_switch_alert_failed", error=str(exc))

    @property
    def last_event(self) -> QSwitchEvent | None:
        return self._last_event
