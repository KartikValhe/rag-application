"""
Step 6a-10b (docs/phase6-plan.md 6a-10b): the token-weighted tokens-per-minute window, on the test Redis (DB 1).

  llm:tpm:{model_key}   a sorted set; member `<token>:<weight>`, score = expiry in ms (reserved at + 60 s)

One Lua script checks the window and adds the reservation in a single Redis call, so two reservations can never
both take the last tokens. settle() corrects a reservation to the reported usage with one more script; the
weight is never below zero and an entry that already left the window is not brought back. A full window is a
returned TpmRejected with a retry-after (never a sleep); a request that is heavier than the whole limit is
refused as "oversize" without a Redis call.

The clock is a fake one that tests move by hand: nothing here waits. The concurrency tests force the overlap with
a barrier in front of the first window command of every task, and assert that every task was in flight before
the first one finished.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.core import redis_client
from app.core.config import settings
from app.llm.tpm import TPM_WINDOW_S, TpmRejected, TpmReservation, TpmWindow, tpm_key
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys, llm_keys  # noqa: F401  (pytest fixture)
from tests.fixtures.llm_tpm import SpyRedis, assert_all_overlapped

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

# Kept at import: a test that makes Redis "go down" patches redis_client.get_redis; checks must still reach the real one.
_real_get_redis = redis_client.get_redis

LIMIT = 8000


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def window(clock: FakeClock) -> TpmWindow:
    return TpmWindow(now_ms=clock.now_ms)


@pytest.fixture
def model() -> str:
    return f"groq:m-{uuid.uuid4().hex[:10]}"


async def members(model_key: str) -> list[tuple[str, int, int]]:
    """(token, weight, expiry ms) of every entry, read straight from Redis (not through the code under test)."""
    r = await _real_get_redis()
    rows = await r.zrange(tpm_key(model_key), 0, -1, withscores=True)
    out = []
    for member, score in rows:
        token, weight = member.rsplit(":", 1)
        out.append((token, int(weight), int(score)))
    return out


async def total(model_key: str) -> int:
    return sum(w for _, w, _ in await members(model_key))


async def reserved(window: TpmWindow, model_key: str, weight: int, limit: int = LIMIT) -> TpmReservation:
    result = await window.reserve(model_key, limit, weight)
    assert isinstance(result, TpmReservation), result
    return result


# ── reserve ──────────────────────────────────────────────────────────────────────────────────────────


async def test_reserve_takes_its_weight_and_the_key_gets_a_ttl(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 1234)
    assert (res.model_key, res.weight) == (model, 1234)
    assert await total(model) == 1234
    r = await _real_get_redis()
    assert await r.type(tpm_key(model)) == "zset"
    assert 0 < await r.pttl(tpm_key(model)) <= int(TPM_WINDOW_S * 1000)


async def test_the_key_is_per_model_and_holds_no_text(window: TpmWindow, model: str) -> None:
    await reserved(window, model, 500)
    assert await llm_keys() == [f"llm:tpm:{model}"]
    ((token, weight, _),) = await members(model)
    assert re.fullmatch(r"[0-9a-f]{32}", token) and weight == 500


async def test_a_reservation_that_fits_exactly_is_taken_and_one_token_more_is_not(window: TpmWindow, model: str) -> None:
    await reserved(window, model, 5000)
    await reserved(window, model, 3000)  # 8000 of 8000
    refused = await window.reserve(model, LIMIT, 1)
    assert isinstance(refused, TpmRejected) and refused.reason == "full"
    assert await total(model) == 8000


async def test_a_full_window_is_a_returned_value_that_consumes_nothing(window: TpmWindow, model: str) -> None:
    await reserved(window, model, 6000)
    before = await members(model)
    refused = await window.reserve(model, LIMIT, 3000)
    assert refused == TpmRejected(reason="full", limit=LIMIT, used=6000, requested=3000, retry_after_seconds=60)
    assert await members(model) == before


async def test_a_request_as_heavy_as_the_whole_limit_fits_an_empty_window(window: TpmWindow, model: str) -> None:
    await reserved(window, model, LIMIT)
    assert await total(model) == LIMIT


async def test_the_limit_is_the_callers_not_a_constant(window: TpmWindow, model: str) -> None:
    await reserved(window, model, 900, limit=1000)
    refused = await window.reserve(model, 1000, 200)
    assert isinstance(refused, TpmRejected) and refused.limit == 1000
    again = await window.reserve(model, 100_000, 200)  # the same model, a bigger limit: it fits
    assert isinstance(again, TpmReservation)


async def test_each_model_has_its_own_window(window: TpmWindow, model: str) -> None:
    other = f"openrouter:o-{uuid.uuid4().hex[:10]}"
    await reserved(window, model, LIMIT)
    assert isinstance(await window.reserve(model, LIMIT, 1), TpmRejected)
    await reserved(window, other, LIMIT)  # not affected by the first one
    assert await total(other) == LIMIT and await total(model) == LIMIT


# ── the window slides ────────────────────────────────────────────────────────────────────────────────


async def test_an_entry_counts_until_exactly_sixty_seconds_after_it_was_taken(
    clock: FakeClock, window: TpmWindow, model: str
) -> None:
    await reserved(window, model, LIMIT)
    clock.advance(59.999)
    assert isinstance(await window.reserve(model, LIMIT, 1), TpmRejected)
    clock.advance(0.001)  # 60 s
    await reserved(window, model, LIMIT)
    assert await total(model) == LIMIT  # the old entry was dropped, the new one counts


async def test_the_window_is_sliding_not_a_fixed_minute(clock: FakeClock, window: TpmWindow, model: str) -> None:
    await reserved(window, model, 4000)
    clock.advance(30)
    await reserved(window, model, 4000)
    clock.advance(31)  # the first is out (61 s), the second is 31 s old
    assert await total(model) == 8000  # not purged yet: purging happens on the next reserve
    await reserved(window, model, 4000)
    assert await total(model) == 8000  # 4000 (31 s old) + 4000 (new)


async def test_the_retry_after_is_when_enough_weight_has_left_the_window(
    clock: FakeClock, window: TpmWindow, model: str
) -> None:
    await reserved(window, model, 3000)  # leaves at +60
    clock.advance(10)
    await reserved(window, model, 5000)  # leaves at +70
    clock.advance(10)  # now +20: used 8000
    small = await window.reserve(model, LIMIT, 3000)  # fits once the first (3000) is gone: at +60, 40 s from now
    assert isinstance(small, TpmRejected) and small.retry_after_seconds == 40
    big = await window.reserve(model, LIMIT, 4000)  # needs both gone: at +70, 50 s from now
    assert isinstance(big, TpmRejected) and big.retry_after_seconds == 50
    clock.advance(40)
    assert isinstance(await window.reserve(model, LIMIT, 3000), TpmReservation)


async def test_the_retry_after_rounds_up_to_whole_seconds(clock: FakeClock, window: TpmWindow, model: str) -> None:
    await reserved(window, model, LIMIT)
    clock.advance(0.2)
    refused = await window.reserve(model, LIMIT, 1)
    assert isinstance(refused, TpmRejected) and refused.retry_after_seconds == 60  # 59.8 s


async def test_entries_expire_by_themselves(window: TpmWindow, model: str) -> None:
    # A crash never settles its reservation. It must not stay in Redis for good: the key has a ttl at most one window.
    await reserved(window, model, 1000)
    r = await _real_get_redis()
    assert 0 < await r.pttl(tpm_key(model)) <= 60_000


# ── oversize ─────────────────────────────────────────────────────────────────────────────────────────


async def test_a_request_above_the_whole_limit_is_oversize_at_once_without_a_redis_call(
    window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SpyRedis(await _real_get_redis())

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)
    refused = await window.reserve(model, LIMIT, LIMIT + 1)
    assert refused == TpmRejected(reason="oversize", limit=LIMIT, used=0, requested=LIMIT + 1, retry_after_seconds=None)
    assert spy.calls == []  # no wait, no retry-after to wait for, not even a command


async def test_oversize_does_not_depend_on_how_full_the_window_is(window: TpmWindow, model: str) -> None:
    await reserved(window, model, 100)
    refused = await window.reserve(model, LIMIT, LIMIT + 5)
    assert isinstance(refused, TpmRejected) and refused.reason == "oversize"
    assert await total(model) == 100


# ── one atomic call ──────────────────────────────────────────────────────────────────────────────────


async def test_a_reservation_is_one_redis_command_and_a_correction_is_one(
    window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SpyRedis(await _real_get_redis())

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)
    res = await reserved(window, model, 1000)
    assert [name for name, _ in spy.tpm_calls()] == ["eval"]
    await window.settle(res, 400)
    assert [name for name, _ in spy.tpm_calls()] == ["eval", "eval"]
    assert [name for name, _ in spy.calls] == ["eval", "eval"]  # nothing else was sent


async def test_a_refused_reservation_is_one_command_too(window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await reserved(window, model, LIMIT)
    spy = SpyRedis(await _real_get_redis())

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)
    assert isinstance(await window.reserve(model, LIMIT, 10), TpmRejected)
    assert [name for name, _ in spy.calls] == ["eval"]


async def _race_reservations(
    window: TpmWindow, model: str, parties: int, weight: int, monkeypatch: pytest.MonkeyPatch
) -> list[Any]:
    spy = SpyRedis(await _real_get_redis(), parties)

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)

    async def one() -> Any:
        result = await window.reserve(model, LIMIT, weight)
        spy.done()
        return result

    results = await asyncio.gather(*(one() for _ in range(parties)))
    assert_all_overlapped(spy.events, parties)
    return list(results)


async def test_concurrent_reservations_never_exceed_the_limit(
    window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _race_reservations(window, model, 12, 1000, monkeypatch)  # 12 x 1000 against 8000
    granted = [r for r in results if isinstance(r, TpmReservation)]
    refused = [r for r in results if isinstance(r, TpmRejected)]
    assert (len(granted), len(refused)) == (8, 4)
    assert await total(model) == 8000
    assert len({r.token for r in granted}) == 8


async def test_concurrent_reservations_of_different_weights_never_exceed_the_limit(
    window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _race_reservations(window, model, 5, 3000, monkeypatch)  # 5 x 3000 against 8000: two fit
    assert sum(isinstance(r, TpmReservation) for r in results) == 2
    assert await total(model) == 6000


async def test_concurrent_corrections_lose_no_update(window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    held = [await reserved(window, model, 1000) for _ in range(6)]
    spy = SpyRedis(await _real_get_redis(), len(held))

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)

    async def one(res: TpmReservation, used: int) -> None:
        await window.settle(res, used)
        spy.done()

    await asyncio.gather(*(one(res, 100 * (i + 1)) for i, res in enumerate(held)))
    assert_all_overlapped(spy.events, len(held))
    assert sorted(w for _, w, _ in await members(model)) == [100, 200, 300, 400, 500, 600]


# ── settle ───────────────────────────────────────────────────────────────────────────────────────────


async def test_settle_refunds_when_the_provider_reports_less(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    await window.settle(res, 1200)
    assert await total(model) == 1200
    assert len(await members(model)) == 1  # corrected in place, not a second entry


async def test_settle_adds_the_overage_when_the_provider_reports_more(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    await window.settle(res, 7500)
    assert await total(model) == 7500
    res2 = await reserved(window, model, 400)  # 7900
    await window.settle(res2, 2000)  # an overrun may take the window above the limit: it already happened
    assert await total(model) == 9500
    refused = await window.reserve(model, LIMIT, 1)
    assert isinstance(refused, TpmRejected) and refused.used == 9500


async def test_settle_keeps_the_original_expiry(clock: FakeClock, window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    ((_, _, before),) = await members(model)
    clock.advance(20)
    await window.settle(res, 1000)
    ((_, _, after),) = await members(model)
    assert after == before  # a correction never moves the entry later: it leaves with the window it was taken in


async def test_settle_to_zero_removes_the_entry(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    await window.settle(res, 0)
    assert await members(model) == []


async def test_release_gives_the_whole_reservation_back(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    await window.release(res)
    assert await total(model) == 0
    await reserved(window, model, LIMIT)  # all of it is free again


async def test_a_reservation_is_settled_at_most_once(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 3000)
    await window.settle(res, 1000)
    await window.settle(res, 9000)
    await window.release(res)
    assert await total(model) == 1000


async def test_settle_with_the_same_amount_sends_nothing(window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    res = await reserved(window, model, 3000)
    spy = SpyRedis(await _real_get_redis())

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)
    await window.settle(res, 3000)
    assert spy.calls == []


async def test_a_correction_never_takes_an_entry_below_zero(window: TpmWindow, model: str) -> None:
    res = await reserved(window, model, 100)
    other = await reserved(window, model, 700)
    res.weight = 5000  # the reservation believes it holds more than Redis does: the refund is bigger than the entry
    await window.settle(res, 0)
    assert await total(model) == 700  # nothing negative, the other entry is untouched
    assert all(w >= 0 for _, w, _ in await members(model))
    await window.settle(other, 700)


async def test_an_entry_that_already_left_the_window_is_not_brought_back(
    clock: FakeClock, window: TpmWindow, model: str
) -> None:
    res = await reserved(window, model, 3000)
    clock.advance(61)
    await window.settle(res, 9000)  # more than reserved, but the entry is gone: nothing is added
    assert await total(model) == 0
    await window.release(res)
    assert await total(model) == 0


async def test_settle_never_raises_for_redis_and_the_entry_expires_by_itself(
    window: TpmWindow, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = await reserved(window, model, 3000)

    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", down)
    await window.settle(res, 100)  # no exception, in any environment
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    await window.release(res)
    monkeypatch.setattr(redis_client, "get_redis", _real_get_redis)
    assert await total(model) == 3000  # still held in full: the over-count, the safe side, until it expires
    assert 0 < await (await _real_get_redis()).pttl(tpm_key(model)) <= 60_000


@pytest.mark.parametrize("used", [-1, 1.5, True, "5", None])
async def test_settle_refuses_a_count_that_is_not_a_whole_number_of_zero_or_more(window: TpmWindow, model: str, used: Any) -> None:
    res = await reserved(window, model, 100)
    with pytest.raises(ValueError):
        await window.settle(res, used)


@pytest.mark.parametrize(("limit", "weight"), [(0, 5), (-1, 5), (5, 0), (5, -1), (1.5, 5), (5, 2.5), (True, 5), (5, True), ("5", 5)])
async def test_reserve_refuses_a_limit_or_weight_that_is_not_a_positive_whole_number(
    window: TpmWindow, model: str, limit: Any, weight: Any
) -> None:
    with pytest.raises(ValueError):
        await window.reserve(model, limit, weight)
    assert await llm_keys() == []


# ── Redis down: the same policy as SlotManager ───────────────────────────────────────────────────────


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _down)


@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_outside_development_is_the_503_service_unavailable(
    env: str, redis_down: None, monkeypatch: pytest.MonkeyPatch, window: TpmWindow, model: str
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    with pytest.raises(HTTPException) as caught:
        await window.reserve(model, LIMIT, 100)
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "service_unavailable"  # type: ignore[index]


async def test_redis_down_in_development_uses_a_per_process_window_with_the_same_rules(
    redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, window: TpmWindow, model: str
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    with capture_logs() as logs:
        first = await window.reserve(model, LIMIT, 5000)
    assert isinstance(first, TpmReservation) and first.backend == "memory"
    assert [e["log_level"] for e in logs if e["event"] == "llm_tpm_redis_unavailable_dev_memory"] == ["error"]
    refused = await window.reserve(model, LIMIT, 4000)
    assert isinstance(refused, TpmRejected) and refused.used == 5000 and refused.retry_after_seconds == 60
    await window.settle(first, 1000)  # refund, in memory
    assert isinstance(await window.reserve(model, LIMIT, 7000), TpmReservation)
    clock.advance(60)
    assert isinstance(await window.reserve(model, LIMIT, LIMIT), TpmReservation)  # everything expired


async def test_a_reservation_taken_in_redis_is_never_settled_in_memory(
    monkeypatch: pytest.MonkeyPatch, window: TpmWindow, model: str
) -> None:
    res = await reserved(window, model, 3000)
    assert res.backend == "redis"

    async def down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", down)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    await window.settle(res, 0)
    fresh = await window.reserve(model, LIMIT, LIMIT)  # the memory window knows nothing of the Redis entry
    assert isinstance(fresh, TpmReservation) and fresh.backend == "memory"


async def test_the_default_clock_is_the_wall_clock(model: str) -> None:
    window = TpmWindow()
    res = await reserved(window, model, 100)
    ((_, _, expiry),) = await members(model)
    assert abs(expiry - (time.time() + TPM_WINDOW_S) * 1000) < 5000
    await window.release(res)
