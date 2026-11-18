"""
Step 5a-3: SlotManager.renew and the caller-supplied unavailable message.

Each test runs against Redis (test DB 1) and against the development in-memory fallback.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException

from app.core import redis_client
from app.core import slots as slots_module
from app.core.config import settings
from app.core.slots import (
    DEFAULT_UNAVAILABLE_MESSAGE,
    RenewResult,
    SlotBackendUnavailable,
    SlotLease,
    SlotLimit,
    SlotManager,
    SlotRejected,
)
from app.services import timetable_parser_runner as tpr

OLD_TIMETABLE_MESSAGE = "Timetable import is temporarily unavailable. Please try again in a few minutes."


@pytest.fixture(params=["redis", "memory"])
async def mgr(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SlotManager]:
    if request.param == "redis":
        try:
            r = await redis_client.get_redis()
            await r.ping()
        except Exception:
            pytest.skip("Redis not reachable")
    else:

        async def _down() -> Any:
            raise ConnectionError("redis down")

        monkeypatch.setattr(redis_client, "get_redis", _down)
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    yield SlotManager()


def lim(key: str, limit: int = 1, ttl: float = 30.0) -> SlotLimit:
    return SlotLimit(key=key, limit=limit, ttl_seconds=ttl)


def new_key() -> str:
    return f"test:renew:{uuid.uuid4()}"


async def test_renew_extends_a_live_lease(mgr: SlotManager) -> None:
    key = new_key()
    # Wide margins: a stalled runner (the test pauses for a second) must not turn "renewed in time" into "expired"
    lease = await mgr.acquire([lim(key, ttl=1.5)])
    await asyncio.sleep(0.2)
    assert await mgr.renew(lease, 3.0) is True
    await asyncio.sleep(1.5)  # past the ORIGINAL expiry (1.5 s after the acquire)
    assert await mgr.count(key) == 1
    with pytest.raises(SlotRejected):
        await mgr.acquire([lim(key)])
    await mgr.release(lease)


async def test_renew_on_an_expired_lease_fails_and_does_not_resurrect(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=0.3)])
    await asyncio.sleep(0.6)
    assert await mgr.renew(lease, 5.0) is False
    assert await mgr.count(key) == 0
    # The slot is free for someone else; the old holder cannot take it back
    other = await mgr.acquire([lim(key)])
    assert await mgr.renew(lease, 5.0) is False
    assert await mgr.count(key) == 1
    await mgr.release(other)


async def test_renew_on_a_released_lease_fails(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key)])
    await mgr.release(lease)
    assert await mgr.renew(lease, 5.0) is False
    assert await mgr.count(key) == 0


async def test_renew_is_all_or_nothing_across_keys(mgr: SlotManager) -> None:
    short, long_ = new_key(), new_key()
    lease = await mgr.acquire([lim(long_, ttl=30.0), lim(short, ttl=0.3)])
    await asyncio.sleep(0.6)  # the token on `short` expired, the one on `long_` is still live
    assert await mgr.renew(lease, 30.0) is False
    # `short` was not brought back
    assert await mgr.count(short) == 0


async def test_renew_extends_every_key_of_the_lease(mgr: SlotManager) -> None:
    a, b = new_key(), new_key()
    lease = await mgr.acquire([lim(a, ttl=0.6), lim(b, ttl=0.6)])
    assert await mgr.renew(lease, 3.0) is True
    await asyncio.sleep(0.9)
    assert await mgr.count(a) == 1
    assert await mgr.count(b) == 1
    await mgr.release(lease)


async def test_a_leaked_slot_still_expires_even_after_renewals(mgr: SlotManager) -> None:
    """A crashed worker stops renewing; the last renewal's ttl is the longest the slot can live."""
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=0.4)])
    assert await mgr.renew(lease, 0.5) is True
    with pytest.raises(SlotRejected):
        await mgr.acquire([lim(key, ttl=0.4)])
    await asyncio.sleep(0.9)
    assert await mgr.count(key) == 0
    again = await mgr.acquire([lim(key)])
    await mgr.release(again)


async def test_renew_does_not_shorten_the_key_expiry(mgr: SlotManager) -> None:
    """A short renewal on one lease must not make a neighbour's longer token disappear."""
    key = new_key()
    long_lease = await mgr.acquire([lim(key, limit=2, ttl=5.0)])
    short_lease = await mgr.acquire([lim(key, limit=2, ttl=5.0)])
    assert await mgr.renew(short_lease, 0.3) is True
    await asyncio.sleep(0.6)
    assert await mgr.count(key) == 1  # only the long lease is left
    assert await mgr.renew(long_lease, 5.0) is True
    await mgr.release(long_lease)


# ─── Unavailable message ─────────────────────────────────────────────────────

class _BrokenRedis:
    async def eval(self, *_: Any, **__: Any) -> Any:
        raise ConnectionError("redis went away")

    async def zrem(self, *_: Any, **__: Any) -> Any:
        raise ConnectionError("redis went away")


@pytest.fixture
def redis_down_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _broken() -> Any:
        return _BrokenRedis()

    monkeypatch.setattr(redis_client, "get_redis", _broken)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")


