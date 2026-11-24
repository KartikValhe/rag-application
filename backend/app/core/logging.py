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
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog

from app.core.config import settings

# Event keys named like a credential are dropped before anything is written (docs/phase6-plan.md 7.6, F17).
# A key matches when its name, with '-' read as '_' and in lower case, contains one of these.
CREDENTIAL_KEY_NAMES = (
    "authorization",
    "api_key",
    "apikey",
    "x-goog-api-key",
    "cookie",
    "password",
    "secret",
)
_CREDENTIAL_PARTS = tuple(name.lower().replace("-", "_") for name in CREDENTIAL_KEY_NAMES)
_MAX_DEPTH = 6


def _is_credential_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    name = key.lower().replace("-", "_")
    return any(part in name for part in _CREDENTIAL_PARTS)


def _scrub(value: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, depth + 1) for k, v in value.items() if not _is_credential_key(k)}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def drop_credential_keys(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: removes every key named like a credential, also inside nested dicts and lists."""
    return cast(MutableMapping[str, Any], _scrub(dict(event_dict), 0))


def quiet_http_loggers() -> None:
    """httpx logs every request line at INFO; httpcore logs connection details at DEBUG. Keep both at WARNING."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


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
        drop_credential_keys,
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
    quiet_http_loggers()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the given module name."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))

