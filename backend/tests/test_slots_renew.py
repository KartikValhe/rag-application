"""
Step 5a-3: SlotManager.renew and the caller-supplied unavailable message.

Each test runs against Redis (test DB 1) and against the development in-memory fallback.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException

from app.core import redis_client
from app.core.config import settings
from app.core.slots import (
    DEFAULT_UNAVAILABLE_MESSAGE,
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
    lease = await mgr.acquire([lim(key, ttl=0.6)])
    await asyncio.sleep(0.3)
    assert await mgr.renew(lease, 2.0) is True
    await asyncio.sleep(0.6)  # past the ORIGINAL expiry
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
    with pytest.raises(HTTPException) as e2:
        await m.renew(SlotLease(token="t", keys=(new_key(),)), 5.0)
    with pytest.raises(HTTPException) as e3:
        await m.count(new_key())
    for exc in (e1, e2, e3):
        assert _message(exc) == DEFAULT_UNAVAILABLE_MESSAGE
        assert "imetable" not in _message(exc)


async def test_caller_supplied_message_is_used_by_every_call(redis_down_in_production: None) -> None:
    m = SlotManager()
    msg = "Document uploads are paused. Try again soon."
    with pytest.raises(HTTPException) as e1:
        await m.acquire([lim(new_key())], unavailable_message=msg)
    with pytest.raises(HTTPException) as e2:
        await m.renew(SlotLease(token="t", keys=(new_key(),)), 5.0, unavailable_message=msg)
    with pytest.raises(HTTPException) as e3:
        await m.count(new_key(), unavailable_message=msg)
    assert [_message(e) for e in (e1, e2, e3)] == [msg, msg, msg]


async def test_timetable_messages_are_unchanged(redis_down_in_production: None) -> None:
    assert tpr.UNAVAILABLE_MESSAGE == OLD_TIMETABLE_MESSAGE
    with pytest.raises(HTTPException) as e1:
        await tpr.check_and_record_rate_limit(uuid.uuid4(), uuid.uuid4())
    assert _message(e1) == OLD_TIMETABLE_MESSAGE
    with pytest.raises(HTTPException) as e2:
        async with tpr.parse_concurrency_guard(uuid.uuid4()):
            pass
    assert _message(e2) == OLD_TIMETABLE_MESSAGE
