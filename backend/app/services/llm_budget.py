"""
app/services/llm_budget.py
──────────────────────────
Daily token budgets (docs/phase6-plan.md 7.7): one counter per tenant and one for the platform, per UTC day.

    usage:t:{tenant_id}:d:{yyyymmdd}:tokens      usage:platform:d:{yyyymmdd}:tokens      (INCRBY, TTL 48 h)

Use (no caller until 6a-10/6a-11):

    result = await default_budget.reserve(tenant_id, reservation_amount(req), tenant_override=override)
    if isinstance(result, BudgetExhausted):
        ...  # the retrieval-only answer (budget_retrieval_only); NOT an error
    else:
        ...  # make the call, then ALWAYS:
        await default_budget.settle(result, prompt_tokens + completion_tokens)   # or release(result) if no call was made

Rules:
  * reserve() checks the tenant limit and the platform limit and adds the amount to both counters in ONE Lua script:
    a refusal consumes nothing on either counter, and two reservations can never both take the last tokens;
  * the amount is the estimated prompt plus max_output_tokens (`reservation_amount`), so the call can never go
    over the budget by more than the provider's own over-run; settle() then corrects both counters to the usage the
    provider reported (or our estimate, when it reported none). A reservation is settled at most once. One that is
    never settled (a crash) stays counted: an over-count, which is the safe side;
  * an exhausted budget is a returned BudgetExhausted value, never an exception, so it cannot become a 500;
  * the limits are settings: LLM_TENANT_DAILY_TOKENS (or the tenant's tenant_settings.chat_daily_token_budget) and
    LLM_PLATFORM_DAILY_TOKENS. Both defaults are PLACEHOLDERS, set after Q1. A tenant override above the platform
    ceiling is refused: the default applies and a warning is logged (the settings route of 6a-12 answers 422 with
    `check_budget_override`);
  * the day is the UTC day of the reservation; a correction after midnight goes to the day it was reserved on;
  * every counter gets a 48 h TTL when it is first written; later writes never extend it.

Token estimate when a provider reports nothing (`estimate_text_tokens`): characters divided by the
LLM_ESTIMATE_CHARS_PER_TOKEN_* setting of their script, rounded up: Devanagari code points by the Devanagari ratio,
every other character by the Latin ratio. The ratios are UNVERIFIED (6a-6 measures them) and are settings only.

Redis down:
  * reserve() outside development: the same 503 service_unavailable as SlotManager (fail closed);
  * reserve() in development (chosen for 6a-5, like SlotManager's fallback): a per-process in-memory counter with
    the same limits, and an error-level log line `llm_budget_redis_unavailable_dev_memory`. The reservation records
    its backend, and a Redis-held reservation is never settled in memory (nor the other way round);
  * settle() never raises in any environment (the call already happened): a lost correction is logged and the
    reservation stays counted in full.

Keys hold the tenant id and the day; values are integers. No user id, no text.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fractions import Fraction
from typing import Any, Final, Literal

import structlog

from app.core import redis_client
from app.core.config import settings
from app.core.slots import REDIS_OP_TIMEOUT
from app.llm.breaker import service_unavailable
from app.llm.types import LLMRequest

logger = structlog.get_logger(__name__)

COUNTER_TTL_SECONDS: Final = 48 * 3600

BACKEND_REDIS: Final = "redis"
BACKEND_MEMORY: Final = "memory"  # development only: Redis was unreachable when the reservation was made

# Devanagari (U+0900-U+097F) and Devanagari Extended (U+A8E0-U+A8FF).
_DEVANAGARI_RANGES: Final = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))

# KEYS[1] tenant counter, KEYS[2] platform counter.
# ARGV[1] amount, ARGV[2] tenant limit, ARGV[3] platform limit, ARGV[4] ttl ms.
# Returns {0, tenant used after} when reserved, or {1, tenant used} / {2, platform used} for the limit that refused.
# Both limits are checked before anything is written, so a refusal changes neither counter.
_RESERVE_LUA = """
local amount = tonumber(ARGV[1])
local t = tonumber(redis.call('GET', KEYS[1]) or '0')
if t + amount > tonumber(ARGV[2]) then return {1, t} end
local p = tonumber(redis.call('GET', KEYS[2]) or '0')
if p + amount > tonumber(ARGV[3]) then return {2, p} end
for i = 1, 2 do
  redis.call('INCRBY', KEYS[i], amount)
  if redis.call('PTTL', KEYS[i]) < 0 then redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[4])) end
