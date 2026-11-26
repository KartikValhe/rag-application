"""
Step 6a-10b (docs/phase6-plan.md 6a-10b): the token-weighted tokens-per-minute window in the chain.

Rules under test (the owner's 6a-10b brief):
  * a model with a `tpm` reserves (estimated prompt + max_output_tokens, the SAME estimator as the budget service)
    before the call, in one atomic script; a model without a `tpm` gets no window check and no Redis call for it;
  * after the call the window is corrected to the provider-reported prompt + completion tokens (hidden reasoning
    included): more is added, less is refunded; a failure before the first token refunds the whole reservation;
    a cancel or an interruption after tokens corrects to the best known count (the reported usage if it arrived,
    else the prompt estimate plus the estimate of the text sent);
  * a fallback model is checked against its OWN limit; one model's tpm is never applied to another;
  * a full window never makes the chain wait: the model is skipped with the result `tpm_limit` and a retry-after
    (the attempt record carries it), no cooldown is set (a smaller request may fit), the next model is tried;
  * a request heavier than the model's whole limit can never fit: `tpm_oversize`, at once, with no Redis call
    for the window and nothing taken from RPM/RPD;
  * a reservation that a full RPM/RPD slot then refuses is given back; a full window takes nothing from RPM/RPD;
  * Redis down follows the policy of the RPM/RPD acquire: 503 outside development, a per-process window in it.

Every provider is a FakeLLM or a tiny scripted client; time is a fake clock whose sleep returns at once and is
asserted never to have been called. Redis is the test Redis (DB 1).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.core import redis_client
from app.core.config import Settings, settings
from app.core.slots import SlotManager
from app.llm.breaker import Breaker, Cooldowns
from app.llm.chain import (
    Attempt,
    LLMChain,
    LLMChainExhausted,
    LLMStreamInterrupted,
    TenantLLMContext,
    rpd_key,
    rpm_key,
)
from app.llm.errors import LLMUnavailable
from app.llm.fake import FakeLLM, Script
from app.llm.models import ModelRegistry, parse_registry
from app.llm.tpm import TpmRejected, TpmReservation, TpmWindow, tpm_key
from app.llm.types import LLMEvent, LLMMessage, LLMRequest, LLMTimeouts, TokenEvent, UsageEvent
from app.services import llm_budget
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys, llm_keys  # noqa: F401  (pytest fixture)
from tests.fixtures.llm_tpm import SpyRedis, assert_all_overlapped

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

_real_get_redis = redis_client.get_redis

TENANT = TenantLLMContext(tenant_id=uuid.UUID("7a1b2c3d-0000-4000-8000-00000000abcd"))
APP_LLM = Path(__file__).resolve().parent.parent / "app" / "llm"
TPM_A = 3000  # the limit of model A; one request weighs about 1014, so two fit and a third does not


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def uid() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture
def keys(uid: str) -> dict[str, str]:
    return {"A": f"groq:a-{uid}", "B": f"openrouter:b-{uid}", "C": f"groq:c-{uid}"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def registry(keys: dict[str, str], tpm: dict[str, int | None], rpm: dict[str, int] | None = None) -> ModelRegistry:
    entries = []
    for name, key in keys.items():
        provider, model_id = key.split(":", 1)
        row: dict[str, object] = {
            "provider": provider,
            "model_id": model_id,
            "purposes": ["answer"],
            "trains_on_inputs": False,
            "rpm": (rpm or {}).get(name, 100),
            "rpd": 1000,
            "pinned_on": "2026-11-24",
        }
        if tpm.get(name) is not None:
            row["tpm"] = tpm[name]
        entries.append(row)
    return parse_registry({"schema_version": 1, "models": entries})


def make_settings(answer: Sequence[str], *, env: str = "development") -> Settings:
    fields: dict[str, object] = {
        "ENVIRONMENT": env,
        "LLM_ANSWER_CHAIN": ",".join(answer),
        "LLM_CONDENSE_CHAIN": "",
        "LLM_ZDR_CONFIRMED": False,
        "LLM_ALLOW_TRAINING_PROVIDERS": False,
    }
    if env != "development":
        fields["REDIS_PASSWORD"] = "a-redis-password"
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model_key": "chain:any",
        "system": "You are a careful tutor.",
        "messages": (LLMMessage(role="user", content="What is osmosis?"),),
        "max_output_tokens": 1000,
        "timeouts": LLMTimeouts(connect=5, first_token=10, idle=15, total=45),
        "request_id": "req-0001",
    }
    fields.update(overrides)
    return LLMRequest(**fields)


REQ = request()
WEIGHT = llm_budget.reservation_amount(REQ)  # 14 prompt tokens + 1000 = 1014 with the default ratios
PROMPT_EST = llm_budget.estimate_prompt_tokens(REQ)


def ok(*tokens: str, prompt: int = 11, completion: int = 2) -> Script:
    return Script(
        tokens=tokens or ("Hello", " world"),
        usage=UsageEvent(prompt_tokens=prompt, completion_tokens=completion, estimated=False),
    )


def build(
    clock: FakeClock,
    settings_: Settings,
    reg: ModelRegistry,
    client: Any,
    window: TpmWindow | None = None,
    slots: SlotManager | None = None,
) -> LLMChain:
    return LLMChain(
        clients={"groq": client, "openrouter": client, "gemini": client},
        registry=reg,
        settings=settings_,
        slots=slots or SlotManager(),
        breaker=Breaker(now_ms=clock.now_ms),
        cooldowns=Cooldowns(now_ms=clock.now_ms),
        clock=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 1.25,
        tpm=window or TpmWindow(now_ms=clock.now_ms),
    )


async def collect(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


async def members(model_key: str) -> list[tuple[str, int, int]]:
    r = await _real_get_redis()
    out = []
    for member, score in await r.zrange(tpm_key(model_key), 0, -1, withscores=True):
        token, weight = member.rsplit(":", 1)
        out.append((token, int(weight), int(score)))
    return out


async def total(model_key: str) -> int:
    return sum(w for _, w, _ in await members(model_key))


async def fill(clock: FakeClock, model_key: str, limit: int) -> TpmReservation:
    """Fill the model's window to its limit with one reservation (as other requests in flight would)."""
    res = await TpmWindow(now_ms=clock.now_ms).reserve(model_key, limit, limit)
    assert isinstance(res, TpmReservation)
    return res


