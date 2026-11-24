"""
Step 6a-4 (docs/phase6-plan.md 7.1, 7.4, 7.7, 11.1): the chain, `answer_stream(purpose, req, tenant_ctx)`.

Rules under test (plan 7.4 and the owner's decisions for 6a-4):
  * retry at most once on the same model, ONLY before the first token: full jitter 0.5-2 s, or Retry-After when
    it is 5 s or less;
  * fall back to the next key on LLMRateLimited, LLMUnavailable, LLMTimeout, an open breaker, a cooldown, a full
    RPM/RPD slot; NEVER on LLMBadRequest, LLMContentBlocked, LLMAuthError (an auth error opens the breaker);
  * a failure after the first token is neither retried nor failed over (LLMStreamInterrupted);
  * provider RPM/RPD through SlotManager in one atomic acquire: a rejection consumes nothing on the other key;
  * the four timeouts; the whole-answer total spans every attempt;
  * an empty condense chain means "no condense", not an error;
  * Redis down outside development fails closed with 503 service_unavailable;
  * no prompt or answer text in Redis or in the logs, no tenant text in a key.

Every provider is a FakeLLM; time is a fake clock whose sleep returns at once. Redis is the test Redis (DB 1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.core import redis_client
from app.core.config import Settings, settings
from app.core.slots import SlotManager
from app.llm import chain as chain_module
from app.llm.breaker import Admission, Breaker, Cooldowns
from app.llm.chain import (
    RETRY_AFTER_MAX_S,
    LLMChain,
    LLMChainExhausted,
    LLMStreamInterrupted,
    TenantLLMContext,
    retry_jitter_s,
)
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMContentBlocked,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.fake import FakeLLM, FakeScriptExhausted, Script
from app.llm.models import ModelRegistry, parse_registry
from app.llm.types import (
    DoneEvent,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys, llm_keys  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

TENANT = TenantLLMContext(tenant_id=uuid.UUID("7a1b2c3d-0000-4000-8000-00000000abcd"))
JITTER = 1.25


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def uid() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture
def keys(uid: str) -> dict[str, str]:
    return {
        "A": f"groq:a-{uid}",
        "A2": f"groq:a2-{uid}",
        "B": f"openrouter:b-{uid}",
        "C": f"groq:c-{uid}",  # condense
        "G": f"gemini:g-{uid}",  # trains on inputs
    }


def entry(key: str, *, rpm: int = 100, rpd: int = 1000, purposes: Sequence[str] = ("answer",)) -> dict[str, object]:
    provider, model_id = key.split(":", 1)
    return {
        "provider": provider,
        "model_id": model_id,
        "purposes": list(purposes),
        "trains_on_inputs": provider == "gemini",
        "rpm": rpm,
        "rpd": rpd,
        "pinned_on": "2026-11-24",
    }


def registry(keys: dict[str, str], **limits: tuple[int, int]) -> ModelRegistry:
    entries = []
    for name, key in keys.items():
        rpm, rpd = limits.get(name, (100, 1000))
        entries.append(entry(key, rpm=rpm, rpd=rpd, purposes=("condense",) if name == "C" else ("answer",)))
    return parse_registry({"schema_version": 1, "models": entries})


def make_settings(
    answer: Sequence[str] = (), condense: Sequence[str] = (), *, env: str = "development", **extra: Any
) -> Settings:
    fields: dict[str, object] = {
        "ENVIRONMENT": env,
        "LLM_ANSWER_CHAIN": ",".join(answer),
        "LLM_CONDENSE_CHAIN": ",".join(condense),
        "LLM_ZDR_CONFIRMED": False,
        "LLM_ALLOW_TRAINING_PROVIDERS": False,
    }
    if env != "development":
        fields["REDIS_PASSWORD"] = "a-redis-password"
    fields.update(extra)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model_key": "chain:any",
        "system": "You are a careful tutor.",
        "messages": (LLMMessage(role="user", content="What is osmosis?"),),
        "max_output_tokens": 64,
        "timeouts": LLMTimeouts(connect=5, first_token=10, idle=15, total=45),
        "request_id": "req-0001",
    }
    fields.update(overrides)
    return LLMRequest(**fields)


def ok(*tokens: str) -> Script:
    return Script(tokens=tokens or ("Hello", " world"), usage=UsageEvent(prompt_tokens=11, completion_tokens=2, estimated=False))


def build(
    clock: FakeClock,
    settings_: Settings,
    reg: ModelRegistry,
    fake: Any,
    *,
    jitter: float = JITTER,
    slots: SlotManager | None = None,
) -> LLMChain:
    return LLMChain(
        clients={"groq": fake, "openrouter": fake, "gemini": fake},
        registry=reg,
        settings=settings_,
        slots=slots or SlotManager(),
        breaker=Breaker(now_ms=clock.now_ms),
        cooldowns=Cooldowns(now_ms=clock.now_ms),
        clock=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: jitter,
    )


async def collect(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


async def collect_until_error(stream: AsyncIterator[LLMEvent]) -> tuple[list[LLMEvent], BaseException | None]:
    events: list[LLMEvent] = []
    try:
        async for event in stream:
            events.append(event)
    except Exception as exc:  # noqa: BLE001  (the test inspects it)
        return events, exc
    return events, None


def texts(events: Sequence[LLMEvent]) -> list[str]:
    return [e.text for e in events if isinstance(e, TokenEvent)]


def called(fake: FakeLLM) -> list[str]:
    return [c.model_key for c in fake.calls]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


FALLBACK_ERRORS = [
    pytest.param(lambda: LLMUnavailable(provider="groq"), "unavailable", id="unavailable"),
    pytest.param(lambda: LLMTimeout(provider="groq"), "timeout", id="timeout"),
]
NEVER_FALL_BACK = [
    pytest.param(LLMBadRequest, id="bad_request"),
    pytest.param(LLMContentBlocked, id="content_blocked"),
    pytest.param(LLMAuthError, id="auth_error"),
]
ALL_ERRORS = [
    pytest.param(lambda: LLMRateLimited(retry_after_s=1), id="rate_limited"),
    pytest.param(lambda: LLMUnavailable(), id="unavailable"),
    pytest.param(lambda: LLMTimeout(), id="timeout"),
    pytest.param(lambda: LLMBadRequest(), id="bad_request"),
    pytest.param(lambda: LLMContentBlocked(), id="content_blocked"),
    pytest.param(lambda: LLMAuthError(), id="auth_error"),
]


# ── the happy path ───────────────────────────────────────────────────────────────────────────────────


async def test_the_first_model_answers_and_every_event_passes_through(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([ok("Hel", "lo")])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    events = await collect(stream)
    assert texts(events) == ["Hel", "lo"]
    assert events[-2] == UsageEvent(prompt_tokens=11, completion_tokens=2, estimated=False)
    assert events[-1] == DoneEvent(finish_reason="stop")
    assert called(fake) == [keys["A"]]
    assert stream.model_key == keys["A"]
    assert [(a.model_key, a.result) for a in stream.attempts] == [(keys["A"], "ok")]
    assert clock.sleeps == []


async def test_the_request_sent_is_the_callers_with_only_the_model_key_replaced(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    original = request(temperature=0.3)
    await collect(chain.answer_stream("answer", original, TENANT))
    assert fake.calls[0] == dataclasses.replace(original, model_key=keys["A"])


# ── retry: once, before the first token only ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("make_error", "_code"), FALLBACK_ERRORS)
async def test_one_retry_on_the_same_model_before_the_first_token(
    make_error: Any, _code: str, clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(error=make_error()), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    events = await collect(chain.answer_stream("answer", request(), TENANT))
    assert texts(events) == ["Hello", " world"]
    assert called(fake) == [keys["A"], keys["A"]]
    assert clock.sleeps == [JITTER]


@pytest.mark.parametrize(("make_error", "code"), FALLBACK_ERRORS)
async def test_only_one_retry_then_the_next_model(make_error: Any, code: str, clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=make_error()), Script(error=make_error()), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["A"], keys["A"], keys["B"]]
    assert [(a.model_key, a.result) for a in stream.attempts] == [
        (keys["A"], code),
        (keys["A"], code),
        (keys["B"], "ok"),
    ]
    assert stream.model_key == keys["B"]


@pytest.mark.parametrize("make_error", ALL_ERRORS)
async def test_a_failure_after_the_first_token_is_neither_retried_nor_failed_over(
    make_error: Any, clock: FakeClock, keys: dict[str, str]
) -> None:
    error = make_error()
    fake = FakeLLM([Script(tokens=("a", "b", "c"), error=error, error_after_tokens=2), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    events, exc = await collect_until_error(chain.answer_stream("answer", request(), TENANT))
    assert texts(events) == ["a", "b"]
    assert isinstance(exc, LLMStreamInterrupted)
    assert type(exc.error) is type(error)
    assert called(fake) == [keys["A"]]
    assert clock.sleeps == []


def test_the_default_jitter_is_full_jitter_between_half_a_second_and_two_seconds() -> None:
    samples = [retry_jitter_s() for _ in range(2000)]
    assert all(0.5 <= s <= 2.0 for s in samples)
    assert min(samples) < 0.7 and max(samples) > 1.8  # spread over the range, not a constant


# ── never fall back ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("error_type", NEVER_FALL_BACK)
async def test_never_retry_or_fall_back_on_bad_request_blocked_or_auth(
    error_type: type[LLMError], clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(error=error_type(provider="groq")), ok(), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    with pytest.raises(error_type):
        await collect(stream)
    assert called(fake) == [keys["A"]]
    assert clock.sleeps == []


async def test_an_auth_error_opens_the_breaker_of_that_provider(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMAuthError(provider="groq")), ok()])
    chain = build(clock, make_settings([keys["A"], keys["A2"], keys["B"]]), registry(keys), fake)
    with pytest.raises(LLMAuthError):
        await collect(chain.answer_stream("answer", request(), TENANT))
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "open"
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["A"], keys["B"]]  # the second groq model is skipped too: the key is per provider
    assert [(a.model_key, a.result) for a in stream.attempts] == [
        (keys["A"], "breaker_open"),
        (keys["A2"], "breaker_open"),
        (keys["B"], "ok"),
    ]


async def test_an_auth_error_is_logged_for_the_operator_without_any_text(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMAuthError(provider="groq"))])
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    with capture_logs() as logs, pytest.raises(LLMAuthError):
        await collect(chain.answer_stream("answer", request(), TENANT))
    auth = [e for e in logs if e["event"] == "llm_auth_failed"]
    assert len(auth) == 1 and auth[0]["log_level"] == "error"
    assert auth[0]["provider"] == "groq"


# ── fallback ─────────────────────────────────────────────────────────────────────────────────────────


async def test_every_model_failing_ends_in_chain_exhausted_with_codes_only(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=Script(error=LLMUnavailable()))
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(stream)
    assert called(fake) == [keys["A"], keys["A"], keys["B"], keys["B"]]
    assert [a.result for a in caught.value.attempts] == ["unavailable"] * 4
    assert "osmosis" not in str(caught.value) and "tutor" not in str(caught.value)
    assert stream.model_key is None


async def test_an_empty_answer_chain_calls_nobody_and_says_so(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([]), registry(keys), fake)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("answer", request(), TENANT))
    assert caught.value.attempts == () and caught.value.not_configured is True
    assert fake.call_count == 0


# ── Retry-After and cooldowns ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("retry_after", [0.0, 3.0, RETRY_AFTER_MAX_S])
async def test_a_429_with_a_short_retry_after_waits_exactly_that_long_and_retries(
    retry_after: float, clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(error=LLMRateLimited(retry_after_s=retry_after)), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["A"], keys["A"]]
    assert clock.sleeps == [retry_after]


@pytest.mark.parametrize("retry_after", [5.001, 30.0, 3600.0])
async def test_a_429_with_a_long_retry_after_falls_back_at_once_and_cools_the_model_down(
    retry_after: float, clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(error=LLMRateLimited(retry_after_s=retry_after)), ok(), ok(), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["A"], keys["B"]]
    assert clock.sleeps == []
    assert await Cooldowns(now_ms=clock.now_ms).remaining_ms(keys["A"]) == int(round(retry_after * 1000))

    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake)[-1] == keys["B"]
    assert (keys["A"], "cooldown") in [(a.model_key, a.result) for a in stream.attempts]

    clock.advance(retry_after)
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake)[-1] == keys["A"]  # the cooldown is over


async def test_a_429_without_retry_after_retries_after_jitter_and_sets_no_cooldown(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMRateLimited()), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["A"], keys["A"]]
    assert clock.sleeps == [JITTER]
    assert await Cooldowns(now_ms=clock.now_ms).remaining_ms(keys["A"]) == 0


async def test_a_short_retry_after_also_cools_the_model_down_for_other_callers(clock: FakeClock, keys: dict[str, str]) -> None:
    seen: list[int] = []
    cooldowns = Cooldowns(now_ms=clock.now_ms)

    async def sleep_and_look(seconds: float) -> None:
        seen.append(await cooldowns.remaining_ms(keys["A"]))  # what another request sees during the wait
        await clock.sleep(seconds)

    fake = FakeLLM([Script(error=LLMRateLimited(retry_after_s=3)), ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    chain._sleep = sleep_and_look  # noqa: SLF001
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert seen == [3000]


async def test_a_429_after_the_first_token_still_cools_the_model_down(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(tokens=("a", "b"), error=LLMRateLimited(retry_after_s=20), error_after_tokens=1)])
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    _, exc = await collect_until_error(chain.answer_stream("answer", request(), TENANT))
    assert isinstance(exc, LLMStreamInterrupted)
    assert await Cooldowns(now_ms=clock.now_ms).remaining_ms(keys["A"]) == 20_000


# ── the breaker inside the chain ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("make_error", "_code"), FALLBACK_ERRORS)
async def test_unavailable_and_timeout_count_towards_the_breaker(
    make_error: Any, _code: str, clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(
        rules=[(lambda r: r.model_key.startswith("groq:"), Script(error=make_error())), (lambda r: True, ok())]
    )
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    for _ in range(3):  # 2 failures each: the breaker opens on the fifth
        await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake).count(keys["A"]) == 5
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "open"


async def test_a_failure_after_the_first_token_still_counts_towards_the_breaker(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=Script(tokens=("a", "b"), error=LLMUnavailable(), error_after_tokens=1))
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    for _ in range(5):
        _, exc = await collect_until_error(chain.answer_stream("answer", request(), TENANT))
        assert isinstance(exc, LLMStreamInterrupted)
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "open"


async def test_an_auth_error_after_the_first_token_opens_the_breaker_too(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(tokens=("a", "b"), error=LLMAuthError(), error_after_tokens=1)])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    _, exc = await collect_until_error(chain.answer_stream("answer", request(), TENANT))
    assert isinstance(exc, LLMStreamInterrupted) and isinstance(exc.error, LLMAuthError)
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "open"
    assert called(fake) == [keys["A"]]


async def test_rate_limits_and_refusals_do_not_count_towards_the_breaker(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMRateLimited()), Script(error=LLMRateLimited()), ok()] * 3 + [Script(error=LLMBadRequest())] * 3)
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    for _ in range(3):
        await collect(chain.answer_stream("answer", request(), TENANT))
    for _ in range(3):
        with pytest.raises(LLMBadRequest):
            await collect(chain.answer_stream("answer", request(), TENANT))
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "closed"


async def open_groq(clock: FakeClock) -> None:
    b = Breaker(now_ms=clock.now_ms)
    for _ in range(5):
        await b.record_failure("groq", await b.admit("groq"))
    assert await b.state("groq") == "open"


async def test_an_open_breaker_skips_the_provider(clock: FakeClock, keys: dict[str, str]) -> None:
    await open_groq(clock)
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["B"]]


async def test_half_open_lets_exactly_one_of_ten_simultaneous_requests_probe(clock: FakeClock, keys: dict[str, str]) -> None:
    await open_groq(clock)
    clock.advance(60.0)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    others_done = asyncio.Event()
    finished_on_b: list[str] = []

    async def probe_sleep(_seconds: float) -> None:
        probe_started.set()
        await release_probe.wait()

    fake = FakeLLM(
        rules=[
            (lambda r: r.model_key == keys["A"], Script(tokens=("probe",), delay_first_token_s=1.0)),
            (lambda r: r.model_key == keys["B"], ok()),
        ],
        sleep=probe_sleep,
    )
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)

    async def one() -> str | None:
        stream = chain.answer_stream("answer", request(), TENANT)
        await collect(stream)
        if stream.model_key == keys["B"]:
            finished_on_b.append(keys["B"])
            if len(finished_on_b) == 9:
                others_done.set()
        return stream.model_key

    tasks = [asyncio.create_task(one()) for _ in range(10)]
    async with asyncio.timeout(10):
        await probe_started.wait()
        await others_done.wait()
    release_probe.set()
    results = await asyncio.gather(*tasks)
    assert results.count(keys["A"]) == 1 and results.count(keys["B"]) == 9
    assert called(fake).count(keys["A"]) == 1
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "closed"  # the probe answered


async def test_a_failed_probe_opens_the_breaker_again(clock: FakeClock, keys: dict[str, str]) -> None:
    await open_groq(clock)
    clock.advance(60.0)
    fake = FakeLLM([Script(error=LLMUnavailable()), ok(), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    # the probe failed: no retry on an open breaker, the next model answered
    assert called(fake) == [keys["A"], keys["B"]]
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "open"


async def test_a_probe_cancelled_before_its_first_token_frees_the_probe(clock: FakeClock, keys: dict[str, str]) -> None:
    await open_groq(clock)
    clock.advance(60.0)
    started = asyncio.Event()

    async def hang(_seconds: float) -> None:
        started.set()
        await asyncio.Event().wait()

    fake = FakeLLM([Script(tokens=("x",), delay_first_token_s=1.0)], sleep=hang)
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    task = asyncio.create_task(collect(chain.answer_stream("answer", request(), TENANT)))
    async with asyncio.timeout(10):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await Breaker(now_ms=clock.now_ms).admit("groq")).state is Admission.PROBE


async def test_a_probe_that_reaches_its_first_token_closes_the_breaker_even_if_the_reader_stops(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    await open_groq(clock)
    clock.advance(60.0)
    fake = FakeLLM([ok("one", "two", "three")])
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    first = await anext(stream)
    assert first == TokenEvent(text="one")
    await stream.aclose()
    assert await Breaker(now_ms=clock.now_ms).state("groq") == "closed"


# ── provider RPM / RPD ───────────────────────────────────────────────────────────────────────────────


async def test_two_requests_racing_for_the_last_rpm_slot_one_wins_one_falls_back(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, A=(1, 1000)), fake)

    async def one() -> str | None:
        stream = chain.answer_stream("answer", request(), TENANT)
        await collect(stream)
        return stream.model_key

    results = await asyncio.gather(one(), one())
    assert sorted(results) == sorted([keys["A"], keys["B"]])  # type: ignore[type-var]
    assert called(fake).count(keys["A"]) == 1
    assert await SlotManager().count(f"llm:rpm:{keys['A']}") == 1


@pytest.mark.parametrize(
    ("limits", "full", "untouched", "code"),
    [
        pytest.param((5, 1), "rpd", "rpm", "rpd_limit", id="rpd_full_rpm_untouched"),
        pytest.param((1, 5), "rpm", "rpd", "rpm_limit", id="rpm_full_rpd_untouched"),
    ],
)
async def test_a_rejected_limit_consumes_nothing_on_the_other_key(
    limits: tuple[int, int], full: str, untouched: str, code: str, clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, A=limits), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["A"], keys["B"]]
    assert (keys["A"], code) in [(a.model_key, a.result) for a in stream.attempts]
    slots = SlotManager()
    assert await slots.count(f"llm:{full}:{keys['A']}") == 1
    assert await slots.count(f"llm:{untouched}:{keys['A']}") == 1  # the rejected request added nothing here


async def test_the_retry_is_a_new_request_and_needs_a_slot_too(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMUnavailable()), ok()])
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, A=(1, 1000)), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["A"], keys["B"]]
    assert [a.result for a in stream.attempts] == ["unavailable", "rpm_limit", "ok"]


async def test_a_slot_rejection_during_half_open_gives_the_probe_back(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, A=(1, 1000)), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))  # takes A's only RPM slot
    await open_groq(clock)
    clock.advance(60.0)
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert stream.model_key == keys["B"]
    assert (await Breaker(now_ms=clock.now_ms).admit("groq")).state is Admission.PROBE  # not left claimed


# ── timeouts ─────────────────────────────────────────────────────────────────────────────────────────


async def test_a_late_first_token_is_a_timeout_retried_then_failed_over_and_never_shown(clock: FakeClock, keys: dict[str, str]) -> None:
    late = Script(tokens=("LATE",), delay_first_token_s=10.5)
    fake = FakeLLM([late, late, ok()], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    events = await collect(stream)
    assert "LATE" not in texts(events)
    assert called(fake) == [keys["A"], keys["A"], keys["B"]]
    assert [a.result for a in stream.attempts] == ["timeout", "timeout", "ok"]


async def test_a_first_token_exactly_at_the_limit_is_accepted(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(tokens=("ok",), delay_first_token_s=10.0)], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    assert texts(await collect(chain.answer_stream("answer", request(), TENANT))) == ["ok"]


async def test_an_idle_gap_after_the_first_token_interrupts_the_answer(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(tokens=("a", "b"), delay_between_tokens_s=15.5), ok()], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    events, exc = await collect_until_error(chain.answer_stream("answer", request(), TENANT))
    assert texts(events) == ["a"]
    assert isinstance(exc, LLMStreamInterrupted) and isinstance(exc.error, LLMTimeout)
    assert called(fake) == [keys["A"]]


async def test_the_total_spans_every_attempt_and_shrinks_the_later_ones(clock: FakeClock, keys: dict[str, str]) -> None:
    late = Script(tokens=("LATE",), delay_first_token_s=10.5)
    fake = FakeLLM([late, late, ok()], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    await collect(chain.answer_stream("answer", request(), TENANT))
    # 10.5 (first try) + 1.25 (jitter) + 10.5 (retry) = 22.25 s used of 45
    sent = fake.calls[2].timeouts
    assert sent.total == pytest.approx(45 - 22.25)
    assert sent.first_token == 10 and sent.idle == 15 and sent.connect == 5


async def test_no_attempt_starts_once_the_total_is_used_up(clock: FakeClock, keys: dict[str, str]) -> None:
    late = Script(tokens=("LATE",), delay_first_token_s=10.5)
    fake = FakeLLM([late, late, ok()], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    timeouts = LLMTimeouts(connect=5, first_token=10, idle=15, total=20)
    stream = chain.answer_stream("answer", request(timeouts=timeouts), TENANT)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(stream)
    assert fake.calls[1].timeouts.first_token == pytest.approx(20 - 11.75)  # cut to what was left
    assert called(fake) == [keys["A"], keys["A"]]
    assert [a.result for a in caught.value.attempts] == ["timeout", "timeout", "deadline"]


async def test_every_limit_of_a_later_attempt_is_cut_to_what_is_left_including_connect(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    late = Script(tokens=("LATE",), delay_first_token_s=10.5)
    fake = FakeLLM([late, ok()], sleep=clock.sleep)
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    timeouts = LLMTimeouts(connect=5, first_token=10, idle=10, total=12)
    await collect_until_error(chain.answer_stream("answer", request(timeouts=timeouts), TENANT))
    # 12 - 10.5 - 1.25 = 0.25 s left for the retry
    second = fake.calls[1].timeouts
    assert (second.connect, second.first_token, second.idle, second.total) == pytest.approx((0.25, 0.25, 0.25, 0.25))


async def test_the_real_time_backstop_stops_a_client_that_never_answers(keys: dict[str, str]) -> None:
    # The one test on the real clock: a client that ignores its timeouts and never yields. The limits are a
    # few milliseconds, so the test waits about 40 ms in all.
    closed: list[bool] = []

    class Hanging:
        provider = "fake"
        calls = 0

        async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
            Hanging.calls += 1
            try:
                await asyncio.Event().wait()
                yield TokenEvent(text="never")
            finally:
                closed.append(True)

    async def no_wait(_s: float) -> None:
        return None

    chain = LLMChain(
        clients={"groq": Hanging()},
        registry=registry(keys),
        settings=make_settings([keys["A"]]),
        slots=SlotManager(),
        breaker=Breaker(),
        cooldowns=Cooldowns(),
        clock=time.monotonic,
        sleep=no_wait,
        jitter=lambda: 0.0,  # a real 0.5 s would not fit in the 0.2 s total, and the chain would skip the retry
    )
    timeouts = LLMTimeouts(connect=0.01, first_token=0.02, idle=0.02, total=0.2)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("answer", request(timeouts=timeouts), TENANT))
    assert [a.result for a in caught.value.attempts] == ["timeout", "timeout"]
    assert Hanging.calls == 2 and closed == [True, True]


# ── purposes: answer and condense ────────────────────────────────────────────────────────────────────


async def test_an_empty_condense_chain_means_no_condense_not_an_error(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]], condense=[]), registry(keys), fake)
    stream = chain.answer_stream("condense", request(), TENANT)
    assert await collect(stream) == []
    assert stream.configured is False and stream.model_key is None
    assert fake.call_count == 0
    assert chain.is_configured("condense") is False and chain.is_configured("answer") is True


async def test_condense_uses_only_the_models_listed_for_it(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok("short question"))
    chain = build(clock, make_settings([keys["A"]], condense=[keys["C"]]), registry(keys), fake)
    stream = chain.answer_stream("condense", request(), TENANT)
    assert texts(await collect(stream)) == ["short question"]
    assert stream.configured is True
    await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["C"], keys["A"]]


async def test_a_failed_condense_chain_raises_exhausted_for_the_caller_to_use_the_raw_question(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(default=Script(error=LLMUnavailable()))
    chain = build(clock, make_settings([keys["A"]], condense=[keys["C"]]), registry(keys), fake)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("condense", request(), TENANT))
    assert caught.value.not_configured is False


@pytest.mark.parametrize("purpose", ["translate", "", "ANSWER", None])
def test_an_unknown_purpose_is_refused(purpose: Any, clock: FakeClock, keys: dict[str, str]) -> None:
    chain = build(clock, make_settings([keys["A"]]), registry(keys), FakeLLM())
    with pytest.raises(ValueError):
        chain.answer_stream(purpose, request(), TENANT)


def test_the_tenant_context_is_typed(clock: FakeClock, keys: dict[str, str]) -> None:
    chain = build(clock, make_settings([keys["A"]]), registry(keys), FakeLLM())
    with pytest.raises(TypeError):
        chain.answer_stream("answer", request(), {"tenant_id": "x"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TenantLLMContext(tenant_id="7a1b2c3d-0000-4000-8000-00000000abcd")  # type: ignore[arg-type]


# ── the start-up policy is checked again for every key ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bad", "env", "extra"),
    [
        pytest.param("groq:not-in-the-file", "development", {}, id="unknown_key"),
        pytest.param("C", "development", {}, id="condense_model_in_answer_chain"),
        pytest.param("G", "production", {"LLM_ZDR_CONFIRMED": True}, id="training_model_outside_development"),
        pytest.param("G", "development", {}, id="training_model_without_the_flag"),
        pytest.param("A2", "production", {}, id="groq_without_zdr_outside_development"),
        pytest.param("evilcorp:model", "development", {}, id="provider_not_allowed"),
    ],
)
async def test_a_key_the_start_up_policy_would_refuse_is_never_called(
    bad: str, env: str, extra: dict[str, object], clock: FakeClock, keys: dict[str, str]
) -> None:
    bad_key = keys.get(bad, bad)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([bad_key, keys["B"]], env=env, **extra), registry(keys), fake)
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["B"]]
    assert stream.attempts[0].result == "not_allowed"


# ── Redis down ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _down)


@pytest.mark.parametrize("env", ["production", "staging"])
async def test_redis_down_outside_development_is_service_unavailable_and_calls_no_provider(
    env: str, redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, keys: dict[str, str]
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["B"]], env=env), registry(keys), fake)
    with pytest.raises(HTTPException) as caught:
        await collect(chain.answer_stream("answer", request(), TENANT))
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "service_unavailable"  # type: ignore[index]
    assert fake.call_count == 0


async def test_redis_down_in_development_still_answers(
    redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, keys: dict[str, str]
) -> None:
    # Development choice: no breaker and no cooldown (Redis holds them), and SlotManager's per-process limits.
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys), fake)
    assert texts(await collect(chain.answer_stream("answer", request(), TENANT))) == ["Hello", " world"]
    assert called(fake) == [keys["A"]]


# ── nothing a student wrote or read is stored in Redis or in a log ───────────────────────────────────


async def _all_redis_text() -> str:
    r = await redis_client.get_redis()
    parts: list[str] = []
    for key in await llm_keys():
        parts.append(key)
        kind = await r.type(key)
        if kind == "string":
            parts.append(str(await r.get(key)))
        elif kind == "hash":
            parts.extend(f"{k}={v}" for k, v in (await r.hgetall(key)).items())
        elif kind == "zset":
            parts.extend(f"{m}:{s}" for m, s in await r.zrange(key, 0, -1, withscores=True))
        else:
            parts.append(f"<{kind}>")
    return "\n".join(parts)


async def test_no_prompt_or_answer_text_reaches_redis_or_the_logs(
    clock: FakeClock, keys: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    canary_q = "CANARY-QUESTION-5d1f9a"
    canary_s = "CANARY-SYSTEM-77ab02"
    canary_a = "CANARY-ANSWER-c4e8e1"
    fake = FakeLLM(
        [
            Script(error=LLMRateLimited(retry_after_s=30)),  # A: cooldown
            Script(error=LLMUnavailable()),  # B: breaker failure
            Script(error=LLMUnavailable()),  # B retry
            ok(canary_a, " done"),  # A2 answers
            Script(tokens=(canary_a,), error=LLMTimeout(), error_after_tokens=1),  # interrupted
        ]
    )
    chain = build(clock, make_settings([keys["A"], keys["B"], keys["A2"]]), registry(keys), fake)
    req = request(system=canary_s, messages=(LLMMessage(role="user", content=canary_q),))
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        await collect(chain.answer_stream("answer", req, TENANT))
        await collect_until_error(chain.answer_stream("answer", req, TENANT))
    stored = await _all_redis_text()
    logged = "\n".join(repr(e) for e in logs) + "\n" + caplog.text
    for canary in (canary_q, canary_s, canary_a):
        assert canary not in stored
        assert canary not in logged
    assert str(TENANT.tenant_id) not in stored
    for key in await llm_keys():
        assert re.fullmatch(r"llm:(cb|cooldown|rpm|rpd):[A-Za-z0-9._:/@+-]+", key), key
    assert logs, "the chain logs its attempts (ids and codes only)"


# ── odd clients ──────────────────────────────────────────────────────────────────────────────────────


async def test_a_bug_in_a_client_propagates_and_is_never_failed_over(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([])  # no script: FakeScriptExhausted, an AssertionError
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys), fake)
    with pytest.raises(FakeScriptExhausted):
        await collect(chain.answer_stream("answer", request(), TENANT))
    assert called(fake) == [keys["A"]]


async def test_a_provider_with_no_client_is_skipped(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = LLMChain(
        clients={"openrouter": fake},
        registry=registry(keys),
        settings=make_settings([keys["A"], keys["B"]]),
        slots=SlotManager(),
        breaker=Breaker(now_ms=clock.now_ms),
        cooldowns=Cooldowns(now_ms=clock.now_ms),
        clock=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: JITTER,
    )
    stream = chain.answer_stream("answer", request(), TENANT)
    await collect(stream)
    assert called(fake) == [keys["B"]]
    assert stream.attempts[0].result == "no_client"


# ── the module-level entry point ─────────────────────────────────────────────────────────────────────


async def test_the_default_chain_builds_one_adapter_per_allowed_provider_without_network() -> None:
    chain = chain_module.default_chain()
    try:
        assert chain is chain_module.default_chain()  # one per process: one shared httpx client per provider
        assert set(chain.client_names()) == {"groq", "openrouter", "gemini"}
    finally:
        await chain_module.aclose_default_chain()
    assert chain_module.default_chain() is not chain
    await chain_module.aclose_default_chain()
