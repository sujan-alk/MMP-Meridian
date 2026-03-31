"""
FastAPI REST routes.
All routes depend on the Orchestrator being available via app.state.orchestrator.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from api.models import (
    AllStatusResponse, ControlResponse, ConfigResponse,
    ExchangeStatus, FillResponse, HealthResponse, MetricsResponse, OrderResponse,
)
from api.websocket import LiveFeed
from config.schema import BotConfig
from config.settings import save_bot_config, get_runtime_settings
from db.queries import get_fills, get_orders, get_fill_rate, get_recent_pnl
from utils.logging import get_logger
from utils.time_utils import now_s

log = get_logger("routes")

router = APIRouter()
_start_time = time.time()


def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Dependency that enforces X-API-Key auth when API_KEY is configured."""
    configured = get_runtime_settings().api_key
    if configured and x_api_key != configured:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def get_orchestrator(request: Request):
    return request.app.state.orchestrator


def get_db(request: Request):
    return request.app.state.db


def get_live_feed(request: Request) -> LiveFeed:
    return request.app.state.live_feed


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    return HealthResponse(uptime_s=round(now_s() - _start_time, 1))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/api/status", response_model=AllStatusResponse)
async def get_status(request: Request):
    orch = get_orchestrator(request)
    state = orch.get_global_state()
    statuses = orch.get_all_statuses()
    return AllStatusResponse(
        exchanges=[ExchangeStatus(**s) for s in statuses],
        global_mid=state.global_mid if state else None,
        volatility=state.volatility if state else None,
        aggressiveness=state.aggressiveness if state else None,
        zz_vol=state.zz_vol if state else None,
        zz_regime=state.zz_regime if state else None,
        contributing_exchanges=state.contributing_exchanges if state else [],
    )


@router.get("/api/exchanges/{exchange}", response_model=ExchangeStatus)
async def get_exchange_status(request: Request, exchange: str):
    orch = get_orchestrator(request)
    bot = orch.get_bot(exchange)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Exchange '{exchange}' not found")
    return ExchangeStatus(**bot.get_status())


# ---------------------------------------------------------------------------
# Orders & Fills
# ---------------------------------------------------------------------------

@router.get("/api/orders")
async def get_all_orders(request: Request, exchange: str | None = None, limit: int = 100):
    db = get_db(request)
    orders = await get_orders(db, exchange=exchange, limit=limit)
    return {"orders": orders, "count": len(orders)}


@router.get("/api/fills")
async def get_all_fills(
    request: Request,
    exchange: str | None = None,
    limit: int = 200,
    since: float | None = None,
):
    db = get_db(request)
    fills = await get_fills(db, exchange=exchange, since_ts=since, limit=limit)
    return {"fills": fills, "count": len(fills)}


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------

@router.get("/api/balances")
async def get_balances(request: Request):
    orch = get_orchestrator(request)
    result = []
    for bot in orch._bots.values():
        inv = bot.inventory.state()
        result.append({
            "exchange": bot.exchange,
            "usd": inv.usd,
            "token": inv.token,
            "quote_currency": bot.config.quote_currency,
            "initial_usd": inv.initial_usd,
            "initial_token": inv.initial_token,
            "usd_drift_pct": round(inv.usd_drift_pct, 2),
            "token_drift_pct": round(inv.token_drift_pct, 2),
            "skew_factor": round(inv.skew_factor, 4),
        })
    return {"balances": result}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics(request: Request):
    orch = get_orchestrator(request)
    db = get_db(request)
    state = orch.get_global_state()
    fill_rate = await get_fill_rate(db)
    pnl_1h = await get_recent_pnl(db, window_s=3600.0)
    total_orders = sum(bot.order_manager.open_order_count for bot in orch._bots.values())
    return MetricsResponse(
        global_mid=state.global_mid if state else None,
        volatility=state.volatility if state else None,
        aggressiveness=state.aggressiveness if state else None,
        zz_vol=state.zz_vol if state else None,
        zz_regime=state.zz_regime if state else None,
        fill_rate_1m=round(fill_rate, 4),
        pnl_1h=round(pnl_1h, 4),
        total_open_orders=total_orders,
    )


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

@router.post("/api/control/pause", response_model=ControlResponse, dependencies=[Depends(_require_api_key)])
async def pause_all(request: Request):
    orch = get_orchestrator(request)
    for bot in orch._bots.values():
        bot.q_switch.trigger_manually("Paused via API")
    return ControlResponse(success=True, message="All bots paused")


