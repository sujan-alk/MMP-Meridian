"""
ALKIMI Market Making Bot — entry point.

Starts:
1. SQLite database
2. LiveFeed (WebSocket broadcaster)
3. Orchestrator (4 exchange bots + global price loop)
4. FastAPI + uvicorn server

All four run concurrently via asyncio.gather().
"""

from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn

from api.app import create_app
from api.websocket import LiveFeed
from config.settings import load_bot_config, get_runtime_settings
from core.orchestrator import Orchestrator
from db.database import Database
from utils.logging import configure_logging, get_logger

log = get_logger("main")


async def main() -> None:
    settings = get_runtime_settings()
    configure_logging(settings.log_level)

    log.info("alkimi_mm_bot_starting", live_mode=settings.live_mode)

    if settings.live_mode:
        log.warning(
            "LIVE_MODE_ENABLED",
            msg="Bot will place REAL orders. Ensure credentials and config are correct.",
        )
    else:
        log.info("dry_run_mode", msg="No real orders will be placed. Set LIVE_MODE=true to go live.")

    # Load config
    config = load_bot_config(settings.bot_config_path)
    log.info(
        "config_loaded",
        dry_run=config.dry_run,
        exchanges=[ex.exchange for ex in config.enabled_exchanges()],
    )

    # Initialise shared services
    db = Database(settings.db_path)
    await db.connect()

    live_feed = LiveFeed()

    orchestrator = Orchestrator(
        config=config,
        db=db,
        live_feed=live_feed,
        live_mode=settings.live_mode,
    )

    # Build FastAPI app
    app = create_app(orchestrator, db, live_feed)

    # Configure uvicorn
    uvi_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,  # structlog handles access logging
    )
    server = uvicorn.Server(uvi_config)

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()

    def handle_signal(*_):
        log.info("shutdown_signal_received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    async def run_server():
        await server.serve()

    async def run_orchestrator():
        try:
            await orchestrator.start()
        except Exception as exc:
            log.error("orchestrator_crashed", error=str(exc), exc_info=True)
        finally:
            shutdown_event.set()

    async def wait_for_shutdown():
        await shutdown_event.wait()
        log.info("shutdown_initiated")
        await orchestrator.stop()
        server.should_exit = True
        await db.disconnect()
        log.info("shutdown_complete")

    try:
        await asyncio.gather(
            run_server(),
            run_orchestrator(),
            wait_for_shutdown(),
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        pass
    finally:
        log.info("alkimi_mm_bot_exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