def spy_on(monkeypatch: pytest.MonkeyPatch, real: Any, parties: int | None = None) -> SpyRedis:
    spy = SpyRedis(real, parties)

    async def spied() -> Any:
        return spy

    monkeypatch.setattr(redis_client, "get_redis", spied)
    return spy


def touched(spy: SpyRedis, prefix: str) -> list[str]:
    return [name for name, args in spy.calls if any(isinstance(a, str) and a.startswith(prefix) for a in args)]


class ScriptedClient:
    """A provider that sends `tokens`, then waits for ever: the test cancels or closes the stream at that point."""

    provider = "fake"

    def __init__(self, tokens: Sequence[str] = ("Hello", " world"), *, usage: UsageEvent | None = None) -> None:
        self.tokens = tuple(tokens)
        self.usage = usage
        self.reached = asyncio.Event()
        self.calls: list[LLMRequest] = []

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.calls.append(req)
        for text in self.tokens:
            yield TokenEvent(text=text)
        if self.usage is not None:
            yield self.usage
        self.reached.set()
        await asyncio.Event().wait()


# ── the weight: reserved before the call, with the budget service's estimator ────────────────────────


async def test_the_reservation_is_the_prompt_estimate_plus_max_output_tokens_while_the_call_runs(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    stream = chain.answer_stream("answer", REQ, TENANT)
    first = await anext(stream)
    assert isinstance(first, TokenEvent)
    assert await total(keys["A"]) == WEIGHT == PROMPT_EST + REQ.max_output_tokens
    await collect(stream)


async def test_the_weight_is_the_budget_services_estimate_not_a_second_one(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN", 1.0)
    req = request()
    expected = len(req.system) + len(req.messages[0].content) + req.max_output_tokens
    assert llm_budget.reservation_amount(req) == expected  # the ratio setting moved the shared estimator
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": 5000}), fake)
    stream = chain.answer_stream("answer", req, TENANT)
    await anext(stream)
    assert await total(keys["A"]) == expected
    await collect(stream)


def test_the_chain_has_no_estimator_of_its_own() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in APP_LLM.glob("*.py")}
    assert "CHARS_PER_TOKEN" not in "".join(sources.values())
    assert not [name for name, text in sources.items() if re.search(r"def \w*estimate\w*\(", text)]
    chain_text = sources["chain.py"]
    assert "from app.services.llm_budget import" in chain_text and "reservation_amount" in chain_text


# ── corrected to the provider-reported usage ─────────────────────────────────────────────────────────


async def test_after_the_call_the_window_holds_what_the_provider_reported_not_the_reservation(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([ok(prompt=11, completion=2)])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert await total(keys["A"]) == 13
    assert len(await members(keys["A"])) == 1


async def test_hidden_reasoning_above_the_reservation_is_added(clock: FakeClock, keys: dict[str, str]) -> None:
    # Two tokens of visible text, but the provider counts 3,000 completion tokens (the hidden reasoning is in it).
    fake = FakeLLM([ok("a", "b", prompt=100, completion=3000)])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert await total(keys["A"]) == 3100 > WEIGHT
    refused = await TpmWindow(now_ms=clock.now_ms).reserve(keys["A"], TPM_A, 1000)
    assert isinstance(refused, TpmRejected) and refused.used == 3100  # the overrun counts against the next request


async def test_success_without_reported_usage_uses_the_estimate_of_what_was_sent(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(tokens=("Hello", " world"))])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert await total(keys["A"]) == PROMPT_EST + llm_budget.estimate_text_tokens("Hello world")


# ── refund and correction on every way out ───────────────────────────────────────────────────────────


async def test_a_failure_before_the_first_token_refunds_the_whole_reservation(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_on(monkeypatch, await _real_get_redis())
    fake = FakeLLM([Script(error=LLMUnavailable(provider="groq")), Script(error=LLMUnavailable(provider="groq"))])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    with pytest.raises(LLMChainExhausted):
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert fake.call_count == 2  # the one retry
    assert await total(keys["A"]) == 0
    # two reservations and two refunds, one command each
    assert touched(spy, "llm:tpm:") == ["eval"] * 4


async def test_the_retry_is_its_own_request_with_its_own_reservation(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([Script(error=LLMUnavailable(provider="groq")), ok(prompt=20, completion=5)])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert await total(keys["A"]) == 25  # the failed attempt left nothing behind


async def test_a_deadline_that_passes_after_the_reservation_gives_it_back(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    req = request(timeouts=LLMTimeouts(connect=1, first_token=1, idle=1, total=1))
    original = TpmWindow.reserve

    async def slow(self: TpmWindow, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        clock.advance(5)  # the reservation "took 5 seconds": the whole answer time is gone
        return result

    monkeypatch.setattr(TpmWindow, "reserve", slow)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("answer", req, TENANT))
    assert [a.result for a in caught.value.attempts] == ["deadline"]
    assert fake.call_count == 0
    assert await total(keys["A"]) == 0


async def test_a_failure_after_the_first_token_corrects_to_the_best_known_count(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM([Script(tokens=("Hello", " world", "!"), error=LLMUnavailable(provider="groq"), error_after_tokens=2)])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    with pytest.raises(LLMStreamInterrupted):
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert fake.call_count == 1  # never retried after a token
    assert await total(keys["A"]) == PROMPT_EST + llm_budget.estimate_text_tokens("Hello world")


async def test_closing_the_stream_after_tokens_corrects_to_the_best_known_count(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    client = ScriptedClient(("Hello", " world"))
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), client)
    stream = chain.answer_stream("answer", REQ, TENANT)
    await anext(stream)
    await anext(stream)
    assert await total(keys["A"]) == WEIGHT
    await stream.aclose()  # the client went away
    assert await total(keys["A"]) == PROMPT_EST + llm_budget.estimate_text_tokens("Hello world")


async def test_closing_the_stream_after_the_usage_arrived_uses_the_reported_count(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    client = ScriptedClient(("Hello",), usage=UsageEvent(prompt_tokens=40, completion_tokens=900, estimated=False))
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), client)
    stream = chain.answer_stream("answer", REQ, TENANT)
    seen = [await anext(stream), await anext(stream)]
    assert isinstance(seen[1], UsageEvent)
    await stream.aclose()
    assert await total(keys["A"]) == 940


async def test_a_cancelled_task_after_tokens_corrects_to_the_best_known_count(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    client = ScriptedClient(("Hello", " world"))
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), client)

    async def consume() -> None:
        async with aclosing(chain.answer_stream("answer", REQ, TENANT)) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(client.reached.wait(), 5)
    assert await total(keys["A"]) == WEIGHT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await total(keys["A"]) == PROMPT_EST + llm_budget.estimate_text_tokens("Hello world")


async def test_a_cancelled_task_before_the_first_token_refunds_the_reservation(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    client = ScriptedClient(())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), client)

    async def consume() -> None:
        async with aclosing(chain.answer_stream("answer", REQ, TENANT)) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(client.reached.wait(), 5)
    assert await total(keys["A"]) == WEIGHT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await total(keys["A"]) == 0


async def test_a_reservation_nobody_settled_leaves_with_the_window(clock: FakeClock, keys: dict[str, str]) -> None:
    # A crashed worker never settles. The next request, a minute later, finds the window empty again.
    await fill(clock, keys["A"], TPM_A)
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    with pytest.raises(LLMChainExhausted):
        await collect(chain.answer_stream("answer", REQ, TENANT))
    clock.advance(60)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert fake.call_count == 1
    assert await total(keys["A"]) == 13


# ── a model without tpm ──────────────────────────────────────────────────────────────────────────────


async def test_a_model_without_tpm_gets_no_window_check_and_no_extra_redis_call(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_on(monkeypatch, await _real_get_redis())
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": None}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert touched(spy, "llm:tpm:") == []
    assert [k for k in await llm_keys() if k.startswith("llm:tpm:")] == []
    assert touched(spy, "llm:rpm:") == ["eval"] and touched(spy, "llm:rpd:") == ["eval"]  # the unchanged acquire


async def test_a_huge_request_is_never_limited_on_a_model_without_tpm(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM([ok()])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": None}), fake)
    stream = chain.answer_stream("answer", request(max_output_tokens=900_000), TENANT)
    await collect(stream)
    assert [(a.model_key, a.result) for a in stream.attempts] == [(keys["A"], "ok")]


async def test_the_pinned_models_json_limits_only_the_120b() -> None:
    from app.llm.models import load_registry

    reg = load_registry()
    assert {m.key: m.tpm for m in reg.models} == {"groq:openai/gpt-oss-120b": 8000, "groq:openai/gpt-oss-20b": None}


# ── a full window: skipped, never waited for ─────────────────────────────────────────────────────────


async def test_a_full_window_skips_the_model_with_a_retry_after_and_never_waits(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    await fill(clock, keys["A"], TPM_A)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": TPM_A, "B": None}), fake)
    stream = chain.answer_stream("answer", REQ, TENANT)
    await collect(stream)
    assert [(c.model_key) for c in fake.calls] == [keys["B"]]
    assert stream.attempts == [
        Attempt(model_key=keys["A"], result="tpm_limit", retry_after_s=60),
        Attempt(model_key=keys["B"], result="ok"),
    ]
    assert clock.sleeps == [] and clock.now == FakeClock().now  # not one second was spent waiting


async def test_every_model_full_ends_exhausted_with_the_retry_after_in_the_attempts(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    await fill(clock, keys["A"], TPM_A)
    clock.advance(15)
    await fill(clock, keys["B"], 5000)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": TPM_A, "B": 5000}), fake)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert caught.value.attempts == (
        Attempt(model_key=keys["A"], result="tpm_limit", retry_after_s=45),
        Attempt(model_key=keys["B"], result="tpm_limit", retry_after_s=60),
    )
    assert fake.call_count == 0 and clock.sleeps == []


async def test_a_full_window_sets_no_cooldown_so_a_smaller_request_still_gets_in(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    await TpmWindow(now_ms=clock.now_ms).reserve(keys["A"], TPM_A, 2500)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)
    with pytest.raises(LLMChainExhausted):
        await collect(chain.answer_stream("answer", REQ, TENANT))  # 2500 + 1014 > 3000
    assert [k for k in await llm_keys() if "cooldown" in k] == []
    small = request(max_output_tokens=100)  # 2500 + 114 fits
    await collect(chain.answer_stream("answer", small, TENANT))
    assert fake.call_count == 1


async def test_a_full_window_takes_nothing_from_rpm_or_rpd(clock: FakeClock, keys: dict[str, str]) -> None:
    await fill(clock, keys["A"], TPM_A)
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), FakeLLM(default=ok()))
    with pytest.raises(LLMChainExhausted):
        await collect(chain.answer_stream("answer", REQ, TENANT))
    r = await _real_get_redis()
    assert await r.zcard(rpm_key(keys["A"])) == 0 and await r.zcard(rpd_key(keys["A"])) == 0


async def test_a_reservation_that_a_full_rpm_slot_then_refuses_is_given_back(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}, rpm={"A": 1}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))  # takes the one RPM slot; the window holds 13
    stream = chain.answer_stream("answer", REQ, TENANT)
    with pytest.raises(LLMChainExhausted):
        await collect(stream)
    assert [a.result for a in stream.attempts] == ["rpm_limit"]
    assert await total(keys["A"]) == 13  # the second request's reservation is gone
    assert fake.call_count == 1


