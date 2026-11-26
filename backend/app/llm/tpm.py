"""
app/llm/tpm.py
──────────────
The token-weighted tokens-per-minute window of one model (docs/phase6-plan.md 6a-10b).

    llm:tpm:{model_key}    a Redis sorted set: member `<token>:<weight>`, score = expiry in ms (reserved at + 60 s)

The chain (app/llm/chain.py) uses it only for a model whose models.json entry has a `tpm`:

    result = await window.reserve(model_key, tpm, weight)     # weight: the budget service's reservation_amount
    if isinstance(result, TpmRejected):
        ...  # skip the model (result.reason "full" with a retry-after, or "oversize"): NEVER wait here
    else:
        ...  # make the call, then ALWAYS settle(result, reported tokens) or release(result)

Rules:
  * reserve() drops the entries that have left the window, adds up the rest, and either refuses or adds the new
    entry, all in ONE Lua script (one Redis call, never two commands), so two reservations can never both take the
    last tokens. A refusal consumes nothing;
  * a full window is a returned TpmRejected (reason "full") with the seconds until enough weight has left the
    window for this request to fit (rounded up). Nothing sleeps. A request that is heavier than the whole limit
    can never fit: it is TpmRejected (reason "oversize", no retry-after) at once, without a Redis call;
  * settle() replaces the entry's weight by the reported count (prompt + completion, hidden reasoning included):
    more is added, less is refunded (one script, one call). The weight never goes below zero. The entry keeps its
    expiry, so a correction never makes tokens stay longer than the minute they were taken in. An entry that has
    already left the window is not brought back: nothing is added or refunded for it. A reservation is settled at
    most once; one that is never settled (a crash) stays counted in full until it leaves the window (the safe side);
  * the key has a ttl of one window on every write, so nothing stays reserved forever;
  * the clock is injectable (`now_ms`, wall-clock milliseconds, default time.time) and is passed to the scripts, so
    a test moves time by hand;
  * Redis down follows SlotManager: outside development reserve() raises the 503 `service_unavailable`; in
    development it uses a per-process window with the same rules and logs `llm_tpm_redis_unavailable_dev_memory`.
    A reservation records its backend, and a Redis-held reservation is never settled in memory. settle() and
    release() never raise (the call already happened): a lost correction is logged and the entry expires by itself.

Keys hold a model key; members hold a random token and a number. No tenant id, no user id, no text.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog

from app.core import redis_client
from app.core.config import settings
from app.core.slots import REDIS_OP_TIMEOUT
from app.llm.breaker import service_unavailable

logger = structlog.get_logger(__name__)

TPM_WINDOW_S: Final = 60.0
BACKEND_REDIS: Final = "redis"
BACKEND_MEMORY: Final = "memory"  # development only: Redis was unreachable when the reservation was made

NowMs = Callable[[], int]


def tpm_key(model_key: str) -> str:
    return f"llm:tpm:{model_key}"


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


# KEYS[1] the window. ARGV: 1 now ms, 2 window ms, 3 limit, 4 weight, 5 token.
# Returns {0, 0, used after} when reserved, or {1, retry-after ms, used} when the window is full.
# The retry-after is the time until enough weight has left (oldest entries first) for the new weight to fit.
# Entries are `<token>:<weight>`; the score is the expiry in ms; an entry whose expiry is at or before now is gone.
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local weight = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local used = 0
for i = 1, #entries, 2 do
  used = used + tonumber(string.match(entries[i], ':(%d+)$'))
end
if used + weight > limit then
  local free = used
  local retry = window
  for i = 1, #entries, 2 do
    free = free - tonumber(string.match(entries[i], ':(%d+)$'))
    if free + weight <= limit then
      retry = math.max(1, tonumber(entries[i + 1]) - now)
      break
    end
  end
  return {1, retry, used}
end
redis.call('ZADD', KEYS[1], now + window, ARGV[5] .. ':' .. ARGV[4])
redis.call('PEXPIRE', KEYS[1], window)
return {0, 0, used + weight}
"""

# KEYS[1] the window. ARGV: 1 now ms, 2 token, 3 delta (may be negative).
# The entry's weight becomes max(0, weight + delta), in place and with its own expiry; a weight of 0 removes it.
# An entry that is not there (it left the window, or Redis lost it) is left alone: nothing is created.
# Returns {1, new weight} when corrected, {0, 0} when there was no such entry.
_ADJUST_LUA = """
local now = tonumber(ARGV[1])
local delta = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
for i = 1, #entries, 2 do
  local token, weight = string.match(entries[i], '^(.*):(%d+)$')
  if token == ARGV[2] then
    local updated = tonumber(weight) + delta
    if updated < 0 then updated = 0 end
    redis.call('ZREM', KEYS[1], entries[i])
    if updated > 0 then
      redis.call('ZADD', KEYS[1], entries[i + 1], token .. ':' .. string.format('%d', updated))
    end
    return {1, updated}
  end
end
return {0, 0}
"""


