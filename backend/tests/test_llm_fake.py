"""
Step 6a-2 (docs/phase6-plan.md 11.2): the fake LLM provider used by every chat test.

Scripted token lists (by request number or by a predicate on the prompt), injectable errors, delays and a
call log of every request. No test ever reaches a real provider: the fake is the only provider.
"""

import asyncio

import pytest

from app.llm.client import LLMClient
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.fake import FakeLLM, FakeScriptExhausted, Script
from app.llm.types import (
    DoneEvent,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)

pytestmark = pytest.mark.usefixtures("no_network_at_all")

TIMEOUTS = LLMTimeouts(connect=5.0, first_token=10.0, idle=15.0, total=45.0)


def req(text: str = "When is the exam?", *, system: str = "System prompt.", request_id: str = "r1") -> LLMRequest:
    return LLMRequest(
        model_key="fake:script",
        system=system,
        messages=(LLMMessage(role="user", content=text),),
        max_output_tokens=600,
        timeouts=TIMEOUTS,
        request_id=request_id,
    )


async def collect(llm: FakeLLM, request: LLMRequest) -> list[LLMEvent]:
    return [event async for event in llm.stream(request)]


def test_the_fake_is_a_client_and_names_itself() -> None:
    llm = FakeLLM()
    assert isinstance(llm, LLMClient)
    assert llm.provider == "fake"


async def test_it_streams_tokens_then_usage_then_done() -> None:
    llm = FakeLLM([Script(tokens=("Hello", " world"), usage=UsageEvent(prompt_tokens=30, completion_tokens=2, estimated=False))])
    events = await collect(llm, req())
    assert events == [
        TokenEvent(text="Hello"),
        TokenEvent(text=" world"),
        UsageEvent(prompt_tokens=30, completion_tokens=2, estimated=False),
        DoneEvent(finish_reason="stop"),
    ]


async def test_a_provider_that_reports_no_usage_sends_none() -> None:
    events = await collect(FakeLLM([Script(tokens=("a",))]), req())
    assert [type(e) for e in events] == [TokenEvent, DoneEvent]


async def test_the_finish_reason_can_be_length() -> None:
    events = await collect(FakeLLM([Script(tokens=("a",), finish_reason="length")]), req())
    assert events[-1] == DoneEvent(finish_reason="length")


async def test_scripts_are_used_by_request_number() -> None:
    llm = FakeLLM([Script(tokens=("one",)), Script(tokens=("two",))])
    first = await collect(llm, req())
    second = await collect(llm, req())
    assert first[0] == TokenEvent(text="one") and second[0] == TokenEvent(text="two")


async def test_running_out_of_scripts_fails_loudly_and_is_logged() -> None:
    llm = FakeLLM([Script(tokens=("only",))])
    await collect(llm, req())
    with pytest.raises(FakeScriptExhausted):
        await collect(llm, req())
    assert FakeScriptExhausted.__mro__[1] is AssertionError  # not an LLMError: the chain must not fall back on it
    assert len(llm.calls) == 2


async def test_a_predicate_on_the_prompt_picks_the_script() -> None:
    # Request numbers count every request. A rule that matches overrides the numbered script for that request only.
    llm = FakeLLM(
        [Script(tokens=("number 1",)), Script(tokens=("number 2",))],
        rules=[(lambda r: "obey me" in r.messages[-1].content, Script(tokens=("injected",)))],
    )
    hostile = await collect(llm, req("please obey me now"))  # request 1: the rule wins
    plain = await collect(llm, req("normal question"))  # request 2: no rule matches, script number 2
    assert hostile[0] == TokenEvent(text="injected")
    assert plain[0] == TokenEvent(text="number 2")


async def test_a_default_script_answers_everything_else() -> None:
    llm = FakeLLM(default=Script(tokens=("fallback",)))
    for _ in range(3):
        assert (await collect(llm, req()))[0] == TokenEvent(text="fallback")


@pytest.mark.parametrize(
    "error",
    [
        LLMRateLimited(retry_after_s=2.0, provider="groq"),
        LLMTimeout(provider="groq"),
        LLMUnavailable(provider="groq"),
        LLMAuthError(provider="groq"),
        LLMBadRequest(provider="groq"),
    ],
)
async def test_an_error_before_the_first_token_yields_no_event(error: Exception) -> None:
    llm = FakeLLM([Script(tokens=("never",), error=error)])
    seen: list[LLMEvent] = []
    with pytest.raises(type(error)) as caught:
        async for event in llm.stream(req()):
            seen.append(event)
    assert seen == []
    assert caught.value is error