# ── a request that can never fit ─────────────────────────────────────────────────────────────────────


async def test_a_request_above_the_whole_limit_is_unavailable_at_once_and_touches_nothing(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_on(monkeypatch, await _real_get_redis())
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": 1000, "B": None}), fake)
    stream = chain.answer_stream("answer", REQ, TENANT)  # weighs 1014 against 1000
    await collect(stream)
    assert [(a.model_key, a.result, a.retry_after_s) for a in stream.attempts] == [
        (keys["A"], "tpm_oversize", None),
        (keys["B"], "ok", None),
    ]
    assert [c.model_key for c in fake.calls] == [keys["B"]]
    assert clock.sleeps == [] and clock.now == FakeClock().now
    assert touched(spy, "llm:tpm:") == []  # not a window command for A
    # nothing of A's RPM, RPD or window was touched (its cooldown lookup, which comes first, is not a slot)
    assert touched(spy, rpm_key(keys["A"])) == [] and touched(spy, rpd_key(keys["A"])) == []


async def test_an_oversize_request_alone_ends_exhausted_without_waiting(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": 500}), fake)
    with pytest.raises(LLMChainExhausted) as caught:
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert caught.value.attempts == (Attempt(model_key=keys["A"], result="tpm_oversize"),)
    assert fake.call_count == 0 and clock.sleeps == [] and clock.now == FakeClock().now


async def test_oversize_is_per_request_a_lighter_one_is_served_by_the_same_model(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": 1000}), fake)
    with pytest.raises(LLMChainExhausted):
        await collect(chain.answer_stream("answer", REQ, TENANT))
    await collect(chain.answer_stream("answer", request(max_output_tokens=500), TENANT))
    assert fake.call_count == 1


# ── a fallback is checked against its own limit ──────────────────────────────────────────────────────


async def test_the_fallback_is_checked_against_its_own_limit_not_the_primarys(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    fake = FakeLLM(default=ok())
    # A can never take this request (500); B can (8000). B must not inherit A's 500.
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": 500, "B": 8000}), fake)
    stream = chain.answer_stream("answer", REQ, TENANT)
    await collect(stream)
    assert [a.result for a in stream.attempts] == ["tpm_oversize", "ok"]
    assert await total(keys["B"]) == 13 and await total(keys["A"]) == 0


async def test_the_primary_is_not_held_to_the_fallbacks_smaller_limit(clock: FakeClock, keys: dict[str, str]) -> None:
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": 8000, "B": 500}), fake)
    stream = chain.answer_stream("answer", REQ, TENANT)
    await collect(stream)
    assert [(a.model_key, a.result) for a in stream.attempts] == [(keys["A"], "ok")]
    assert await total(keys["A"]) == 13 and await members(keys["B"]) == []


async def test_a_fallback_without_tpm_is_not_limited_by_the_primarys_tpm(clock: FakeClock, keys: dict[str, str]) -> None:
    await fill(clock, keys["A"], TPM_A)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": TPM_A, "B": None}), fake)
    stream = chain.answer_stream("answer", request(max_output_tokens=5000), TENANT)  # more than A's whole limit
    await collect(stream)
    assert [a.result for a in stream.attempts] == ["tpm_oversize", "ok"]
    assert [k for k in await llm_keys() if k.startswith("llm:tpm:")] == [tpm_key(keys["A"])]


async def test_the_window_of_a_model_that_answered_is_the_one_charged(clock: FakeClock, keys: dict[str, str]) -> None:
    await fill(clock, keys["A"], TPM_A)
    fake = FakeLLM(default=ok(prompt=30, completion=70))
    chain = build(clock, make_settings([keys["A"], keys["B"]]), registry(keys, {"A": TPM_A, "B": 8000}), fake)
    await collect(chain.answer_stream("answer", REQ, TENANT))
    assert await total(keys["A"]) == TPM_A  # the full one was not touched
    assert await total(keys["B"]) == 100


# ── concurrency ──────────────────────────────────────────────────────────────────────────────────────


async def test_concurrent_streams_never_take_more_than_the_limit(
    clock: FakeClock, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    parties = 6
    spy = spy_on(monkeypatch, await _real_get_redis(), parties)
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake)

    async def one() -> str:
        try:
            await collect(chain.answer_stream("answer", REQ, TENANT))
            return "answered"
        except LLMChainExhausted as exhausted:
            assert [a.result for a in exhausted.attempts] == ["tpm_limit"]
            return "full"
        finally:
            spy.done()

    results = await asyncio.gather(*(one() for _ in range(parties)))
    assert_all_overlapped(spy.events, parties)
    assert sorted(results) == ["answered"] * 2 + ["full"] * 4  # 6 x 1014 against 3000: two fit
    assert fake.call_count == 2
    assert await total(keys["A"]) == 26  # both corrected to the reported 13


# ── Redis down: the policy of the RPM/RPD acquire ────────────────────────────────────────────────────


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
    chain = build(clock, make_settings([keys["B"]], env=env), registry(keys, {"B": TPM_A}), fake)
    with pytest.raises(HTTPException) as caught:
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "service_unavailable"  # type: ignore[index]
    assert fake.call_count == 0


async def test_redis_down_in_development_still_answers_with_a_per_process_window(
    redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, keys: dict[str, str]
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    fake = FakeLLM(default=ok())
    window = TpmWindow(now_ms=clock.now_ms)
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": TPM_A}), fake, window=window)
    with capture_logs() as logs:
        await collect(chain.answer_stream("answer", REQ, TENANT))
        await collect(chain.answer_stream("answer", REQ, TENANT))
        with pytest.raises(LLMChainExhausted) as caught:  # 26 used, +2984 does not fit in 3000
            await collect(chain.answer_stream("answer", request(max_output_tokens=2970), TENANT))
    assert fake.call_count == 2
    assert [a.result for a in caught.value.attempts] == ["tpm_limit"]
    assert any(e["event"] == "llm_tpm_redis_unavailable_dev_memory" and e["log_level"] == "error" for e in logs)


async def test_redis_down_does_not_touch_a_model_without_tpm_any_differently(
    redis_down: None, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, keys: dict[str, str]
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    fake = FakeLLM(default=ok())
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": None}), fake)
    with capture_logs() as logs:
        await collect(chain.answer_stream("answer", REQ, TENANT))
    assert not any(e["event"].startswith("llm_tpm") for e in logs)


# ── nothing a student wrote reaches the window ───────────────────────────────────────────────────────


async def test_no_prompt_or_answer_text_reaches_the_window_or_the_logs(clock: FakeClock, keys: dict[str, str]) -> None:
    canary = "CANARY-TPM-3e91c0"
    req = request(system=f"System {canary}", messages=(LLMMessage(role="user", content=f"Question {canary}"),))
    fake = FakeLLM([ok(f"Answer {canary}")])
    chain = build(clock, make_settings([keys["A"]]), registry(keys, {"A": 100_000}), fake)
    with capture_logs() as logs:
        await collect(chain.answer_stream("answer", req, TENANT))
    r = await _real_get_redis()
    stored = [f"{k}|{m}" for k in await llm_keys() if k.startswith("llm:tpm:") for m in await r.zrange(k, 0, -1)]
    assert stored and all(canary not in s for s in stored)
    assert canary not in repr(logs)