def _message(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    assert exc.value.status_code == 503
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "service_unavailable"
    return str(exc.value.detail["message"])


async def test_default_unavailable_message_is_generic(redis_down_in_production: None) -> None:
    m = SlotManager()
    with pytest.raises(HTTPException) as e1:
        await m.acquire([lim(new_key())])
    with pytest.raises(HTTPException) as e3:
        await m.count(new_key())
    for exc in (e1, e3):
        assert _message(exc) == DEFAULT_UNAVAILABLE_MESSAGE
        assert "imetable" not in _message(exc)


async def test_caller_supplied_message_is_used_by_the_request_path_calls(redis_down_in_production: None) -> None:
    m = SlotManager()
    msg = "Document uploads are paused. Try again soon."
    with pytest.raises(HTTPException) as e1:
        await m.acquire([lim(new_key())], unavailable_message=msg)
    with pytest.raises(HTTPException) as e3:
        await m.count(new_key(), unavailable_message=msg)
    assert [_message(e) for e in (e1, e3)] == [msg, msg]


async def test_timetable_messages_are_unchanged(redis_down_in_production: None) -> None:
    assert tpr.UNAVAILABLE_MESSAGE == OLD_TIMETABLE_MESSAGE
    with pytest.raises(HTTPException) as e1:
        await tpr.check_and_record_rate_limit(uuid.uuid4(), uuid.uuid4())
    assert _message(e1) == OLD_TIMETABLE_MESSAGE
    with pytest.raises(HTTPException) as e2:
        async with tpr.parse_concurrency_guard(uuid.uuid4()):
            pass
    assert _message(e2) == OLD_TIMETABLE_MESSAGE


# ─── renew in a worker: a domain exception, never an HTTP error (review of 5a-3, finding 12) ───

async def test_renew_raises_a_domain_exception_when_redis_is_down(redis_down_in_production: None) -> None:
    m = SlotManager()
    with pytest.raises(SlotBackendUnavailable) as exc:
        await m.renew(SlotLease(token="t", keys=(new_key(),)), 5.0)
    assert not isinstance(exc.value, HTTPException)
    assert not hasattr(exc.value, "status_code")


async def test_renew_with_a_failing_redis_command_raises_the_domain_exception(
    monkeypatch: pytest.MonkeyPatch, redis_down_in_production: None
) -> None:
    class Flaky:
        async def eval(self, *_: Any, **__: Any) -> Any:
            raise TimeoutError("slow")

    async def get() -> Any:
        return Flaky()

    monkeypatch.setattr(redis_client, "get_redis", get)
    with pytest.raises(SlotBackendUnavailable):
        await SlotManager().renew(SlotLease(token="t", keys=(new_key(),)), 5.0)


async def test_the_request_path_still_gets_http_503(redis_down_in_production: None) -> None:
    """Only renew became a domain exception: acquire and count keep translating to 503 for requests."""
    m = SlotManager()
    with pytest.raises(HTTPException) as e:
        await m.acquire([lim(new_key())])
    assert e.value.status_code == 503
    assert not issubclass(SlotBackendUnavailable, HTTPException)


async def test_renew_in_development_still_falls_back_to_memory(mgr: SlotManager) -> None:
    lease = await mgr.acquire([lim(new_key(), ttl=5.0)])
    assert await mgr.renew(lease, 5.0) is True
    await mgr.release(lease)


# ─── Maximum total lease lifetime ────────────────────────────────────────────

async def test_a_lease_has_a_start_time() -> None:
    before = time.monotonic()
    lease = SlotLease(token="t", keys=("k",))
    assert before <= lease.started_at <= time.monotonic()


async def test_a_heartbeat_cannot_renew_past_the_maximum_lifetime(mgr: SlotManager) -> None:
    """A hung worker whose heartbeat keeps running: renew must start returning False at the cap."""
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=2.0)])  # long enough that a slow runner cannot expire it between renewals
    started = time.monotonic()
    renewals = 0
    async with asyncio.timeout(8):  # without the cap this loop would never end
        while await mgr.renew(lease, 2.0, max_total_seconds=1.0):
            renewals += 1
            await asyncio.sleep(0.15)
    elapsed = time.monotonic() - started
    assert renewals >= 4, renewals
    assert 0.9 <= elapsed <= 4.0, elapsed  # the cap (1 s) ended it; the upper bound only rules out "never"
    # Nothing keeps the slot after the cap: it expires on its own within the last ttl
    await asyncio.sleep(0.7)
    assert await mgr.count(key) == 0


async def test_the_ttl_is_cut_to_the_time_left_before_the_cap(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=30.0)])
    assert await mgr.renew(lease, 100.0, max_total_seconds=1.0) is True  # would live 100 s without the cap
    await asyncio.sleep(1.4)
    assert await mgr.count(key) == 0, "the lease outlived its maximum lifetime"


async def test_renew_without_a_maximum_is_unchanged(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=2.0)])
    for _ in range(4):
        assert await mgr.renew(lease, 2.0) is True
        await asyncio.sleep(0.2)
    await mgr.release(lease)


