"""
Step 6a-5 (docs/phase6-plan.md 7.7): the daily token counters on the test Redis (DB 1).

  usage:t:{tenant}:d:{yyyymmdd}:tokens and usage:platform:d:{yyyymmdd}:tokens, INCRBY, TTL 48 h.

reserve(estimated prompt + max output) checks both limits and adds to both counters in ONE Lua script, so a rejection
consumes nothing and two reservations can never both take the last tokens. settle() corrects the reservation to the
reported (or estimated) usage. An exhausted budget is a returned value, never an exception (6a-11 turns it into the
retrieval-only answer). The UTC day comes from a fake clock, so nothing here waits.

The concurrency tests force the overlap: every reservation's first Redis call waits at a barrier until all of them
have sent it, and the test asserts that every reservation was in flight before the first one finished.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.core import redis_client
from app.core.config import settings
from app.services import llm_budget
from app.services.llm_budget import (
    COUNTER_TTL_SECONDS,
    BudgetExhausted,
    Reservation,
    TokenBudget,
    platform_day_key,
    tenant_day_key,
)

# Kept at import: a test that makes Redis "go down" patches redis_client.get_redis; cleanup needs the real one.
_real_get_redis = redis_client.get_redis

DAY = date(2026, 11, 25)


class DayClock:
    """The only clock the budget sees here: a UTC datetime that tests move by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


async def _usage_keys() -> list[str]:
    r = await _real_get_redis()
    return sorted([k async for k in r.scan_iter(match="usage:*", count=500)])


async def _delete_usage_keys() -> None:
    keys = await _usage_keys()
    if keys:
        await (await _real_get_redis()).delete(*keys)


@pytest.fixture(autouse=True)
async def clean_usage_keys() -> AsyncIterator[None]:
    await _delete_usage_keys()
    yield
    await _delete_usage_keys()


