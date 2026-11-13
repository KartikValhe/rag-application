"""
app/core/slots.py
─────────────────
Atomic slot manager used for concurrency caps and sliding-window rate limits.

Each limit is a Redis sorted set. Members are slot tokens, scores are expiry
timestamps (milliseconds, taken from the Redis clock). One Lua script does
"drop expired tokens, check every cap, add the token to every set" in a single
atomic step, so:

- a crashed or killed worker never leaks a slot: its token simply expires;
- attempts (even rejected ones) never extend the life of other tokens;
- a rejection on a later limit consumes nothing on the earlier limits.

Redis is checked on every call. If it cannot be reached we fail CLOSED (503)
unless ENVIRONMENT is "development", where a per-process in-memory fallback is
used and a loud error is logged.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core import redis_client
from app.core.config import settings

logger = logging.getLogger(__name__)

REDIS_OP_TIMEOUT = 1.0

# KEYS[i]        sorted set for limit i
# ARGV[1]        token
# ARGV[2i], ARGV[2i+1]  limit and ttl in milliseconds for limit i
# Returns {0, 0} on success, or {i, retry_after_ms} for the first limit that is full.
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local token = ARGV[1]
for i = 1, #KEYS do
  local limit = tonumber(ARGV[2 * i])
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
  if redis.call('ZCARD', KEYS[i]) >= limit then
    local oldest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
    local retry = 0
    if oldest[2] then retry = math.max(0, tonumber(oldest[2]) - now) end
    return {i, retry}
  end
end
for i = 1, #KEYS do
  local ttl = tonumber(ARGV[2 * i + 1])
  redis.call('ZADD', KEYS[i], now + ttl, token)
  redis.call('PEXPIRE', KEYS[i], ttl)
end
return {0, 0}
"""

_COUNT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""


@dataclass(frozen=True)
class SlotLimit:
    key: str
    limit: int
    ttl_seconds: float


@dataclass(frozen=True)
class SlotLease:
    token: str
    keys: tuple[str, ...]


class SlotRejected(Exception):
    """A cap is full. `index` is the position of the limit that rejected the request."""

    def __init__(self, index: int, retry_after_seconds: int) -> None:
        super().__init__(f"slot limit {index} reached")
        self.index = index
        self.retry_after_seconds = retry_after_seconds


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "service_unavailable",
            "message": "Timetable import is temporarily unavailable. Please try again in a few minutes.",
        },
    )


class SlotManager:
    def __init__(self) -> None:
        # In-memory store, only used in development when Redis is unreachable
        self._mem: dict[str, dict[str, float]] = {}

    # ── Public API ──────────────────────────────────────────────────────────
    async def acquire(self, limits: Sequence[SlotLimit]) -> SlotLease:
        token = uuid.uuid4().hex
        keys = tuple(lim.key for lim in limits)
        try:
            redis = await redis_client.get_redis()
            args: list[str | int] = [token]
            for lim in limits:
                args += [lim.limit, max(1, int(lim.ttl_seconds * 1000))]
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                res = await redis.eval(_ACQUIRE_LUA, len(keys), *keys, *args)  # type: ignore[no-untyped-call]
            index, retry_ms = int(res[0]), int(res[1])
        except Exception as exc:
            if not settings.is_development:
                logger.error("Redis unavailable for timetable slots, failing closed: %r", exc)
                raise _unavailable() from exc
            logger.error(
                "REDIS UNAVAILABLE: using per-process in-memory slot limits (development only): %r", exc
            )
            index, retry_ms = self._mem_acquire(token, limits)
        if index:
            raise SlotRejected(index - 1, -(-retry_ms // 1000))
        return SlotLease(token=token, keys=keys)

    async def release(self, lease: SlotLease) -> None:
        """Best effort. If this fails the token expires on its own."""
        for key in lease.keys:
            self._mem.get(key, {}).pop(lease.token, None)
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                for key in lease.keys:
                    await redis.zrem(key, lease.token)
        except Exception as exc:
            logger.warning("Could not release slot (it will expire by itself): %r", exc)

    async def count(self, key: str) -> int:
        """Number of live tokens for a key."""
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                return int(await redis.eval(_COUNT_LUA, 1, key))  # type: ignore[no-untyped-call]
        except Exception:
            if not settings.is_development:
                raise _unavailable() from None
            return self._mem_count(key)

    # ── In-memory fallback (single event loop, no awaits: atomic) ───────────
    def _mem_count(self, key: str) -> int:
        now = time.monotonic()
        live = {t: exp for t, exp in self._mem.get(key, {}).items() if exp > now}
        self._mem[key] = live
        return len(live)

    def _mem_acquire(self, token: str, limits: Sequence[SlotLimit]) -> tuple[int, int]:
        now = time.monotonic()
        for i, lim in enumerate(limits, start=1):
            if self._mem_count(lim.key) >= lim.limit:
                oldest = min(self._mem[lim.key].values())
                return i, int(max(0.0, oldest - now) * 1000)
        for lim in limits:
            self._mem[lim.key][token] = now + lim.ttl_seconds
        return 0, 0


default_manager = SlotManager()
