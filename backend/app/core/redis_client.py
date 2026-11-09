"""
app/core/redis_client.py
─────────────────────────
Shared async Redis client.

We use a single connection pool across the application.  Redis is used for:
  - Login throttling (Phase 1)
  - Session / token caching (Phase 8)
  - Rate limits and job queue (Phase 8)
"""


import redis.asyncio as aioredis

from app.core.config import settings

_redis_pool: aioredis.Redis | None = None  # type: ignore[type-arg]


async def get_redis() -> aioredis.Redis:  # type: ignore[type-arg]
    """
    Return a shared async Redis client.  Creates the connection pool on first
    call; subsequent calls return the same pool.
    """
    global _redis_pool
    if _redis_pool is None:
        kwargs: dict[str, object] = {
            "encoding": "utf-8",
            "decode_responses": True,
            "max_connections": 20,
        }
        if settings.REDIS_PASSWORD:
            kwargs["password"] = settings.REDIS_PASSWORD
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            **kwargs,
        )
    return _redis_pool


async def close_redis() -> None:
    """Call at application shutdown to release connections gracefully."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.close()
        _redis_pool = None

