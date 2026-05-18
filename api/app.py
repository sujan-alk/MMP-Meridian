"""
FastAPI application factory.
State is attached to app.state so all routes can access the Orchestrator, DB, and LiveFeed.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes import router
from utils.logging import get_logger

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator
    from db.database import Database
    from api.websocket import LiveFeed

log = get_logger("app")


def create_app(
    orchestrator: "Orchestrator | None",
    db: "Database",
    live_feed: "LiveFeed",
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        orchestrator: The running Orchestrator instance
        db: Shared Database connection
        live_feed: The LiveFeed WebSocket broadcaster
    """
    app = FastAPI(
        title="ALKIMI Market Making Bot",
        description="REST + WebSocket API for the ALKIMI MM Bot",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow all origins in dev; restrict in production via env
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Attach shared state
    app.state.orchestrator = orchestrator
    app.state.db = db
    app.state.live_feed = live_feed

    # API auth middleware
    MM_API_KEY = os.getenv("MM_API_KEY", "")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if MM_API_KEY and request.url.path.startswith("/api/control"):
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            if token != MM_API_KEY:
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    # Register routes
    app.include_router(router)

    # Serve web UI dashboard from static/ directory
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    @app.on_event("startup")
    async def on_startup():
        log.info("api_server_started")

    @app.on_event("shutdown")
    async def on_shutdown():
        log.info("api_server_stopping")

    return app
