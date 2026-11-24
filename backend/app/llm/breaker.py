"""
app/llm/breaker.py
──────────────────
The provider breaker and the model cooldowns, in Redis (docs/phase6-plan.md 7.4).

Breaker `llm:cb:{provider}` (a hash: open_until, probe_until, probe_token) with its failure window
`llm:cb:{provider}:fails` (a sorted set of failure times):
  * closed: every call is let through; a failure (LLMUnavailable, LLMTimeout) is added to the window;
  * 5 failures inside 60 s open it for 60 s: every call is refused;
  * then half-open: exactly ONE caller gets a probe permit (claimed atomically, held for at most
    `probe_ttl_s`); everybody else is still refused. The probe's result closes the breaker (the provider
    answered) or opens it again for 60 s. A probe that ends with no result (the reader left) gives its permit
    back, and a probe that never reports loses it after `probe_ttl_s`;
  * an auth error opens it at once (`trip`).
Cooldown `llm:cooldown:{model_key}` (a string: the time it ends, PX = the cooldown): a 429 makes the chain skip
that model until Retry-After has passed. A shorter cooldown never shortens a longer one.

Every change is one Lua script, so two API processes can never both take the probe. Time is the Redis clock
unless a test passes `now_ms` (a fake clock). Keys hold a provider name or a model key and nothing else: no
tenant, no user, no text of a question or an answer.

Redis down:
  * the checks that let a call through (`admit`, `remaining_ms`) fail CLOSED outside development with the
    same 503 `service_unavailable` as SlotManager; in development they let the call through (no breaker, no
    cooldown) and log an error;
  * the records made after a call (`record_failure`, `record_success`, `release`, `trip`, `start`) never
    raise: the call already happened, and losing one count must not turn an answer into an error.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog
from fastapi import HTTPException, status

from app.core import redis_client
from app.core.config import settings
from app.core.slots import DEFAULT_UNAVAILABLE_MESSAGE, REDIS_OP_TIMEOUT

logger = structlog.get_logger(__name__)

# How long the breaker hash lives without a change. It is rewritten on every state change, so this only
# cleans up after a provider nobody calls any more (an expired hash reads as closed).
STATE_TTL_MS: Final = 3_600_000

_NOW = """
local function now_ms(arg)
  if arg ~= nil and arg ~= '' then return tonumber(arg) end
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
"""

# KEYS[1] state hash. ARGV: now ('' = Redis clock), probe ttl ms, probe token, state ttl ms.
# Returns 'closed', 'open' or 'probe' (this caller holds the one probe permit).
_ADMIT = _NOW + """
local now = now_ms(ARGV[1])
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until == 0 then return 'closed' end
if now < open_until then return 'open' end
local probe_until = tonumber(redis.call('HGET', KEYS[1], 'probe_until') or '0')
if probe_until > now then return 'open' end
redis.call('HSET', KEYS[1], 'probe_until', now + tonumber(ARGV[2]), 'probe_token', ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[4]))
return 'probe'
"""

# KEYS[1] state hash, KEYS[2] failure window. ARGV: now, window ms, threshold, open ms, member, probe token
# ('' = no probe), state ttl ms.
_FAILURE = _NOW + """
local now = now_ms(ARGV[1])
if ARGV[6] ~= '' and redis.call('HGET', KEYS[1], 'probe_token') == ARGV[6] then
  redis.call('DEL', KEYS[2])
  redis.call('HDEL', KEYS[1], 'probe_until', 'probe_token')
  redis.call('HSET', KEYS[1], 'open_until', now + tonumber(ARGV[4]))
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[7]))
  return 'reopened'
end
if tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0') > 0 then
  return 'ignored'
end
redis.call('ZADD', KEYS[2], now, ARGV[5])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - tonumber(ARGV[2]))
redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[2]))
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
  redis.call('DEL', KEYS[2])
  redis.call('HDEL', KEYS[1], 'probe_until', 'probe_token')
  redis.call('HSET', KEYS[1], 'open_until', now + tonumber(ARGV[4]))
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[7]))
  return 'opened'
