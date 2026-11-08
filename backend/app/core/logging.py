"""
app/core/logging.py
───────────────────
Structured JSON logging using structlog.

Every log event automatically carries:
  - timestamp (ISO-8601)
  - log level
  - request_id  (set by TenantResolutionMiddleware from X-Request-ID header or generated)
  - tenant_id   (set after JWT / host resolution)
  - environment

Usage anywhere in the app:
    from app.core.logging import get_logger
    logger = get_logger(__name__)
    logger.info("user_created", user_id=str(user.id))
"""

import logging
import sys
from typing import cast

import structlog

from app.core.config import settings


def configure_logging() -> None:
    """
    Call once at application startup (in app/main.py).
    Sets up structlog with JSON output in production and
    human-readable console output in development.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Route stdlib loggers (uvicorn, sqlalchemy, etc.) through structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_development:
        # Pretty console output for local dev.
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        # Compact JSON for log aggregators (Loki, CloudWatch, etc.).
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the given module name."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))