@router.post("/api/control/resume", response_model=ControlResponse, dependencies=[Depends(_require_api_key)])
async def resume_all(request: Request):
    orch = get_orchestrator(request)
    for bot in orch._bots.values():
        bot.q_switch.reset()
        bot.circuit_breaker.reset()
    return ControlResponse(success=True, message="All bots resumed")


@router.post("/api/control/emergency_stop", response_model=ControlResponse, dependencies=[Depends(_require_api_key)])
async def emergency_stop(request: Request):
    orch = get_orchestrator(request)
    await orch.emergency_stop_all("Emergency stop via API")
    return ControlResponse(success=True, message="Emergency stop executed on all exchanges")


@router.post("/api/control/exchanges/{exchange}/pause", response_model=ControlResponse, dependencies=[Depends(_require_api_key)])
async def pause_exchange(request: Request, exchange: str):
    orch = get_orchestrator(request)
    bot = orch.get_bot(exchange)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Exchange '{exchange}' not found")
    bot.q_switch.trigger_manually("Paused via API")
    return ControlResponse(success=True, message=f"{exchange} paused")


@router.post("/api/control/exchanges/{exchange}/resume", response_model=ControlResponse, dependencies=[Depends(_require_api_key)])
async def resume_exchange(request: Request, exchange: str):
    orch = get_orchestrator(request)
    bot = orch.get_bot(exchange)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Exchange '{exchange}' not found")
    bot.q_switch.reset()
    bot.circuit_breaker.reset()
    return ControlResponse(success=True, message=f"{exchange} resumed")


# ---------------------------------------------------------------------------
# Config hot-reload
# ---------------------------------------------------------------------------

@router.get("/api/config", response_model=ConfigResponse)
async def get_config(request: Request):
    orch = get_orchestrator(request)
    return ConfigResponse(config=orch.config.model_dump())


@router.put("/api/config", response_model=ConfigResponse, dependencies=[Depends(_require_api_key)])
async def update_config(request: Request):
    """
    Update the meta config (spread, depth, volatility params).
    Hot-reloads without restarting the bot.
    NOTE: Credential changes and dry_run changes require a full restart.
    """
    orch = get_orchestrator(request)
    body = await request.json()
    try:
        new_config = BotConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Persist to bot.json
    save_bot_config(new_config)

    # Apply to running orchestrator
    orch.trigger_config_reload(new_config)

    live_feed = get_live_feed(request)
    await live_feed.emit_config_reloaded()

    log.info("config_updated_via_api")
    return ConfigResponse(config=new_config.model_dump(), message="Config updated and hot-reloaded")


@router.put("/api/config/exchanges/{exchange}", response_model=ConfigResponse, dependencies=[Depends(_require_api_key)])
async def update_exchange_config(request: Request, exchange: str):
    """Update a single exchange's spread/depth/safety config."""
    orch = get_orchestrator(request)
    body = await request.json()

    current = orch.config.model_dump()
    # Find and update the matching exchange config
    for i, ex in enumerate(current.get("exchanges", [])):
        if ex.get("exchange") == exchange:
            current["exchanges"][i].update(body)
            break
    else:
        raise HTTPException(status_code=404, detail=f"Exchange '{exchange}' not in config")

    try:
        new_config = BotConfig.model_validate(current)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    save_bot_config(new_config)
    orch.trigger_config_reload(new_config)

    live_feed = get_live_feed(request)
    await live_feed.emit_config_reloaded()

    return ConfigResponse(config=new_config.model_dump(), message=f"{exchange} config updated")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint. Clients connect here to receive live bot events.
    Events are JSON-encoded strings with {"event": ..., "data": ..., "timestamp": ...}.
    """
    live_feed: LiveFeed = websocket.app.state.live_feed
    await websocket.accept()
    q = await live_feed.subscribe()
    log.debug("ws_client_connected", clients=live_feed.client_count)
    try:
        while True:
            # Wait for events from the live feed
            message = await asyncio.wait_for(q.get(), timeout=30.0)
            await websocket.send_text(message)
    except asyncio.TimeoutError:
        # Send a heartbeat ping to keep the connection alive
        import json as _json
        from utils.time_utils import now_s as _now
        await websocket.send_text(_json.dumps({"event": "heartbeat", "timestamp": _now()}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws_client_error", error=str(exc))
    finally:
        await live_feed.unsubscribe(q)
        log.debug("ws_client_disconnected", clients=live_feed.client_count)