end
return {0, t + amount}
"""

# KEYS[1] tenant counter, KEYS[2] platform counter. ARGV[1] delta (may be negative), ARGV[2] ttl ms.
# A counter never goes below zero (Redis lost it in between), and it always ends with a TTL.
_ADJUST_LUA = """
for i = 1, #KEYS do
  local v = redis.call('INCRBY', KEYS[i], tonumber(ARGV[1]))
  if v < 0 then redis.call('SET', KEYS[i], 0, 'KEEPTTL') end
  if redis.call('PTTL', KEYS[i]) < 0 then redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[2])) end
end
return 1
"""


# ── keys and the day ─────────────────────────────────────────────────────────────────────────────────


def utc_day(moment: datetime) -> date:
    """The UTC calendar day of an aware datetime. A naive one is refused: it has no day."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("the budget clock must return an aware datetime")
    return moment.astimezone(UTC).date()


def tenant_day_key(tenant_id: uuid.UUID, day: date) -> str:
    return f"usage:t:{tenant_id}:d:{day:%Y%m%d}:tokens"


def platform_day_key(day: date) -> str:
    return f"usage:platform:d:{day:%Y%m%d}:tokens"


# ── the estimate ─────────────────────────────────────────────────────────────────────────────────────


def _is_devanagari(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _DEVANAGARI_RANGES)


def _ratio(value: float) -> Fraction:
    # Exact arithmetic from the setting's decimal text, so 5 characters at 2.5 per token are exactly 2 tokens.
    return Fraction(str(value))


def estimate_text_tokens(text: str) -> int:
    """Tokens of `text` by the UNVERIFIED characters-per-token settings, rounded up."""
    devanagari = sum(1 for ch in text if _is_devanagari(ch))
    other = len(text) - devanagari
    total = Fraction(other) / _ratio(settings.LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN) + Fraction(devanagari) / _ratio(
        settings.LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI
    )
    return math.ceil(total)


def estimate_prompt_tokens(req: LLMRequest) -> int:
    """The prompt of one request: the system prompt and every message."""
    return estimate_text_tokens(req.system) + sum(estimate_text_tokens(m.content) for m in req.messages)


def reservation_amount(req: LLMRequest) -> int:
    """What reserve() takes before a call: the estimated prompt plus the most the answer may use."""
    return estimate_prompt_tokens(req) + req.max_output_tokens


# ── limits and the override ──────────────────────────────────────────────────────────────────────────