async def test_a_cap_already_reached_returns_false_and_changes_nothing(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=0.6)])
    await asyncio.sleep(0.3)
    assert await mgr.renew(lease, 60.0, max_total_seconds=0.2) is False
    await asyncio.sleep(0.5)
    assert await mgr.count(key) == 0  # still expired at its ORIGINAL time, not extended


# ─── Review of 5b, finding 3: a lease remembers which backend holds its token ───

@pytest.fixture
async def real_redis() -> Any:
    try:
        r = await redis_client.get_redis()
        await r.ping()
    except Exception:
        pytest.skip("Redis not reachable")
    return r


async def test_a_redis_held_lease_is_never_renewed_from_memory_when_redis_fails_in_development(
    real_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    m = SlotManager()
    key = new_key()
    lease = await m.acquire([lim(key, ttl=30.0)])

    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", down)
    with pytest.raises(SlotBackendUnavailable):  # not False: the token is still alive in Redis
        await m.renew(lease, 30.0)

    async def back() -> Any:
        return real_redis

    monkeypatch.setattr(redis_client, "get_redis", back)
    assert await m.renew(lease, 30.0) is True  # Redis recovered and the lease was never lost
    await m.release(lease)


async def test_a_slow_redis_call_is_unavailability_not_a_lost_lease(
    real_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renewal slower than REDIS_OP_TIMEOUT (development mode, Redis-held lease) raises, it does not fall back."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(slots_module, "REDIS_OP_TIMEOUT", 0.05)
    m = SlotManager()
    lease = await m.acquire([lim(new_key(), ttl=30.0)])

    class Slow:
        async def eval(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(30)  # cancelled by the 0.05 s timeout long before it ends

    async def get_slow() -> Any:
        return Slow()

    monkeypatch.setattr(redis_client, "get_redis", get_slow)
    with pytest.raises(SlotBackendUnavailable):
        await m.renew(lease, 30.0)


async def test_a_lease_records_the_backend_that_holds_it(real_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    m = SlotManager()
    in_redis = await m.acquire([lim(new_key())])
    assert in_redis.backend == "redis"
    await m.release(in_redis)

    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", down)
    in_memory = await m.acquire([lim(new_key())])
    assert in_memory.backend == "memory"


async def test_a_memory_lease_is_renewed_in_memory_and_never_asks_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", down)
    m = SlotManager()
    lease = await m.acquire([lim(new_key(), ttl=30.0)])

    class MustNotBeUsed:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("a memory lease must not be renewed in Redis")

    async def recovered() -> Any:
        return MustNotBeUsed()

    monkeypatch.setattr(redis_client, "get_redis", recovered)  # Redis is back, but never saw this token
    assert await m.renew(lease, 30.0) is True
    await m.release(lease)


# ─── Review of 5b, finding 5: renew says WHY it refused ──────────────────────

async def test_renew_lease_reports_renewed(mgr: SlotManager) -> None:
    lease = await mgr.acquire([lim(new_key(), ttl=30.0)])
    assert await mgr.renew_lease(lease, 30.0) is RenewResult.RENEWED
    assert await mgr.renew_lease(lease, 30.0, max_total_seconds=3600.0) is RenewResult.RENEWED
    await mgr.release(lease)


async def test_renew_lease_reports_a_lost_lease(mgr: SlotManager) -> None:
    lease = await mgr.acquire([lim(new_key(), ttl=30.0)])
    await mgr.release(lease)
    assert await mgr.renew_lease(lease, 30.0) is RenewResult.LEASE_LOST
    assert await mgr.renew_lease(lease, 30.0, max_total_seconds=3600.0) is RenewResult.LEASE_LOST


async def test_renew_lease_reports_the_maximum_lifetime_and_leaves_the_lease_alone(mgr: SlotManager) -> None:
    key = new_key()
    lease = await mgr.acquire([lim(key, ttl=30.0)])
    assert await mgr.renew_lease(lease, 30.0, max_total_seconds=0.0) is RenewResult.MAX_LIFETIME
    assert await mgr.count(key) == 1  # refused, not released: the holder decides what to do
    await mgr.release(lease)


async def test_the_maximum_lifetime_is_reported_before_redis_is_asked(redis_down_in_production: None) -> None:
    result = await SlotManager().renew_lease(SlotLease(token="t", keys=(new_key(),)), 5.0, max_total_seconds=0.0)
    assert result is RenewResult.MAX_LIFETIME


async def test_renew_lease_raises_when_the_backend_is_down(redis_down_in_production: None) -> None:
    with pytest.raises(SlotBackendUnavailable):
        await SlotManager().renew_lease(SlotLease(token="t", keys=(new_key(),)), 5.0)


async def test_renew_is_renew_lease_reduced_to_a_bool(mgr: SlotManager) -> None:
    lease = await mgr.acquire([lim(new_key(), ttl=30.0)])
    assert await mgr.renew(lease, 30.0, max_total_seconds=0.0) is False  # max lifetime
    assert await mgr.renew(lease, 30.0) is True
    await mgr.release(lease)
    assert await mgr.renew(lease, 30.0) is False  # lost