def _whole_number(name: str, value: object, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


@dataclass(slots=True, eq=False)
class TpmReservation:
    """Tokens taken in one model's window for one call. Settle it exactly once (settle or release)."""

    model_key: str
    token: str
    weight: int
    backend: str = BACKEND_REDIS
    settled: bool = field(default=False)


@dataclass(frozen=True, slots=True)
class TpmRejected:
    """The model cannot take this request now. Not an error: the chain skips the model (plan 6a-10b).

    reason "full":     the window is full; `retry_after_seconds` says when enough weight will have left it.
    reason "oversize": the request alone is heavier than the model's whole limit, so it never fits (no retry-after).
    """

    reason: Literal["full", "oversize"]
    limit: int
    used: int
    requested: int
    retry_after_seconds: int | None


class TpmWindow:
    def __init__(self, *, now_ms: NowMs | None = None) -> None:
        self._now_ms = now_ms or _wall_clock_ms
        # Development fallback only (Redis unreachable): model key -> {token: (expiry ms, weight)}, one process.
        self._mem: dict[str, dict[str, tuple[int, int]]] = {}

    async def reserve(self, model_key: str, limit: int, weight: int) -> TpmReservation | TpmRejected:
        limit = _whole_number("limit", limit, 1)
        weight = _whole_number("weight", weight, 1)
        if weight > limit:  # it can never fit, however long it waits: no Redis call, no retry-after
            return TpmRejected(reason="oversize", limit=limit, used=0, requested=weight, retry_after_seconds=None)
        token = uuid.uuid4().hex
        now = self._now_ms()
        window_ms = int(TPM_WINDOW_S * 1000)
        backend = BACKEND_REDIS
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                res: Any = await redis.eval(  # type: ignore[no-untyped-call]
                    _RESERVE_LUA, 1, tpm_key(model_key), now, window_ms, limit, weight, token
                )
            refused, retry_ms, used = int(res[0]), int(res[1]), int(res[2])
        except Exception as exc:
            if not settings.is_development:
                logger.error("llm_tpm_redis_unavailable", error=type(exc).__name__)
                raise service_unavailable() from None
            logger.error("llm_tpm_redis_unavailable_dev_memory", error=type(exc).__name__)
            refused, retry_ms, used = self._mem_reserve(model_key, token, now, window_ms, limit, weight)
            backend = BACKEND_MEMORY
        if refused:
            return TpmRejected(
                reason="full", limit=limit, used=used, requested=weight, retry_after_seconds=-(-retry_ms // 1000)
            )
        return TpmReservation(model_key=model_key, token=token, weight=weight, backend=backend)

    async def settle(self, reservation: TpmReservation, used_tokens: int) -> None:
        """Correct the reservation to `used_tokens`. Once only; never raises for Redis."""
        used_tokens = _whole_number("used_tokens", used_tokens, 0)
        if reservation.settled:
            return
        reservation.settled = True
        delta = used_tokens - reservation.weight
        if delta == 0:
            return
        now = self._now_ms()
        if reservation.backend == BACKEND_MEMORY:
            self._mem_adjust(reservation, now, delta)
            return
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                await redis.eval(  # type: ignore[no-untyped-call]
                    _ADJUST_LUA, 1, tpm_key(reservation.model_key), now, reservation.token, delta
                )
        except Exception as exc:
            logger.warning("llm_tpm_correction_lost", error=type(exc).__name__, delta=delta)

    async def release(self, reservation: TpmReservation) -> None:
        """No tokens were used: give the whole reservation back."""
        await self.settle(reservation, 0)

    # ── development fallback (one event loop, no await in here: atomic within the process) ──

    def _mem_live(self, model_key: str, now: int) -> dict[str, tuple[int, int]]:
        live = {t: entry for t, entry in self._mem.get(model_key, {}).items() if entry[0] > now}
        self._mem[model_key] = live
        return live

    def _mem_reserve(
        self, model_key: str, token: str, now: int, window_ms: int, limit: int, weight: int
    ) -> tuple[int, int, int]:
        live = self._mem_live(model_key, now)
        used = sum(w for _, w in live.values())
        if used + weight > limit:
            free = used
            retry = window_ms
            for expiry, w in sorted(live.values()):
                free -= w
                if free + weight <= limit:
                    retry = max(1, expiry - now)
                    break
            return 1, retry, used
        live[token] = (now + window_ms, weight)
        return 0, 0, used + weight

    def _mem_adjust(self, reservation: TpmReservation, now: int, delta: int) -> None:
        live = self._mem_live(reservation.model_key, now)
        entry = live.get(reservation.token)
        if entry is None:
            return
        updated = max(0, entry[1] + delta)
        if updated == 0:
            del live[reservation.token]
        else:
            live[reservation.token] = (entry[0], updated)