end
return 'counted'
"""

# KEYS[1] state hash, KEYS[2] failure window. ARGV: probe token. Only the probe holder can close it.
_SUCCESS = """
if ARGV[1] ~= '' and redis.call('HGET', KEYS[1], 'probe_token') == ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2])
  return 'closed'
end
return 'unchanged'
"""

# KEYS[1] state hash. ARGV: probe token. The probe ended with no result: the next caller may probe.
_RELEASE = """
if ARGV[1] ~= '' and redis.call('HGET', KEYS[1], 'probe_token') == ARGV[1] then
  redis.call('HDEL', KEYS[1], 'probe_until', 'probe_token')
  return 'released'
end
return 'unchanged'
"""

# KEYS[1] state hash, KEYS[2] failure window. ARGV: now, open ms, state ttl ms.
_TRIP = _NOW + """
local now = now_ms(ARGV[1])
redis.call('DEL', KEYS[2])
redis.call('HDEL', KEYS[1], 'probe_until', 'probe_token')
redis.call('HSET', KEYS[1], 'open_until', now + tonumber(ARGV[2]))
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
return 'opened'
"""

# KEYS[1] state hash. ARGV: now. Returns 'closed', 'open' or 'half_open'.
_STATE = _NOW + """
local now = now_ms(ARGV[1])
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until') or '0')
if open_until == 0 then return 'closed' end
if now < open_until then return 'open' end
return 'half_open'
"""

# KEYS[1] cooldown key. ARGV: now, cooldown ms. Never shortens a longer cooldown.
_COOLDOWN_START = _NOW + """
local now = now_ms(ARGV[1])
local ms = tonumber(ARGV[2])
local ends = now + ms
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if ends > current then
  redis.call('SET', KEYS[1], ends, 'PX', ms)
  return 1
end
return 0
"""

# KEYS[1] cooldown key. ARGV: now. Milliseconds left, 0 when there is no cooldown.
_COOLDOWN_LEFT = _NOW + """
local now = now_ms(ARGV[1])
local ends = tonumber(redis.call('GET', KEYS[1]) or '0')
if ends > now then return ends - now end
return 0
"""


def breaker_key(provider: str) -> str:
    return f"llm:cb:{provider}"


def _fails_key(provider: str) -> str:
    return f"llm:cb:{provider}:fails"


def cooldown_key(model_key: str) -> str:
    return f"llm:cooldown:{model_key}"


def service_unavailable() -> HTTPException:
    """The 503 SlotManager raises when Redis cannot be reached (same code, same text)."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "service_unavailable", "message": DEFAULT_UNAVAILABLE_MESSAGE},
    )


NowMs = Callable[[], int]


async def _eval(script: str, keys: Sequence[str], args: Sequence[str | int]) -> Any:
    redis = await redis_client.get_redis()
    async with asyncio.timeout(REDIS_OP_TIMEOUT):
        return await redis.eval(script, len(keys), *keys, *args)  # type: ignore[no-untyped-call]


def _gate_failed(what: str, exc: Exception) -> None:
    """A check that lets a call through could not reach Redis. Raise 503 outside development."""
    if not settings.is_development:
        logger.error("llm_gate_redis_unavailable", check=what, error=type(exc).__name__)
        raise service_unavailable() from None
    logger.error("llm_gate_redis_unavailable_dev_allowing", check=what, error=type(exc).__name__)


def _record_failed(what: str, exc: Exception) -> None:
    logger.warning("llm_record_redis_unavailable", record=what, error=type(exc).__name__)


class Admission(enum.Enum):
    CLOSED = "closed"
    PROBE = "probe"  # this caller holds the one half-open probe permit
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class Permit:
    state: Admission
    probe_token: str | None = None


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    failures: int = 5
    window_s: float = 60.0
    open_s: float = 60.0
    # How long a probe keeps its permit when it never reports back: longer than LLM_TIMEOUT_TOTAL (45 s)
    probe_ttl_s: float = 60.0


