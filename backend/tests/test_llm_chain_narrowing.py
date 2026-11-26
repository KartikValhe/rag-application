"""
tests/test_llm_chain_narrowing.py
─────────────────────────────────
The chain side of step 6a-11 (docs/phase6-plan.md 7.4, 9.1 `allowed_llm_providers`; owner decisions 2 and 3):

  - TenantLLMContext.allowed_providers narrows BOTH chains: None is the platform chain unchanged; a set keeps only the
    keys of those providers, in order. A key of a provider the tenant does not allow is skipped with the result
    `tenant_not_allowed` BEFORE any other check, and its client is never called;
  - nothing left of the answer chain: LLMChainExhausted(not_configured=True), no call; nothing left of the condense
    chain: no event, no error, `configured` False (the caller uses the original question);
  - `is_configured(purpose, ctx)` gives the same answer without starting anything;
  - every attempt carries its number (1 or 2 for the one retry) and, when a provider was called, the latency of that
    call; neither takes part in equality (older comparisons stay exact).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from app.core.config import Settings
from app.core.slots import SlotManager
from app.llm.breaker import Breaker, Cooldowns
from app.llm.chain import Attempt, LLMChain, LLMChainExhausted, TenantLLMContext
from app.llm.errors import LLMUnavailable
from app.llm.fake import FakeLLM, Script
from app.llm.models import ModelRegistry, parse_registry
from app.llm.types import LLMEvent, LLMMessage, LLMRequest, LLMTimeouts, TokenEvent, UsageEvent
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys, llm_keys  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

TENANT_ID = uuid.UUID("7a1b2c3d-0000-4000-8000-00000000beef")


@pytest.fixture
def keys() -> dict[str, str]:
    uid = uuid.uuid4().hex[:10]
    return {"A": f"groq:a-{uid}", "B": f"openrouter:b-{uid}", "C": f"groq:c-{uid}", "D": f"openrouter:d-{uid}"}


def _registry(keys: dict[str, str]) -> ModelRegistry:
    entries = []
    for name, key in keys.items():
        provider, model_id = key.split(":", 1)
        entries.append({"provider": provider, "model_id": model_id,
                        "purposes": ["condense"] if name in ("C", "D") else ["answer"], "trains_on_inputs": False,
                        "rpm": 100, "rpd": 1000, "pinned_on": "2026-11-26"})
    return parse_registry({"schema_version": 1, "models": entries})


def _settings(answer: Sequence[str], condense: Sequence[str] = ()) -> Settings:
    return Settings(_env_file=None, ENVIRONMENT="development", LLM_ANSWER_CHAIN=",".join(answer),  # type: ignore[call-arg]
                    LLM_CONDENSE_CHAIN=",".join(condense))


def _request() -> LLMRequest:
    return LLMRequest(model_key="chain:any", system="You are a careful tutor.",
                      messages=(LLMMessage(role="user", content="What is osmosis?"),), max_output_tokens=64,
                      timeouts=LLMTimeouts(connect=5, first_token=10, idle=15, total=45), request_id="req-6a11")


class PerProvider:
    """One FakeLLM per provider name, so a test sees which provider got a request."""

    def __init__(self, **fakes: FakeLLM) -> None:
        self.fakes = fakes

    def clients(self) -> dict[str, Any]:
        return dict(self.fakes)


def _ok(*tokens: str) -> Script:
    return Script(tokens=tokens or ("Hi",), usage=UsageEvent(prompt_tokens=11, completion_tokens=2, estimated=False))


def _chain(clock: FakeClock, settings: Settings, keys: dict[str, str], clients: dict[str, Any]) -> LLMChain:
    return LLMChain(clients=clients, registry=_registry(keys), settings=settings, slots=SlotManager(),
                    breaker=Breaker(now_ms=clock.now_ms), cooldowns=Cooldowns(now_ms=clock.now_ms),
                    clock=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0.0)


async def _collect(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


def _ctx(allowed: frozenset[str] | None) -> TenantLLMContext:
    return TenantLLMContext(tenant_id=TENANT_ID, allowed_providers=allowed)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ── Narrowing ─────────────────────────────────────────────────────────────────


async def test_none_is_the_platform_chain_unchanged(clock: FakeClock, keys: dict[str, str]) -> None:
    groq, openrouter = FakeLLM(default=_ok()), FakeLLM(default=_ok())
    chain = _chain(clock, _settings([keys["A"], keys["B"]]), keys, {"groq": groq, "openrouter": openrouter})
    stream = chain.answer_stream("answer", _request(), _ctx(None))
    await _collect(stream)
    assert [c.model_key for c in groq.calls] == [keys["A"]] and openrouter.calls == []
    assert [(a.model_key, a.result) for a in stream.attempts] == [(keys["A"], "ok")]


async def test_a_disallowed_provider_is_skipped_and_never_called(clock: FakeClock, keys: dict[str, str]) -> None:
    groq, openrouter = FakeLLM(default=_ok()), FakeLLM(default=_ok("Or"))
    chain = _chain(clock, _settings([keys["A"], keys["B"]]), keys, {"groq": groq, "openrouter": openrouter})
    stream = chain.answer_stream("answer", _request(), _ctx(frozenset({"openrouter"})))
    events = await _collect(stream)
    assert groq.calls == []
    assert [c.model_key for c in openrouter.calls] == [keys["B"]]
    assert [e.text for e in events if isinstance(e, TokenEvent)] == ["Or"]
    assert [(a.model_key, a.result) for a in stream.attempts] == [(keys["A"], "tenant_not_allowed"), (keys["B"], "ok")]
    assert stream.model_key == keys["B"]


async def test_the_tenant_check_comes_before_every_other_check(clock: FakeClock, keys: dict[str, str]) -> None:
    """A disallowed key takes no cooldown lookup, no breaker admission, no window and no slot."""
    groq = FakeLLM(default=_ok())
    chain = _chain(clock, _settings([keys["A"]]), keys, {"groq": groq})
    touched: list[str] = []

    async def spy_remaining(_key: str) -> int:
        touched.append("cooldown")
        return 0

    chain._cooldowns.remaining_ms = spy_remaining  # type: ignore[method-assign]
    with pytest.raises(LLMChainExhausted):
        await _collect(chain.answer_stream("answer", _request(), _ctx(frozenset({"openrouter"}))))
    assert touched == [] and groq.calls == []


@pytest.mark.parametrize("allowed", [frozenset(), frozenset({"gemini"}), frozenset({"GROQ", " groq", "fake"})])
async def test_nothing_left_of_the_answer_chain_is_exhausted_with_no_call(
    clock: FakeClock, keys: dict[str, str], allowed: frozenset[str]
) -> None:
    groq, openrouter = FakeLLM(default=_ok()), FakeLLM(default=_ok())
    chain = _chain(clock, _settings([keys["A"], keys["B"]]), keys, {"groq": groq, "openrouter": openrouter})
    assert chain.is_configured("answer", _ctx(allowed)) is False
    stream = chain.answer_stream("answer", _request(), _ctx(allowed))
    assert stream.configured is False
    with pytest.raises(LLMChainExhausted) as caught:
        await _collect(stream)
    assert caught.value.not_configured is True
    assert [a.result for a in caught.value.attempts] == ["tenant_not_allowed", "tenant_not_allowed"]
    assert groq.calls == [] and openrouter.calls == []


async def test_nothing_left_of_the_condense_chain_is_no_condense(clock: FakeClock, keys: dict[str, str]) -> None:
    groq = FakeLLM(default=_ok())
    chain = _chain(clock, _settings([keys["A"]], [keys["C"]]), keys, {"groq": groq})
    assert chain.is_configured("condense", _ctx(None)) is True
    assert chain.is_configured("condense", _ctx(frozenset({"openrouter"}))) is False
    stream = chain.answer_stream("condense", _request(), _ctx(frozenset({"openrouter"})))
    assert stream.configured is False
    assert await _collect(stream) == []
    assert groq.calls == []


async def test_the_condense_chain_is_narrowed_too(clock: FakeClock, keys: dict[str, str]) -> None:
    groq, openrouter = FakeLLM(default=_ok()), FakeLLM(default=_ok("x"))
    chain = _chain(clock, _settings([keys["A"]], [keys["C"], keys["D"]]), keys, {"groq": groq, "openrouter": openrouter})
    stream = chain.answer_stream("condense", _request(), _ctx(frozenset({"openrouter"})))
    await _collect(stream)
    assert groq.calls == [] and [c.model_key for c in openrouter.calls] == [keys["D"]]


async def test_is_configured_without_a_context_is_the_platform_chain(clock: FakeClock, keys: dict[str, str]) -> None:
    chain = _chain(clock, _settings([keys["A"]]), keys, {"groq": FakeLLM(default=_ok())})
    assert chain.is_configured("answer") is True and chain.is_configured("condense") is False
    assert chain.is_configured("answer", _ctx(frozenset({"groq"}))) is True


# ── Attempt numbers and latency ───────────────────────────────────────────────


async def test_each_call_attempt_has_its_number_and_latency_and_skips_have_none(
    clock: FakeClock, keys: dict[str, str]
) -> None:
    groq = FakeLLM([Script(error=LLMUnavailable(provider="groq")), Script(error=LLMUnavailable(provider="groq"))])
    slow = FakeLLM(default=Script(tokens=("ok",), delay_first_token_s=0.25,
                                  usage=UsageEvent(prompt_tokens=1, completion_tokens=1, estimated=False)),
                   sleep=clock.sleep)
    chain = _chain(clock, _settings([keys["A"], keys["B"]]), keys, {"groq": groq, "openrouter": slow})
    stream = chain.answer_stream("answer", _request(), _ctx(None))
    await _collect(stream)
    assert [(a.result, a.number) for a in stream.attempts] == [("unavailable", 1), ("unavailable", 2), ("ok", 1)]
    assert all(a.latency_ms is not None and a.latency_ms >= 0 for a in stream.attempts)
    assert stream.attempts[2].latency_ms == 250
    narrowed = chain.answer_stream("answer", _request(), _ctx(frozenset({"openrouter"})))
    await _collect(narrowed)
    skipped = narrowed.attempts[0]
    assert (skipped.result, skipped.latency_ms) == ("tenant_not_allowed", None)


def test_number_and_latency_take_no_part_in_equality() -> None:
    assert Attempt("groq:m", "ok", number=2, latency_ms=17) == Attempt("groq:m", "ok")
    assert Attempt("groq:m", "ok") != Attempt("groq:m", "timeout")