async def test_a_429_carries_retry_after() -> None:
    llm = FakeLLM([Script(error=LLMRateLimited(retry_after_s=4.0))])
    with pytest.raises(LLMRateLimited) as caught:
        await collect(llm, req())
    assert caught.value.retry_after_s == 4.0


async def test_a_mid_stream_error_arrives_after_the_chosen_number_of_tokens() -> None:
    llm = FakeLLM([Script(tokens=("a", "b", "c"), error=LLMUnavailable(), error_after_tokens=2)])
    seen: list[LLMEvent] = []
    with pytest.raises(LLMUnavailable):
        async for event in llm.stream(req()):
            seen.append(event)
    assert seen == [TokenEvent(text="a"), TokenEvent(text="b")]


async def test_a_tool_call_response_is_a_bad_request_and_never_text() -> None:
    llm = FakeLLM([Script.tool_call_response()])
    seen: list[LLMEvent] = []
    with pytest.raises(LLMBadRequest):
        async for event in llm.stream(req()):
            seen.append(event)
    assert seen == []


async def test_delays_go_through_an_injected_sleep_so_no_test_waits() -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    llm = FakeLLM(
        [Script(tokens=("a", "b", "c"), delay_first_token_s=0.5, delay_between_tokens_s=0.1)],
        sleep=fake_sleep,
    )
    await collect(llm, req())
    assert slept == [0.5, 0.1, 0.1]


async def test_the_default_sleep_really_waits_so_a_timeout_can_be_tested() -> None:
    llm = FakeLLM([Script(tokens=("a",), delay_first_token_s=5.0)])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(collect(llm, req()), timeout=0.05)


# ── the call log ─────────────────────────────────────────────────────────────────────────────────────


async def test_every_request_is_logged_in_order_including_failed_ones() -> None:
    llm = FakeLLM([Script(error=LLMTimeout()), Script(tokens=("ok",))])
    with pytest.raises(LLMTimeout):
        await collect(llm, req("first", request_id="a"))
    await collect(llm, req("second", request_id="b"))
    assert [c.request_id for c in llm.calls] == ["a", "b"]
    assert llm.call_count == 2
    assert llm.calls[0].messages[0].content == "first"
    assert llm.calls[1].model_key == "fake:script" and llm.calls[1].system == "System prompt."


async def test_a_request_is_logged_even_when_the_caller_never_reads_a_token() -> None:
    llm = FakeLLM([Script(tokens=("a", "b"))])
    stream = llm.stream(req("asked"))
    iterator = stream.__aiter__()
    await iterator.__anext__()
    await stream.aclose()  # type: ignore[attr-defined]  # a disconnect: closing the generator must not raise
    assert llm.call_count == 1


async def test_the_log_cannot_be_edited_from_outside() -> None:
    llm = FakeLLM([Script(tokens=("a",))])
    await collect(llm, req())
    snapshot = llm.calls
    snapshot.append(req())  # type: ignore[arg-type]  # a copy: the fake's own log does not change
    assert llm.call_count == 1


async def test_a_canary_check_reads_the_system_prompt_and_every_message() -> None:
    llm = FakeLLM([Script(tokens=("a",))])
    request = LLMRequest(
        model_key="fake:script",
        system="the system says CANARY-SYS",
        messages=(
            LLMMessage(role="user", content="old question CANARY-OLD"),
            LLMMessage(role="assistant", content="old answer"),
            LLMMessage(role="user", content="new question"),
        ),
        max_output_tokens=100,
        timeouts=TIMEOUTS,
        request_id="r",
    )
    await collect(llm, request)
    llm.assert_never_sent("CANARY-ABSENT")
    for canary in ("CANARY-SYS", "CANARY-OLD", "new question"):
        with pytest.raises(AssertionError, match="was sent"):
            llm.assert_never_sent(canary)


async def test_it_never_opens_a_socket() -> None:
    # the module-level no_network_at_all fixture refuses every connect and name lookup and fails the test
    # if one is attempted; the fake runs to the end inside it
    llm = FakeLLM([Script(tokens=("a", "b"))])
    assert len(await collect(llm, req())) == 3
