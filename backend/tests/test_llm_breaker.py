"""
Step 6a-4 (docs/phase6-plan.md 7.4): the provider breaker and the model cooldowns, on the test Redis (DB 1).

Breaker llm:cb:{provider}: 5 failures in 60 s open it for 60 s; then ONE half-open probe is let through. The
probe's result closes it (the provider answered) or opens it again for 60 s. An auth error opens it at once.
Cooldown llm:cooldown:{model_key}: a 429 makes the chain skip that model for Retry-After.

Time comes from a fake clock passed to the scripts, so nothing here waits.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.core import redis_client
from app.core.config import settings
from app.llm.breaker import Admission, Breaker, BreakerConfig, Cooldowns, breaker_key, cooldown_key
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys, llm_keys  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

P = "groq"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> Breaker:
    return Breaker(now_ms=clock.now_ms)


async def fail_times(breaker: Breaker, n: int, clock: FakeClock | None = None, step_s: float = 0.0) -> None:
    for _ in range(n):
        permit = await breaker.admit(P)
        await breaker.record_failure(P, permit)
        if clock is not None:
            clock.advance(step_s)


async def open_it(breaker: Breaker) -> None:
    await fail_times(breaker, 5)
    assert (await breaker.admit(P)).state is Admission.OPEN


# ── the defaults are the plan's ──────────────────────────────────────────────────────────────────────


def test_the_defaults_are_the_plan_values() -> None:
    c = BreakerConfig()
    assert (c.failures, c.window_s, c.open_s) == (5, 60.0, 60.0)


def test_the_keys_hold_the_provider_or_model_key_and_nothing_else() -> None:
    assert breaker_key("groq") == "llm:cb:groq"
    assert cooldown_key("groq:openai/gpt-oss-120b") == "llm:cooldown:groq:openai/gpt-oss-120b"


# ── opening ──────────────────────────────────────────────────────────────────────────────────────────


async def test_four_failures_leave_it_closed_the_fifth_opens_it(breaker: Breaker) -> None:
    await fail_times(breaker, 4)
    assert (await breaker.admit(P)).state is Admission.CLOSED
    await fail_times(breaker, 1)
    assert (await breaker.admit(P)).state is Admission.OPEN
    assert await breaker.state(P) == "open"


async def test_failures_spread_over_more_than_60_s_never_open_it(breaker: Breaker, clock: FakeClock) -> None:
    await fail_times(breaker, 12, clock, step_s=15.0)  # never 5 inside one 60 s window
    assert (await breaker.admit(P)).state is Admission.CLOSED


async def test_five_failures_inside_60_s_open_it_even_after_older_ones_expired(breaker: Breaker, clock: FakeClock) -> None:
    await fail_times(breaker, 4)
    clock.advance(60.0)  # those four are now exactly 60 s old: outside the window
    await fail_times(breaker, 4, clock, step_s=1.0)
    assert (await breaker.admit(P)).state is Admission.CLOSED
    await fail_times(breaker, 1)
    assert (await breaker.admit(P)).state is Admission.OPEN


async def test_successes_do_not_reset_the_window(breaker: Breaker) -> None:
    # The plan counts failures in a window, not consecutive failures
    for _ in range(5):
        permit = await breaker.admit(P)
        await breaker.record_failure(P, permit)
        ok = await breaker.admit(P)
        if ok.state is Admission.OPEN:
            break
        await breaker.record_success(P, ok)
    assert (await breaker.admit(P)).state is Admission.OPEN


async def test_each_provider_has_its_own_breaker(breaker: Breaker) -> None:
    await open_it(breaker)
    assert (await breaker.admit("openrouter")).state is Admission.CLOSED


async def test_it_stays_open_for_60_s(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(59.999)
    assert (await breaker.admit(P)).state is Admission.OPEN
    clock.advance(0.001)
    assert (await breaker.admit(P)).state is Admission.PROBE


async def test_failures_while_open_do_not_extend_it(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(30.0)
    # a request that started before the breaker opened fails now: it has no probe permit
    await breaker.record_failure(P, Breaker.untracked())
    clock.advance(30.0)
    assert (await breaker.admit(P)).state is Admission.PROBE


async def test_an_auth_error_opens_it_at_once(breaker: Breaker, clock: FakeClock) -> None:
    await breaker.trip(P)
    assert (await breaker.admit(P)).state is Admission.OPEN
    clock.advance(60.0)
    assert (await breaker.admit(P)).state is Admission.PROBE


# ── half-open: exactly one probe ─────────────────────────────────────────────────────────────────────


async def test_half_open_lets_exactly_one_of_ten_simultaneous_callers_through(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    permits = await asyncio.gather(*(breaker.admit(P) for _ in range(10)))
    states = [p.state for p in permits]
    assert states.count(Admission.PROBE) == 1
    assert states.count(Admission.OPEN) == 9
    assert states.count(Admission.CLOSED) == 0


async def test_a_successful_probe_closes_it(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    probe = await breaker.admit(P)
    assert probe.state is Admission.PROBE
    await breaker.record_success(P, probe)
    assert await breaker.state(P) == "closed"
    assert [(await breaker.admit(P)).state for _ in range(3)] == [Admission.CLOSED] * 3
    await fail_times(breaker, 4)  # the old failures were cleared: four new ones do not open it
    assert (await breaker.admit(P)).state is Admission.CLOSED


async def test_a_failed_probe_opens_it_again_for_60_s(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    probe = await breaker.admit(P)
    await breaker.record_failure(P, probe)
    assert await breaker.state(P) == "open"  # open again, not "half-open with a probe still in flight"
    assert (await breaker.admit(P)).state is Admission.OPEN
    clock.advance(59.0)
    assert (await breaker.admit(P)).state is Admission.OPEN
    clock.advance(1.0)
    assert (await breaker.admit(P)).state is Admission.PROBE


async def test_an_auth_error_on_the_probe_opens_it_again(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    assert (await breaker.admit(P)).state is Admission.PROBE
    await breaker.trip(P)
    assert (await breaker.admit(P)).state is Admission.OPEN


async def test_a_released_probe_lets_the_next_caller_probe(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    probe = await breaker.admit(P)
    assert (await breaker.admit(P)).state is Admission.OPEN  # probe in flight
    await breaker.release(P, probe)  # the probe ended with no result (the student left)
    assert (await breaker.admit(P)).state is Admission.PROBE


async def test_a_probe_that_never_reports_back_is_replaced_after_its_lease(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    stale = await breaker.admit(P)
    clock.advance(BreakerConfig().probe_ttl_s - 0.001)
    assert (await breaker.admit(P)).state is Admission.OPEN
    clock.advance(0.001)
    fresh = await breaker.admit(P)
    assert fresh.state is Admission.PROBE
    # the stale probe reporting late does not decide for the fresh one
    await breaker.record_success(P, stale)
    assert await breaker.state(P) == "half_open"
    await breaker.record_success(P, fresh)
    assert await breaker.state(P) == "closed"


async def test_a_closed_success_or_an_untracked_permit_changes_nothing(breaker: Breaker) -> None:
    await breaker.record_success(P, Breaker.untracked())
    await breaker.release(P, Breaker.untracked())
    assert await breaker.state(P) == "closed"


async def test_only_the_breaker_keys_of_the_provider_are_written(breaker: Breaker, clock: FakeClock) -> None:
    await open_it(breaker)
    clock.advance(60.0)
    await breaker.admit(P)
    assert set(await llm_keys()) <= {"llm:cb:groq", "llm:cb:groq:fails"}
    r = await redis_client.get_redis()
    assert await r.pttl("llm:cb:groq") > 0  # nothing lives in Redis forever


# ── cooldowns ────────────────────────────────────────────────────────────────────────────────────────

M = "groq:some-model"


async def test_a_cooldown_lasts_exactly_retry_after(clock: FakeClock) -> None:
    cd = Cooldowns(now_ms=clock.now_ms)
    assert await cd.remaining_ms(M) == 0
    await cd.start(M, 30.0)
    assert await cd.remaining_ms(M) == 30_000
    clock.advance(29.999)
    assert await cd.remaining_ms(M) == 1
    clock.advance(0.001)
    assert await cd.remaining_ms(M) == 0
    r = await redis_client.get_redis()
    assert 0 < await r.pttl(cooldown_key(M)) <= 30_000


async def test_a_shorter_cooldown_never_shortens_a_longer_one(clock: FakeClock) -> None:
    cd = Cooldowns(now_ms=clock.now_ms)
    await cd.start(M, 30.0)
    await cd.start(M, 2.0)
    assert await cd.remaining_ms(M) == 30_000
    await cd.start(M, 40.0)
    assert await cd.remaining_ms(M) == 40_000


async def test_a_zero_cooldown_sets_nothing(clock: FakeClock) -> None:
    cd = Cooldowns(now_ms=clock.now_ms)
    await cd.start(M, 0.0)
    assert await cd.remaining_ms(M) == 0
    assert await llm_keys() == []


async def test_cooldowns_are_per_model(clock: FakeClock) -> None:
    cd = Cooldowns(now_ms=clock.now_ms)
    await cd.start(M, 10.0)
    assert await cd.remaining_ms("groq:other-model") == 0


# ── Redis down ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _down)


@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_outside_development_fails_closed_with_service_unavailable(
    env: str, redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    with pytest.raises(HTTPException) as caught:
        await Breaker(now_ms=clock.now_ms).admit(P)
    assert caught.value.status_code == 503 and caught.value.detail["code"] == "service_unavailable"  # type: ignore[index]
    with pytest.raises(HTTPException) as caught:
        await Cooldowns(now_ms=clock.now_ms).remaining_ms(M)
    assert caught.value.status_code == 503 and caught.value.detail["code"] == "service_unavailable"  # type: ignore[index]


async def test_redis_down_in_development_admits_and_logs(redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    with capture_logs() as logs:
        assert (await Breaker(now_ms=clock.now_ms).admit(P)).state is Admission.CLOSED
        assert await Cooldowns(now_ms=clock.now_ms).remaining_ms(M) == 0
    loud = [e for e in logs if e["event"] == "llm_gate_redis_unavailable_dev_allowing"]
    assert [e["check"] for e in loud] == ["breaker", "cooldown"]
    assert all(e["log_level"] == "error" for e in loud)


@pytest.mark.parametrize("env", ["production", "development"])
async def test_recording_never_raises_when_redis_is_down(
    env: str, redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    # the call already happened: losing one failure count must not turn an answer into an error
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    b = Breaker(now_ms=clock.now_ms)
    await b.record_failure(P, Breaker.untracked())
    await b.record_success(P, Breaker.untracked())
    await b.release(P, Breaker.untracked())
    await b.trip(P)
    await Cooldowns(now_ms=clock.now_ms).start(M, 5.0)
