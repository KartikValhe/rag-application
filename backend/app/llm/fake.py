"""
app/llm/fake.py
───────────────
FakeLLM, the scripted provider every chat test uses (docs/phase6-plan.md 11.2). It is the only provider
tests ever use: it opens no socket, reads no key and is NOT on the provider allow-list, so it cannot be put
into LLM_ANSWER_CHAIN.

What a test can script:
  * tokens (and the usage and finish reason) per request number, or by a predicate on the request;
  * an error before the first token, or after N tokens (a mid-stream failure);
  * delays, through an injectable `sleep`, so a test never has to wait;
  * the tool-call response of a hostile provider, which an adapter must turn into LLMBadRequest.
The call log keeps every request, including failed ones, so a test can assert on exact prompts and that a
canary never reached a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.llm.errors import LLMBadRequest, LLMError
from app.llm.types import DoneEvent, FinishReason, LLMEvent, LLMRequest, TokenEvent, UsageEvent


class FakeScriptExhausted(AssertionError):
    """More requests than scripts. An AssertionError, not an LLMError: the chain must never fall back on it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Script:
    tokens: tuple[str, ...] = ()
    usage: UsageEvent | None = None  # None: this provider reports no usage
    finish_reason: FinishReason = "stop"
    error: LLMError | None = None
    error_after_tokens: int = 0  # 0: the error comes before the first token
    delay_first_token_s: float = 0.0
    delay_between_tokens_s: float = 0.0

    @classmethod
    def tool_call_response(cls) -> Script:
        """A provider that answers with a tool call. The model was given no tools, so this is a bad request."""
        return cls(error=LLMBadRequest(provider="fake"))


Predicate = Callable[[LLMRequest], bool]
Sleep = Callable[[float], Awaitable[None]]


class FakeLLM:
    provider = "fake"

    def __init__(
        self,
        scripts: Sequence[Script] = (),
        *,
        rules: Sequence[tuple[Predicate, Script]] = (),
        default: Script | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._scripts = tuple(scripts)
        self._rules = tuple(rules)
        self._default = default
        self._sleep = sleep
        self._calls: list[LLMRequest] = []

    # ── the call log ─────────────────────────────────────────────────────────────────────────────────

    @property
    def calls(self) -> list[LLMRequest]:
        """A copy of the log: every request, in order, including the ones that failed."""
        return list(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def assert_never_sent(self, canary: str) -> None:
        """Fail if the text reached a model in the system prompt or in any message of any request."""
        for number, call in enumerate(self._calls, start=1):
            if canary in call.system or any(canary in m.content for m in call.messages):
                raise AssertionError(f"{canary!r} was sent to the model in request {number}")

    # ── the stream ───────────────────────────────────────────────────────────────────────────────────

    def _pick(self, req: LLMRequest, number: int) -> Script:
        for predicate, script in self._rules:
            if predicate(req):
                return script
        if number <= len(self._scripts):
            return self._scripts[number - 1]
        if self._default is not None:
            return self._default
        raise FakeScriptExhausted(f"FakeLLM has no script for request {number}")

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        self._calls.append(req)
        script = self._pick(req, len(self._calls))
        if script.error is not None and script.error_after_tokens == 0:
            raise script.error
        for index, text in enumerate(script.tokens):
            if script.error is not None and index == script.error_after_tokens:
                raise script.error
            delay = script.delay_first_token_s if index == 0 else script.delay_between_tokens_s
            if delay:
                await self._sleep(delay)
            yield TokenEvent(text=text)
        if script.error is not None:  # error_after_tokens is at or past the last token
            raise script.error
        if script.usage is not None:
            yield script.usage
        yield DoneEvent(finish_reason=script.finish_reason)