class BudgetOverrideRefused(ValueError):
    """A tenant_settings.chat_daily_token_budget value that may not be stored (6a-12 answers 422 with `code`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_budget_override(value: int | None) -> None:
    """None (the platform default) or a positive integer up to LLM_PLATFORM_DAILY_TOKENS."""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BudgetOverrideRefused("token_budget_invalid", "The daily token budget must be a positive whole number.")
    if value > settings.LLM_PLATFORM_DAILY_TOKENS:
        raise BudgetOverrideRefused(
            "token_budget_above_platform_ceiling", "The daily token budget may not be above the platform ceiling."
        )


def effective_tenant_limit(override: int | None) -> int:
    """The tenant's daily limit: its override when valid, else LLM_TENANT_DAILY_TOKENS. An override above the
    platform ceiling (stored before the ceiling was lowered) is refused, not applied."""
    if override is None:
        return settings.LLM_TENANT_DAILY_TOKENS
    try:
        check_budget_override(override)
    except BudgetOverrideRefused as exc:
        logger.warning("llm_budget_override_refused", code=exc.code)
        return settings.LLM_TENANT_DAILY_TOKENS
    return override


# ── reservations ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True, eq=False)
class Reservation:
    """Tokens taken for one call. Settle it exactly once (settle or release)."""

    tenant_id: uuid.UUID
    day: date
    amount: int
    backend: str = BACKEND_REDIS
    settled: bool = field(default=False)

    @property
    def keys(self) -> tuple[str, str]:
        return tenant_day_key(self.tenant_id, self.day), platform_day_key(self.day)


@dataclass(frozen=True, slots=True)
class BudgetExhausted:
    """The call must not be made: the caller gives the retrieval-only answer (plan 7.5). Not an error."""

    scope: Literal["tenant", "platform"]
    limit: int
    used: int
    requested: int
    code: str = "budget_exhausted"


def _positive_int(name: str, value: object, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


class TokenBudget:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        # Development fallback only (Redis unreachable): key -> tokens, one process.
        self._mem: dict[str, int] = {}

    def today(self) -> date:
        return utc_day(self._now())

    async def reserve(
        self, tenant_id: uuid.UUID, amount: int, *, tenant_override: int | None
    ) -> Reservation | BudgetExhausted:
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a UUID")
        amount = _positive_int("amount", amount, 1)
        day = self.today()
        tenant_limit = effective_tenant_limit(tenant_override)
        platform_limit = settings.LLM_PLATFORM_DAILY_TOKENS
        keys = (tenant_day_key(tenant_id, day), platform_day_key(day))
        backend = BACKEND_REDIS
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                res: Any = await redis.eval(  # type: ignore[no-untyped-call]
                    _RESERVE_LUA, 2, *keys, amount, tenant_limit, platform_limit, COUNTER_TTL_SECONDS * 1000
                )
            which, used = int(res[0]), int(res[1])
        except Exception as exc:
            if not settings.is_development:
                logger.error("llm_budget_redis_unavailable", error=type(exc).__name__)
                raise service_unavailable() from None
            logger.error("llm_budget_redis_unavailable_dev_memory", error=type(exc).__name__)
            which, used = self._mem_reserve(keys, amount, tenant_limit, platform_limit)
            backend = BACKEND_MEMORY
        if which == 1:
            return BudgetExhausted(scope="tenant", limit=tenant_limit, used=used, requested=amount)
        if which == 2:
            return BudgetExhausted(scope="platform", limit=platform_limit, used=used, requested=amount)
        return Reservation(tenant_id=tenant_id, day=day, amount=amount, backend=backend)

    async def settle(self, reservation: Reservation, used_tokens: int) -> None:
        """Correct both counters from the reserved amount to `used_tokens`. Once only; never raises for Redis."""
        used_tokens = _positive_int("used_tokens", used_tokens, 0)
        if reservation.settled:
            return
        reservation.settled = True
        delta = used_tokens - reservation.amount
        if delta == 0:
            return
        if reservation.backend == BACKEND_MEMORY:
            for key in reservation.keys:
                self._mem[key] = max(0, self._mem.get(key, 0) + delta)
            return
        try:
            redis = await redis_client.get_redis()
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                await redis.eval(  # type: ignore[no-untyped-call]
                    _ADJUST_LUA, 2, *reservation.keys, delta, COUNTER_TTL_SECONDS * 1000
                )
        except Exception as exc:
            logger.warning("llm_budget_correction_lost", error=type(exc).__name__, delta=delta)

    async def release(self, reservation: Reservation) -> None:
        """No call was made: give the whole reservation back."""
        await self.settle(reservation, 0)

    def _mem_reserve(
        self, keys: tuple[str, str], amount: int, tenant_limit: int, platform_limit: int
    ) -> tuple[int, int]:
        # One event loop and no await in here: atomic within the process, like SlotManager's fallback.
        t, p = self._mem.get(keys[0], 0), self._mem.get(keys[1], 0)
        if t + amount > tenant_limit:
            return 1, t
        if p + amount > platform_limit:
            return 2, p
        self._mem[keys[0]], self._mem[keys[1]] = t + amount, p + amount
        return 0, t + amount


default_budget = TokenBudget()