@pytest.fixture
def clock() -> DayClock:
    return DayClock()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Small limits, set through the settings (the only place they live)."""
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 3000)


@pytest.fixture
def budget(clock: DayClock, limits: None) -> TokenBudget:
    return TokenBudget(now=clock)


async def _get(key: str) -> int | None:
    value = await (await _real_get_redis()).get(key)
    return None if value is None else int(value)


async def _ttl(key: str) -> int:
    return int(await (await _real_get_redis()).pttl(key))


async def _counters(tid: uuid.UUID, day: date = DAY) -> tuple[int | None, int | None]:
    return await _get(tenant_day_key(tid, day)), await _get(platform_day_key(day))


def _reserved(result: Reservation | BudgetExhausted) -> Reservation:
    assert isinstance(result, Reservation), f"expected a reservation, got {result!r}"
    return result


# ── reserve ──────────────────────────────────────────────────────────────────────────────────────────


async def test_reserve_adds_the_amount_to_both_counters_with_a_48_hour_ttl(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 300, tenant_override=None))
    assert (r.tenant_id, r.day, r.amount) == (tid, DAY, 300)
    assert await _counters(tid) == (300, 300)
    for key in (tenant_day_key(tid, DAY), platform_day_key(DAY)):
        ttl = await _ttl(key)
        assert 0 < ttl <= COUNTER_TTL_SECONDS * 1000, f"{key} has no 48 h TTL (pttl {ttl})"
        assert ttl > (COUNTER_TTL_SECONDS - 60) * 1000


async def test_a_later_reservation_never_extends_the_ttl(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    _reserved(await budget.reserve(tid, 100, tenant_override=None))
    await (await _real_get_redis()).pexpire(tenant_day_key(tid, DAY), 5_000)
    _reserved(await budget.reserve(tid, 100, tenant_override=None))
    assert 0 < await _ttl(tenant_day_key(tid, DAY)) <= 5_000


async def test_the_budget_can_be_filled_exactly_but_not_by_one_more_token(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    _reserved(await budget.reserve(tid, 999, tenant_override=None))
    _reserved(await budget.reserve(tid, 1, tenant_override=None))
    result = await budget.reserve(tid, 1, tenant_override=None)
    assert result == BudgetExhausted(scope="tenant", limit=1000, used=1000, requested=1)
    assert await _counters(tid) == (1000, 1000)


async def test_tenant_exhausted_is_a_value_and_consumes_nothing(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    _reserved(await budget.reserve(tid, 700, tenant_override=None))
    result = await budget.reserve(tid, 301, tenant_override=None)
    assert isinstance(result, BudgetExhausted)
    assert (result.scope, result.code) == ("tenant", "budget_exhausted")
    assert await _counters(tid) == (700, 700), "a refused reservation changed a counter"


async def test_platform_exhausted_consumes_nothing_on_the_tenant_counter(budget: TokenBudget) -> None:
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    for t in (a, b):
        _reserved(await budget.reserve(t, 1000, tenant_override=None))
    _reserved(await budget.reserve(c, 900, tenant_override=None))  # platform at 2900 of 3000
    result = await budget.reserve(d, 101, tenant_override=None)
    assert result == BudgetExhausted(scope="platform", limit=3000, used=2900, requested=101)
    assert await _get(tenant_day_key(d, DAY)) is None, "the tenant counter was touched by a platform refusal"
    assert await _get(platform_day_key(DAY)) == 2900


async def test_an_amount_above_the_whole_budget_is_exhausted_at_once(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    assert isinstance(await budget.reserve(tid, 1001, tenant_override=None), BudgetExhausted)
    assert await _counters(tid) == (None, None)


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "10"])
async def test_the_amount_must_be_a_positive_integer(amount: Any, budget: TokenBudget) -> None:
    with pytest.raises(ValueError):
        await budget.reserve(uuid.uuid4(), amount, tenant_override=None)


async def test_the_tenant_id_must_be_a_uuid(budget: TokenBudget) -> None:
    with pytest.raises(TypeError):
        await budget.reserve("not-a-uuid", 10, tenant_override=None)  # type: ignore[arg-type]


# ── tenant override ──────────────────────────────────────────────────────────────────────────────────


async def test_the_tenant_override_replaces_the_default(budget: TokenBudget) -> None:
    low, high = uuid.uuid4(), uuid.uuid4()
    assert isinstance(await budget.reserve(low, 201, tenant_override=200), BudgetExhausted)
    _reserved(await budget.reserve(low, 200, tenant_override=200))
    _reserved(await budget.reserve(high, 2500, tenant_override=2500))  # above the 1000 default, below the ceiling
    assert await _counters(high) == (2500, 2700)


async def test_an_override_above_the_platform_ceiling_is_refused(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    with capture_logs() as logs:
        result = await budget.reserve(tid, 1500, tenant_override=3001)
    assert result == BudgetExhausted(scope="tenant", limit=1000, used=0, requested=1500)
    assert "llm_budget_override_refused" in [e["event"] for e in logs]


async def test_the_limits_come_from_the_settings(budget: TokenBudget, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 50)
    assert isinstance(await budget.reserve(tid, 51, tenant_override=None), BudgetExhausted)
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 40)
    result = await budget.reserve(tid, 41, tenant_override=None)
    assert isinstance(result, BudgetExhausted) and result.scope == "platform"


# ── settle (the correction) ──────────────────────────────────────────────────────────────────────────


async def test_settle_corrects_down_to_the_reported_usage(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 800, tenant_override=None))
    await budget.settle(r, 250)
    assert await _counters(tid) == (250, 250)
    # The tokens given back can be reserved again.
    _reserved(await budget.reserve(tid, 750, tenant_override=None))


async def test_settle_corrects_up_when_the_provider_used_more(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 100, tenant_override=None))
    await budget.settle(r, 180)
    assert await _counters(tid) == (180, 180)


async def test_settle_twice_changes_nothing_the_second_time(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 500, tenant_override=None))
    await budget.settle(r, 100)
    await budget.settle(r, 400)
    await budget.release(r)
    assert await _counters(tid) == (100, 100)
    assert r.settled


async def test_release_gives_the_whole_reservation_back(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    other = uuid.uuid4()
    _reserved(await budget.reserve(other, 100, tenant_override=None))
    r = _reserved(await budget.reserve(tid, 600, tenant_override=None))
    await budget.release(r)
    assert await _counters(tid) == (0, 100)


@pytest.mark.parametrize("used", [-1, 1.5, True, "3"])
async def test_settle_refuses_a_bad_count(used: Any, budget: TokenBudget) -> None:
    r = _reserved(await budget.reserve(uuid.uuid4(), 10, tenant_override=None))
    with pytest.raises(ValueError):
        await budget.settle(r, used)
    assert not r.settled


async def test_a_correction_never_takes_a_counter_below_zero_and_keeps_a_ttl(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 500, tenant_override=None))
    await _delete_usage_keys()  # Redis lost the counters (restart without persistence)
    await budget.settle(r, 100)
    assert await _counters(tid) == (0, 0)
    for key in (tenant_day_key(tid, DAY), platform_day_key(DAY)):
        assert 0 < await _ttl(key) <= COUNTER_TTL_SECONDS * 1000


# ── the UTC day ──────────────────────────────────────────────────────────────────────────────────────


async def test_a_new_utc_day_starts_at_zero(budget: TokenBudget, clock: DayClock) -> None:
    tid = uuid.uuid4()
    _reserved(await budget.reserve(tid, 1000, tenant_override=None))
    assert isinstance(await budget.reserve(tid, 1, tenant_override=None), BudgetExhausted)
    clock.advance(hours=12)  # 2026-11-26 00:00 UTC
    r = _reserved(await budget.reserve(tid, 1000, tenant_override=None))
    assert r.day == date(2026, 11, 26)
    assert await _counters(tid, date(2026, 11, 26)) == (1000, 1000)
    assert await _counters(tid) == (1000, 1000)


async def test_a_correction_after_midnight_goes_to_the_day_of_the_reservation(budget: TokenBudget, clock: DayClock) -> None:
    clock.now = datetime(2026, 11, 25, 23, 59, 59, tzinfo=UTC)
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 600, tenant_override=None))
    clock.advance(seconds=30)
    await budget.settle(r, 200)
    assert await _counters(tid) == (200, 200)
    assert await _counters(tid, date(2026, 11, 26)) == (None, None)


# ── keys and values ──────────────────────────────────────────────────────────────────────────────────


async def test_tenants_are_counted_apart_and_the_platform_counter_is_shared(budget: TokenBudget) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    _reserved(await budget.reserve(a, 400, tenant_override=None))
    _reserved(await budget.reserve(b, 300, tenant_override=None))
    assert await _counters(a) == (400, 700)
    assert await _counters(b) == (300, 700)
    assert await _usage_keys() == sorted([tenant_day_key(a, DAY), tenant_day_key(b, DAY), platform_day_key(DAY)])


async def test_every_key_and_value_is_a_name_and_a_number(budget: TokenBudget) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 42, tenant_override=None))
    await budget.settle(r, 40)
    r = await _real_get_redis()
    for key in await _usage_keys():
        assert key in (tenant_day_key(tid, DAY), platform_day_key(DAY))
        assert await r.type(key) == "string"
        assert (await r.get(key)).isdigit()


# ── concurrency: forced overlap ──────────────────────────────────────────────────────────────────────


class OverlapGate:
    """
    A Redis stand-in that holds the FIRST call of every task at a barrier until `parties` tasks have each sent
    their first call, then lets all of them go. It records when each task entered and finished, so a test can
    prove that every reservation was in flight before the first one completed: the worst case for any
    read-then-write implementation, made certain instead of left to timing.
    """

    def __init__(self, real: Any, parties: int) -> None:
        self._real = real
        self._barrier = asyncio.Barrier(parties)
        self._seen: set[int] = set()
        self.events: list[tuple[str, int]] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            task = id(asyncio.current_task())
            if task not in self._seen:
                self._seen.add(task)
                self.events.append(("enter", task))
                async with asyncio.timeout(10):
                    await self._barrier.wait()
            return await attr(*args, **kwargs)

        return call

    def done(self) -> None:
        self.events.append(("done", id(asyncio.current_task())))


def assert_all_overlapped(events: list[tuple[str, int]], parties: int) -> None:
    enters = [i for i, (kind, _) in enumerate(events) if kind == "enter"]
    dones = [i for i, (kind, _) in enumerate(events) if kind == "done"]
    assert len(enters) == parties, f"expected {parties} reservations in flight, saw {len(enters)}: {events}"
    assert len({t for kind, t in events if kind == "enter"}) == parties, f"not {parties} separate tasks: {events}"
    assert len(dones) == parties, f"not every reservation finished: {events}"
    assert min(dones) > max(enters), f"a reservation finished before the last one started, no overlap: {events}"


async def _race(budget: TokenBudget, tenants: list[uuid.UUID], amount: int, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    gate = OverlapGate(await _real_get_redis(), len(tenants))

    async def gated() -> Any:
        return gate

    monkeypatch.setattr(redis_client, "get_redis", gated)

    async def one(tid: uuid.UUID) -> Any:
        result = await budget.reserve(tid, amount, tenant_override=None)
        gate.done()
        return result

    results = await asyncio.gather(*(one(t) for t in tenants))
    assert_all_overlapped(gate.events, len(tenants))
    return list(results)


async def test_concurrent_reservations_never_exceed_the_tenant_limit(budget: TokenBudget, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()
    results = await _race(budget, [tid] * 10, 300, monkeypatch)  # 10 x 300 against 1000
    granted = [r for r in results if isinstance(r, Reservation)]
    refused = [r for r in results if isinstance(r, BudgetExhausted)]
    assert (len(granted), len(refused)) == (3, 7)
    assert all(r.scope == "tenant" for r in refused)
    assert await _counters(tid) == (900, 900)


async def test_concurrent_reservations_never_exceed_the_platform_limit(budget: TokenBudget, monkeypatch: pytest.MonkeyPatch) -> None:
    tenants = [uuid.uuid4() for _ in range(8)]
    results = await _race(budget, tenants, 700, monkeypatch)  # 8 tenants x 700 against a platform 3000
    granted = [r for r in results if isinstance(r, Reservation)]
    assert len(granted) == 4
    assert all(r.scope == "platform" for r in results if isinstance(r, BudgetExhausted))
    assert await _get(platform_day_key(DAY)) == 2800
    counted = [await _get(tenant_day_key(t, DAY)) for t in tenants]
    assert sorted(c or 0 for c in counted) == [0, 0, 0, 0, 700, 700, 700, 700], "a refused tenant counter changed"


# ── Redis down ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _down)


@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_outside_development_fails_closed_with_service_unavailable(
    env: str, budget: TokenBudget, redis_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    with pytest.raises(HTTPException) as caught:
        await budget.reserve(uuid.uuid4(), 10, tenant_override=None)
    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "service_unavailable",
        "message": "This service is temporarily unavailable. Please try again in a few minutes.",
    }


async def test_redis_down_in_development_uses_a_per_process_counter_and_says_so(
    budget: TokenBudget, redis_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    tid = uuid.uuid4()
    with capture_logs() as logs:
        r = _reserved(await budget.reserve(tid, 900, tenant_override=None))
        assert r.backend == "memory"
        # The limits still hold in memory.
        assert isinstance(await budget.reserve(tid, 101, tenant_override=None), BudgetExhausted)
        await budget.settle(r, 100)
        _reserved(await budget.reserve(tid, 900, tenant_override=None))
    loud = [e for e in logs if e["event"] == "llm_budget_redis_unavailable_dev_memory"]
    assert loud and all(e["log_level"] == "error" for e in loud)
    assert await _usage_keys() == [], "nothing reached Redis"


@pytest.mark.parametrize("env", ["production", "development"])
async def test_settle_never_raises_when_redis_is_down(env: str, budget: TokenBudget, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = uuid.uuid4()
    r = _reserved(await budget.reserve(tid, 300, tenant_override=None))
    assert r.backend == "redis"
    monkeypatch.setattr(settings, "ENVIRONMENT", env)

    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _down)
    with capture_logs() as logs:
        await budget.settle(r, 50)  # the call already happened: losing a correction must not become an error
    assert "llm_budget_correction_lost" in [e["event"] for e in logs]
    assert r.settled
    # A Redis-held reservation is never corrected in the memory fallback.
    assert budget._mem == {}  # noqa: SLF001
    assert await _counters(tid) == (300, 300), "the over-count stays: fail safe"


async def test_the_default_budget_uses_the_real_clock() -> None:
    assert isinstance(llm_budget.default_budget, TokenBudget)
    day = llm_budget.default_budget.today()
    assert day == datetime.now(UTC).date() or day == (datetime.now(UTC) - timedelta(seconds=5)).date()
