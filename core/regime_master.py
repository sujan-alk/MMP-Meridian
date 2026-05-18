"""
Regime Master — top-level intelligence for Meridian MM Platform.

The Regime Master is a single unified regime source for all exchange bots.
It:
1. Subscribes to the price/volatility stream from the Orchestrator
2. Runs the HMM regime detector (RANGING / TRENDING / HIGH_VOL / THIN_BOOK)
3. Cross-checks with the existing Zhang-Zhang classifier
4. Reconciles HMM + ZZ into a unified RegimeState
5. Broadcasts RegimeState to all ExchangeBots via asyncio.Queue

When HMM and ZZ strongly disagree, conservative (wider spread) parameters are used.

This implements Option B: Hierarchical Architecture — one regime master,
many specialist exchange bots that all see the same coherent regime view.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from quant.regime_detector import RegimeDetector, MM_PARAMETERS
from utils.logging import get_logger

if TYPE_CHECKING:
    from core.orchestrator import GlobalState

log = get_logger("regime_master")

# How often the regime master pushes state (seconds)
REGIME_UPDATE_INTERVAL_S = 1.0

# Conservative params used when HMM and ZZ strongly disagree
_CONSERVATIVE_PARAMS = {
    'spread_mult': 2.0,
    'depth_mult': 0.5,
    'aggressiveness': 0.3,
    'inventory_skew': 0.0,
}


@dataclass
class RegimeState:
    """
    Unified regime state broadcast to all ExchangeBots.

    Fields
    ------
    regime      : primary regime from HMM — 'RANGING' | 'TRENDING' | 'HIGH_VOL' | 'THIN_BOOK'
    confidence  : HMM posterior probability of the winning state (0.0 – 1.0)
    zz_regime   : Zhang-Zhang regime string — 'trending_up' | 'trending_down' | 'choppy'
    agreement   : True if HMM and ZZ are broadly aligned
    mm_params   : dict of spread_mult, depth_mult, aggressiveness, inventory_skew
    timestamp   : Unix epoch seconds
    """
    regime: str
    confidence: float
    zz_regime: str
    agreement: bool
    mm_params: dict
    timestamp: float = field(default_factory=time.time)


# Default regime state used before first update
_DEFAULT_REGIME_STATE = RegimeState(
    regime="RANGING",
    confidence=0.5,
    zz_regime="choppy",
    agreement=True,
    mm_params=MM_PARAMETERS["RANGING"].copy(),
    timestamp=0.0,
)


def _zz_to_hmm_regime(zz_regime: str) -> str:
    """
    Map a Zhang-Zhang regime string to the closest HMM regime name.

    ZZ produces: 'trending_up', 'trending_down', 'choppy'
    HMM produces: 'RANGING', 'TRENDING', 'HIGH_VOL', 'THIN_BOOK'
    """
    zz = (zz_regime or "").lower()
    if "trending" in zz:
        return "TRENDING"
    # 'choppy' or 'ranging' → RANGING
    return "RANGING"


def _regimes_agree(hmm_regime: str, zz_regime: str) -> bool:
    """
    Return True if HMM and ZZ are broadly consistent.

    Agreement rules:
    - HMM=RANGING + ZZ=choppy → agree
    - HMM=TRENDING + ZZ=trending_* → agree
    - HMM=HIGH_VOL → ZZ can say anything (no vol state in ZZ)
    - HMM=THIN_BOOK → ZZ can say anything (no book-depth state in ZZ)
    - Otherwise → disagree
    """
    mapped = _zz_to_hmm_regime(zz_regime)
    if hmm_regime in ("HIGH_VOL", "THIN_BOOK"):
        # ZZ doesn't model these — treat as agreement so we don't incorrectly widen
        return True
    return hmm_regime == mapped


class RegimeMaster:
    """
    Top-level regime intelligence.  One instance lives in the Orchestrator.

    Usage
    -----
    >>> master = RegimeMaster()
    >>> master.start_task(asyncio.get_event_loop())
    >>> # push updates from orchestrator price loop:
    >>> master.push_state(global_state)
    >>> # bots read:
    >>> regime = master.current_regime_state
    """

    def __init__(self, subscriber_queues: Optional[list[asyncio.Queue]] = None):
        self._detector = RegimeDetector()
        self._subscriber_queues: list[asyncio.Queue] = subscriber_queues or []
        self._current: RegimeState = _DEFAULT_REGIME_STATE
        self._current_lock = asyncio.Lock()

        # Pending GlobalState pushed by orchestrator price loop
        self._pending_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._task: asyncio.Task | None = None
        self._running = False

        # Track previous mid price for price_change_rate calculation
        self._prev_mid: float | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start_task(self) -> asyncio.Task:
        """Create and return the asyncio task that runs the regime loop."""
        self._running = True
        self._task = asyncio.create_task(self._regime_loop(), name="regime_master")
        log.info("regime_master_started")
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("regime_master_stopped")

    def add_subscriber(self, q: asyncio.Queue) -> None:
        """Register an ExchangeBot queue to receive RegimeState updates."""
        self._subscriber_queues.append(q)

    def remove_subscriber(self, q: asyncio.Queue) -> None:
        self._subscriber_queues = [x for x in self._subscriber_queues if x is not q]

    # ── external push ──────────────────────────────────────────────────────

    def push_state(self, state: "GlobalState") -> None:
        """
        Called by the Orchestrator price loop each tick.
        Non-blocking: drops the update if the internal queue is full.
        """
        try:
            self._pending_queue.put_nowait(state)
        except asyncio.QueueFull:
            log.debug("regime_master_queue_full")

    # ── regime state accessor ─────────────────────────────────────────────

    @property
    def current_regime_state(self) -> RegimeState:
        return self._current

    # ── internal loop ─────────────────────────────────────────────────────

    async def _regime_loop(self) -> None:
        """
        Drain GlobalState updates from the pending queue,
        update the HMM, reconcile with ZZ, and broadcast.
        """
        while self._running:
            try:
                state: "GlobalState" = await asyncio.wait_for(
                    self._pending_queue.get(), timeout=5.0
                )
                await self._process_state(state)
            except asyncio.TimeoutError:
                log.debug("regime_master_no_state")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("regime_master_error", error=str(exc), exc_info=True)

    async def _process_state(self, state: "GlobalState") -> None:
        """Update HMM with latest market data and reconcile with ZZ."""
        # Compute price change rate
        price_change_rate = 0.0
        if self._prev_mid is not None and self._prev_mid > 0:
            price_change_rate = (state.global_mid - self._prev_mid) / self._prev_mid
        self._prev_mid = state.global_mid

        # Build HMM features from GlobalState
        # bid_ask_spread: use vol as proxy (not directly available in GlobalState)
        # ob_imbalance: default to balanced (0.0) unless available
        self._detector.update_from_floats(
            price_vol=state.volatility,
            bid_ask_spread_pct=state.zz_vol * 100.0,  # zz_vol as spread proxy
            ob_imbalance=0.0,          # balanced default; extend with ob data later
            funding_rate=0.0,          # no funding in GlobalState yet; extend later
            price_change_rate=price_change_rate,
        )

        hmm_regime = self._detector.get_regime()
        hmm_confidence = self._detector.get_regime_confidence()
        zz_regime = state.zz_regime or "choppy"

        # Reconcile HMM + ZZ
        agreement = _regimes_agree(hmm_regime, zz_regime)

        if agreement or hmm_regime in ("HIGH_VOL", "THIN_BOOK"):
            mm_params = self._detector.get_mm_parameters(hmm_regime)
        else:
            # HMM and ZZ disagree — use conservative params
            log.warning(
                "regime_disagreement",
                hmm=hmm_regime,
                zz=zz_regime,
                confidence=round(hmm_confidence, 3),
            )
            mm_params = _CONSERVATIVE_PARAMS.copy()

        new_state = RegimeState(
            regime=hmm_regime,
            confidence=hmm_confidence,
            zz_regime=zz_regime,
            agreement=agreement,
            mm_params=mm_params,
            timestamp=state.timestamp,
        )

        async with self._current_lock:
            self._current = new_state

        await self._broadcast(new_state)

        log.debug(
            "regime_updated",
            regime=hmm_regime,
            confidence=round(hmm_confidence, 3),
            zz_regime=zz_regime,
            agreement=agreement,
        )

    async def _broadcast(self, regime_state: RegimeState) -> None:
        """Push RegimeState to all registered subscriber queues."""
        for q in self._subscriber_queues:
            try:
                q.put_nowait(regime_state)
            except asyncio.QueueFull:
                log.debug("regime_subscriber_queue_full")

    def __repr__(self) -> str:
        rs = self._current
        return (f"RegimeMaster(regime={rs.regime!r}, "
                f"confidence={rs.confidence:.1%}, "
                f"zz={rs.zz_regime!r}, "
                f"agreement={rs.agreement})")
