"""
Structured JSON logging via structlog.
Call configure_logging() once at startup, then use get_logger() everywhere.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog with JSON output for production and coloured output for dev."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if sys.stderr.isatty():
        # Local dev: pretty coloured output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(),
        ]
    else:
        # Production (Railway): JSON
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging so that libraries (uvicorn, ccxt) use the same level
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Return a structlog logger, optionally bound with a name context."""
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(logger=name)
    return logger
