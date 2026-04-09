"""
ALKIMI Market Making Bot — entry point.

Starts:
1. FastAPI + uvicorn server (immediately, for health checks)
2. PostgreSQL database (Supabase) — with retries
3. LiveFeed (WebSocket broadcaster)
4. Orchestrator (4 exchange bots + global price loop)

All run concurrently via asyncio.gather().
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import uvicorn

from api.app import create_app
from api.websocket import LiveFeed
from config.settings import load_bot_config, get_runtime_settings
from core.orchestrator import Orchestrator
from db.database import Database
from utils.logging import configure_logging, get_logger

log = get_logger("main")

DB_CONNECT_RETRIES = 10
DB_CONNECT_DELAY = 5  # seconds between retries


async def connect_db_with_retries(db: Database) -> bool:
    """Attempt to connect to the database with retries."""
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            await db.connect()
            return True
        except Exception as exc:
            log.warning(
                "db_connect_retry",
                attempt=attempt,
                max_retries=DB_CONNECT_RETRIES,
                error=str(exc),
            )
            if attempt < DB_CONNECT_RETRIES:
                await asyncio.sleep(DB_CONNECT_DELAY)
    log.error("db_connect_failed", msg="All retries exhausted")
    return False


async def main() -> None:
    settings = get_runtime_settings()
    configure_logging(settings.log_level)

    log.info("alkimi_mm_bot_starting", live_mode=settings.live_mode)

    if settings.live_mode:
        gate_file = Path(".enable_live_mode")
        if not gate_file.exists():
            log.error(
                "LIVE_MODE_BLOCKED",
                msg=(
                    "LIVE_MODE=true but the gate file '.enable_live_mode' does not exist. "
                    "Create it in the working directory to confirm you intend to place real orders."
                ),
            )
            sys.exit(1)
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

    # Initialise shared services (DB connects later with retries)
    db = Database(settings.supabase_db_url)
    live_feed = LiveFeed()

    # Build FastAPI app — starts immediately so health check passes
    # Orchestrator is created after DB connects
    app = create_app(None, db, live_feed)

    # Configure uvicorn
    uvi_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
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

    async def run_bot():
        """Connect DB, then start the orchestrator."""
        try:
            # Connect to DB with retries
            connected = await connect_db_with_retries(db)
            if not connected:
                log.error("bot_startup_failed", msg="Could not connect to database")
                shutdown_event.set()
                return

            # Now create and start orchestrator
            orchestrator = Orchestrator(
                config=config,
                db=db,
                live_feed=live_feed,
                live_mode=settings.live_mode,
            )

            # Update the app with the orchestrator reference
            app.state.orchestrator = orchestrator

            await orchestrator.start()
        except Exception as exc:
            log.error("orchestrator_crashed", error=str(exc), exc_info=True)
        finally:
            shutdown_event.set()

    async def wait_for_shutdown():
        await shutdown_event.wait()
        log.info("shutdown_initiated")
        orchestrator = getattr(app.state, "orchestrator", None)
        if orchestrator:
            await orchestrator.stop()
        server.should_exit = True
        await db.disconnect()
        log.info("shutdown_complete")

    try:
        await asyncio.gather(
            run_server(),
            run_bot(),
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