class Breaker:
    def __init__(self, *, config: BreakerConfig | None = None, now_ms: NowMs | None = None) -> None:
        self._config = config or BreakerConfig()
        self._now_ms = now_ms

    @staticmethod
    def untracked() -> Permit:
        """A permit that is not a probe (a call let through while closed)."""
        return Permit(state=Admission.CLOSED)

    def _now(self) -> str:
        return "" if self._now_ms is None else str(self._now_ms())

    async def admit(self, provider: str) -> Permit:
        token = uuid.uuid4().hex
        c = self._config
        try:
            answer = await _eval(
                _ADMIT,
                [breaker_key(provider)],
                [self._now(), int(c.probe_ttl_s * 1000), token, STATE_TTL_MS],
            )
        except Exception as exc:
            _gate_failed("breaker", exc)
            return self.untracked()
        state = Admission(str(answer))
        return Permit(state=state, probe_token=token if state is Admission.PROBE else None)

    async def record_failure(self, provider: str, permit: Permit) -> None:
        c = self._config
        try:
            result = await _eval(
                _FAILURE,
                [breaker_key(provider), _fails_key(provider)],
                [
                    self._now(),
                    int(c.window_s * 1000),
                    c.failures,
                    int(c.open_s * 1000),
                    uuid.uuid4().hex,
                    permit.probe_token or "",
                    STATE_TTL_MS,
                ],
            )
        except Exception as exc:
            _record_failed("breaker_failure", exc)
            return
        if result in ("opened", "reopened"):
            logger.warning("llm_breaker_opened", provider=provider, how=str(result))

    async def record_success(self, provider: str, permit: Permit) -> None:
        if permit.probe_token is None:
            return
        try:
            result = await _eval(_SUCCESS, [breaker_key(provider), _fails_key(provider)], [permit.probe_token])
        except Exception as exc:
            _record_failed("breaker_success", exc)
            return
        if result == "closed":
            logger.info("llm_breaker_closed", provider=provider)

    async def release(self, provider: str, permit: Permit) -> None:
        if permit.probe_token is None:
            return
        try:
            await _eval(_RELEASE, [breaker_key(provider)], [permit.probe_token])
        except Exception as exc:
            _record_failed("breaker_release", exc)

    async def trip(self, provider: str) -> None:
        try:
            await _eval(
                _TRIP,
                [breaker_key(provider), _fails_key(provider)],
                [self._now(), int(self._config.open_s * 1000), STATE_TTL_MS],
            )
        except Exception as exc:
            _record_failed("breaker_trip", exc)
            return
        logger.warning("llm_breaker_opened", provider=provider, how="tripped")

    async def state(self, provider: str) -> str:
        """'closed', 'open' or 'half_open' (for tests and operators). Raises when Redis is down."""
        return str(await _eval(_STATE, [breaker_key(provider)], [self._now()]))


class Cooldowns:
    def __init__(self, *, now_ms: NowMs | None = None) -> None:
        self._now_ms = now_ms

    def _now(self) -> str:
        return "" if self._now_ms is None else str(self._now_ms())

    async def remaining_ms(self, model_key: str) -> int:
        try:
            return int(await _eval(_COOLDOWN_LEFT, [cooldown_key(model_key)], [self._now()]))
        except Exception as exc:
            _gate_failed("cooldown", exc)
            return 0

    async def start(self, model_key: str, seconds: float) -> None:
        ms = max(0, round(seconds * 1000))
        if ms == 0:
            return
        try:
            await _eval(_COOLDOWN_START, [cooldown_key(model_key)], [self._now(), ms])
        except Exception as exc:
            _record_failed("cooldown", exc)
            return
        logger.info("llm_model_cooling_down", model_key=model_key, seconds=ms / 1000)
