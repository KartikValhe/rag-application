"""
Step 6a-3 (docs/phase6-plan.md 7.1-7.4, 7.6, 11.1): the Groq, OpenRouter and Gemini adapters.

Everything runs on httpx.MockTransport: no socket is opened and no key is real. The key below is a canary; it
must not show up in any URL, body, error, repr, traceback or log line. The fixtures in tests/fixtures/llm/ are
hand-written from the providers' documentation, not recorded (the first real recording is step 6a-6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.llm.client import LLMClient
from app.llm.errmap import ErrorContext, error_for_status, parse_retry_after
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMContentBlocked,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.gemini import GeminiClient
from app.llm.groq import GroqClient
from app.llm.openrouter import OpenRouterClient
from app.llm.types import (
    DoneEvent,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"
CANARY = "sk-canary-7f3a9c1e5b2d4a60-must-never-appear"

MODEL_KEYS = {
    "groq": "groq:openai/gpt-oss-120b",
    "openrouter": "openrouter:openai/gpt-oss-120b",
    "gemini": "gemini:gemini-3.5-flash-lite",
}
PROVIDERS = tuple(MODEL_KEYS)
URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse",
}
OK_FIXTURE = {"groq": "groq_stream_ok.sse", "openrouter": "openrouter_stream_ok.sse", "gemini": "gemini_stream_ok.sse"}
Handler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_timeouts(**kw: float) -> LLMTimeouts:
    return LLMTimeouts(**{"connect": 5.0, "first_token": 10.0, "idle": 15.0, "total": 45.0, **kw})


def make_req(provider: str = "groq", *, model_key: str | None = None, timeouts: LLMTimeouts | None = None, **kw: Any) -> LLMRequest:
    return LLMRequest(
        **{
            "model_key": model_key or MODEL_KEYS[provider],
            "system": "You answer only from the sources.",
            "messages": (
                LLMMessage(role="user", content="Which topics are on the exam?"),
                LLMMessage(role="assistant", content="Chapter 3 and 4."),
                LLMMessage(role="user", content="परीक्षा कधी आहे?"),
            ),
            "max_output_tokens": 600,
            "timeouts": timeouts or make_timeouts(),
            "request_id": "req-internal-0001",
            **kw,
        }
    )


class Wire:
    """The fake network: records every request and answers with whatever the handler returns."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.timeouts: list[dict[str, Any]] = []
        self._handler = handler
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.timeouts.append(dict(request.extensions.get("timeout", {})))
        result = self._handler(request)
        return await result if isinstance(result, Awaitable) else result

    @property
    def body(self) -> dict[str, Any]:
        loaded = json.loads(self.requests[-1].content)
        assert isinstance(loaded, dict)
        return loaded


def adapter(provider: str, wire: Wire, key: str = CANARY) -> LLMClient:
    cls = {"groq": GroqClient, "openrouter": OpenRouterClient, "gemini": GeminiClient}[provider]
    return cls(api_key=SecretStr(key), http=wire.http)  # type: ignore[no-any-return]


def sse_response(data: bytes, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=data, headers={"content-type": "text/event-stream", **(headers or {})})


class Slow(httpx.AsyncByteStream):
    """A body that sends its parts with a pause before each one."""

    def __init__(self, parts: list[tuple[float, bytes]], *, then_raise: Exception | None = None) -> None:
        self.parts = parts
        self.then_raise = then_raise
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, part in self.parts:
            await asyncio.sleep(delay)
            yield part
        if self.then_raise is not None:
            raise self.then_raise

    async def aclose(self) -> None:
        self.closed = True


def slow_response(parts: list[tuple[float, bytes]], *, then_raise: Exception | None = None) -> tuple[httpx.Response, Slow]:
    stream = Slow(parts, then_raise=then_raise)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream), stream


async def collect(client: LLMClient, req: LLMRequest) -> tuple[list[LLMEvent], LLMError | None]:
    events: list[LLMEvent] = []
    try:
        async for event in client.stream(req):
            events.append(event)
    except LLMError as exc:
        return events, exc
    return events, None


async def must_fail(client: LLMClient, req: LLMRequest) -> LLMError:
    events, error = await collect(client, req)
    assert error is not None, f"expected an error, got {events}"
    return error


def chunk(text: str = "", finish: str | None = None, **extra: Any) -> bytes:
    """One OpenAI-style data event."""
    choice: dict[str, Any] = {"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}
    return b"data: " + json.dumps({"choices": [choice], **extra}).encode() + b"\n\n"


def gem(text: str = "", finish: str | None = None, **extra: Any) -> bytes:
    cand: dict[str, Any] = {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    return b"data: " + json.dumps({"candidates": [cand], **extra}).encode() + b"\n\n"


def stream_of(provider: str, *texts: str, finish: str = "STOP") -> bytes:
    """A complete, valid stream for any provider: the texts, then a finish."""
    if provider == "gemini":
        return b"".join(gem(t) for t in texts) + gem("", finish)
    return b"".join(chunk(t) for t in texts) + chunk("", "stop" if finish == "STOP" else "length") + b"data: [DONE]\n\n"


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# The normal answer: tokens, then usage, then done
# ═════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_recorded_style_stream_becomes_tokens_usage_done(provider: str) -> None:
    wire = Wire(lambda r: sse_response(fixture(OK_FIXTURE[provider])))
    events, error = await collect(adapter(provider, wire), make_req(provider))
    assert error is None
    assert isinstance(events[-1], DoneEvent) and events[-1].finish_reason == "stop"
    assert isinstance(events[-2], UsageEvent) and events[-2].estimated is False
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert len(tokens) == len(events) - 2  # tokens first, then exactly one usage, then done
    assert all(isinstance(e, TokenEvent) for e in events[: len(tokens)])
    text = "".join(t.text for t in tokens)
    assert {"groq": "Hello नमस्कार [S1]", "openrouter": "Hi there", "gemini": "Hello नमस्कार."}[provider] == text
    usage = events[-2]
    assert (usage.prompt_tokens, usage.completion_tokens) == {"groq": (120, 7), "openrouter": (50, 4), "gemini": (90, 9)}[provider]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_one_http_request_per_stream_and_it_is_a_post(provider: str) -> None:
    wire = Wire(lambda r: sse_response(fixture(OK_FIXTURE[provider])))
    await collect(adapter(provider, wire), make_req(provider))
    assert len(wire.requests) == 1  # no hidden retry inside the adapter (the chain owns retries)
    assert wire.requests[0].method == "POST"
    assert str(wire.requests[0].url) == URLS[provider]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_adapter_is_an_llm_client(provider: str) -> None:
    wire = Wire(lambda r: sse_response(b""))
    client = adapter(provider, wire)
    assert isinstance(client, LLMClient)
    assert client.provider == provider


async def test_a_stream_with_no_usage_gives_no_usage_event() -> None:
    wire = Wire(lambda r: sse_response(stream_of("groq", "Hi")))
    events, error = await collect(adapter("groq", wire), make_req("groq"))
    assert error is None
    assert [type(e) for e in events] == [TokenEvent, DoneEvent]  # the budget layer estimates; the adapter never invents numbers


async def test_finish_reason_length_is_passed_on() -> None:
    wire = Wire(lambda r: sse_response(stream_of("groq", "Hi", finish="length")))
    events, _ = await collect(adapter("groq", wire), make_req("groq"))
    assert events[-1] == DoneEvent(finish_reason="length")


async def test_gemini_max_tokens_is_length() -> None:
    wire = Wire(lambda r: sse_response(gem("Hi") + gem("", "MAX_TOKENS")))
    events, _ = await collect(adapter("gemini", wire), make_req("gemini"))
    assert events[-1] == DoneEvent(finish_reason="length")


async def test_usage_in_a_top_level_field_is_read_too() -> None:
    body = chunk("Hi") + chunk("", "stop", usage={"prompt_tokens": 5, "completion_tokens": 2}) + b"data: [DONE]\n\n"
    wire = Wire(lambda r: sse_response(body))
    events, _ = await collect(adapter("groq", wire), make_req("groq"))
    assert UsageEvent(prompt_tokens=5, completion_tokens=2, estimated=False) in events


@pytest.mark.parametrize("bad", [{"prompt_tokens": -1, "completion_tokens": 2}, {"prompt_tokens": "5", "completion_tokens": 2}, {"prompt_tokens": True, "completion_tokens": 1}, {"prompt_tokens": 10**12, "completion_tokens": 1}, {"prompt_tokens": 1}, "x"])
async def test_unusable_usage_is_ignored_not_trusted(bad: Any) -> None:
    body = chunk("Hi") + chunk("", "stop", usage=bad) + b"data: [DONE]\n\n"
    wire = Wire(lambda r: sse_response(body))
    events, error = await collect(adapter("groq", wire), make_req("groq"))
    assert error is None
    assert not any(isinstance(e, UsageEvent) for e in events)


async def test_reasoning_text_is_never_passed_on() -> None:
    body = (
        b'data: {"choices":[{"index":0,"delta":{"reasoning":"SECRET-THOUGHT","reasoning_content":"SECRET-THOUGHT"},"finish_reason":null}]}\n\n'
        + chunk("Answer")
        + chunk("", "stop")
        + b"data: [DONE]\n\n"
    )
    wire = Wire(lambda r: sse_response(body))
    events, _ = await collect(adapter("groq", wire), make_req("groq"))
    assert "".join(e.text for e in events if isinstance(e, TokenEvent)) == "Answer"


async def test_gemini_thought_parts_are_not_passed_on() -> None:
    body = b'data: {"candidates":[{"content":{"parts":[{"text":"SECRET-THOUGHT","thought":true},{"text":"Answer"}]},"index":0}]}\n\n' + gem("", "STOP")
    wire = Wire(lambda r: sse_response(body))
    events, _ = await collect(adapter("gemini", wire), make_req("gemini"))
    assert "".join(e.text for e in events if isinstance(e, TokenEvent)) == "Answer"


async def test_a_complete_answer_survives_a_missing_done_marker() -> None:
    wire = Wire(lambda r: sse_response(chunk("Hi") + chunk("", "stop")))  # closes right after the finish chunk
    events, error = await collect(adapter("groq", wire), make_req("groq"))
    assert error is None and events[-1] == DoneEvent(finish_reason="stop")


async def test_a_connection_that_drops_after_the_finish_chunk_still_gives_the_answer() -> None:
    response, _ = slow_response([(0, chunk("Hi") + chunk("", "stop"))], then_raise=httpx.ReadError("reset"))
    events, error = await collect(adapter("groq", Wire(lambda r: response)), make_req("groq"))
    assert error is None and events[-1] == DoneEvent(finish_reason="stop")


async def test_a_stalled_connection_after_the_finish_chunk_still_gives_the_answer() -> None:
    response, _ = slow_response([(0, chunk("Hi") + chunk("", "stop")), (5, b"")])
    req = make_req("groq", timeouts=make_timeouts(first_token=0.2, idle=0.2, total=0.5))
    events, error = await collect(adapter("groq", Wire(lambda r: response)), req)
    assert error is None and events[-1] == DoneEvent(finish_reason="stop")


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# The request: only the fields the plan lists, nothing else
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

FORBIDDEN_KEYS = {
    "tools", "tool_choice", "functions", "function_call", "response_format", "parallel_tool_calls", "toolConfig",
    "tool_config", "functionDeclarations", "responseMimeType", "responseSchema", "user", "metadata", "store",
    "stream_options", "include_reasoning", "reasoning_effort", "service_tier", "seed", "n", "stop", "logprobs",
    "safetySettings", "cachedContent", "labels", "plugins", "transforms", "models", "route", "session_id",
}
EXPECTED_BODY_KEYS = {
    "groq": {"model", "messages", "max_completion_tokens", "temperature", "stream"},
    "openrouter": {"model", "messages", "max_completion_tokens", "temperature", "stream", "provider"},
    "gemini": {"systemInstruction", "contents", "generationConfig"},
}


def all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_body_has_exactly_the_planned_fields_and_no_tool_fields(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider))
    body = wire.body
    assert set(body) == EXPECTED_BODY_KEYS[provider]
    assert not all_keys(body) & FORBIDDEN_KEYS


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_system_messages_and_settings_are_in_the_body(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider, max_output_tokens=321, temperature=0.4))
    body = wire.body
    if provider == "gemini":
        assert body["systemInstruction"] == {"parts": [{"text": "You answer only from the sources."}]}
        assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]  # Gemini calls the assistant "model"
        assert body["contents"][0] == {"role": "user", "parts": [{"text": "Which topics are on the exam?"}]}
        assert body["generationConfig"] == {"maxOutputTokens": 321, "temperature": 0.4}
        return
    assert body["messages"][0] == {"role": "system", "content": "You answer only from the sources."}
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert body["messages"][1] == {"role": "user", "content": "Which topics are on the exam?"}
    assert body["max_completion_tokens"] == 321 and body["temperature"] == 0.4
    assert body["stream"] is True
    assert body["model"] == "openai/gpt-oss-120b"
    assert "max_tokens" not in body  # deprecated name


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_marathi_text_goes_out_as_utf8_unescaped(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider))
    assert "परीक्षा".encode() in wire.requests[0].content


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_nothing_that_identifies_a_user_a_tenant_or_a_conversation_is_sent(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider, request_id="tenant-7f1c-conv-99"))
    request = wire.requests[0]
    haystack = request.content.decode() + str(request.url) + repr(dict(request.headers))
    assert "tenant-7f1c-conv-99" not in haystack and "req-internal" not in haystack


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_key_is_only_in_the_credential_header(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider))
    request = wire.requests[0]
    credential_header = "x-goog-api-key" if provider == "gemini" else "authorization"
    expected = CANARY if provider == "gemini" else f"Bearer {CANARY}"
    assert request.headers[credential_header] == expected
    assert CANARY not in str(request.url)
    assert CANARY not in request.content.decode()
    assert all(CANARY not in v for k, v in request.headers.items() if k != credential_header)
    assert "key=" not in str(request.url)  # never the ?key= form (plan 7.6)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "text/event-stream"


async def test_openrouter_always_asks_for_zero_data_retention() -> None:
    wire = Wire(lambda r: sse_response(stream_of("openrouter", "Hi")))
    client = adapter("openrouter", wire)
    for temperature in (0.0, 0.2, 2.0):
        await collect(client, make_req("openrouter", temperature=temperature))
        assert wire.body["provider"] == {"data_collection": "deny", "zdr": True}
    assert len(wire.requests) == 3


@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_only_openrouter_sends_a_provider_object(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider))
    assert "provider" not in wire.body


async def test_gemini_model_id_goes_into_the_url_and_nothing_else_does() -> None:
    wire = Wire(lambda r: sse_response(stream_of("gemini", "Hi")))
    await collect(adapter("gemini", wire), make_req("gemini", model_key="gemini:gemini-3.8-flash"))
    request = wire.requests[0]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == "/v1beta/models/gemini-3.8-flash:streamGenerateContent"
    assert request.url.query == b"alt=sse"
    assert "model" not in wire.body


@pytest.mark.parametrize("model_id", ["../../v1/files", "gemini/../x", "a:b", "a/b", "a@b", "a+b"])
async def test_gemini_refuses_a_model_id_that_could_change_the_url_path(model_id: str) -> None:
    wire = Wire(lambda r: sse_response(b""))
    error = await must_fail(adapter("gemini", wire), make_req("gemini", model_key=f"gemini:{model_id}"))
    assert isinstance(error, LLMBadRequest)
    assert wire.requests == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_model_key_of_another_provider_is_refused_without_a_request(provider: str) -> None:
    other = next(p for p in PROVIDERS if p != provider)
    wire = Wire(lambda r: sse_response(b""))
    error = await must_fail(adapter(provider, wire), make_req(other))
    assert isinstance(error, LLMBadRequest)
    assert wire.requests == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_text_that_cannot_be_encoded_is_a_bad_request_without_a_request(provider: str) -> None:
    wire = Wire(lambda r: sse_response(b""))
    req = make_req(provider, messages=(LLMMessage(role="user", content="broken \ud800 text"),))
    error = await must_fail(adapter(provider, wire), req)
    assert isinstance(error, LLMBadRequest)
    assert wire.requests == []


async def test_redirects_are_not_followed() -> None:
    wire = Wire(lambda r: httpx.Response(307, headers={"location": "https://evil.example/steal"}))
    error = await must_fail(adapter("groq", wire), make_req("groq"))
    assert isinstance(error, LLMUnavailable)
    assert len(wire.requests) == 1  # the key was not sent anywhere else


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# Keys: from settings, never leaked
# ═════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("key", ["", "   ", "bad\nkey", "bad\rkey", "café", "tab\tkey"])
async def test_a_missing_or_unusable_key_is_an_auth_error_and_nothing_is_sent(provider: str, key: str) -> None:
    wire = Wire(lambda r: sse_response(b""))
    error = await must_fail(adapter(provider, wire, key=key), make_req(provider))
    assert isinstance(error, LLMAuthError)
    assert wire.requests == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_keys_come_from_settings_as_secrets(provider: str) -> None:
    field = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
    settings = Settings(_env_file=None, **{field: CANARY})  # type: ignore[call-arg]
    cls = {"groq": GroqClient, "openrouter": OpenRouterClient, "gemini": GeminiClient}[provider]
    client = cls.from_settings(settings)
    for text in (repr(client), str(client), repr(vars(client)) if hasattr(client, "__dict__") else ""):
        assert CANARY not in text


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_from_settings_with_no_key_fails_as_auth_at_call_time(provider: str) -> None:
    cls = {"groq": GroqClient, "openrouter": OpenRouterClient, "gemini": GeminiClient}[provider]
    wire = Wire(lambda r: sse_response(b""))
    field = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
    client = cls.from_settings(Settings(_env_file=None, **{field: ""}), http=wire.http)  # type: ignore[call-arg]
    error = await must_fail(client, make_req(provider))
    assert isinstance(error, LLMAuthError) and wire.requests == []


def error_paths(provider: str) -> dict[str, Handler]:
    """Every way a call can fail. A hostile or sloppy provider echoes the key back in the body each time."""
    echo = f'{{"error":{{"message":"invalid key {CANARY}","code":401,"type":"{CANARY}"}}}}'.encode()
    slow, _ = slow_response([(0, chunk("x"))], then_raise=httpx.ReadError(CANARY))
    paths: dict[str, Handler] = {
        f"status_{status}": (lambda r, s=status: httpx.Response(s, content=echo, headers={"retry-after": "2"}))  # type: ignore[misc]
        for status in (400, 401, 402, 403, 404, 408, 413, 422, 424, 429, 498, 499, 500, 502, 503, 504, 307)
    }

    def raiser(exc: Exception) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        return handler

    paths.update(
        connect_timeout=raiser(httpx.ConnectTimeout(CANARY)),
        read_timeout=raiser(httpx.ReadTimeout(CANARY)),
        connect_error=raiser(httpx.ConnectError(CANARY)),
        protocol_error=raiser(httpx.RemoteProtocolError(CANARY)),
        decoding_error=raiser(httpx.DecodingError(CANARY)),
        mid_stream_drop=lambda r: slow,
        bad_json=lambda r: sse_response(b"data: {not json " + CANARY.encode() + b"\n\n"),
        not_utf8=lambda r: sse_response(b"data: \xff" + CANARY.encode() + b"\n\n"),
        truncated=lambda r: sse_response(chunk("x" + CANARY)),
        error_chunk=lambda r: sse_response(b'data: {"error":{"code":429,"message":"' + CANARY.encode() + b'"}}\n\n'),
        tool_call=lambda r: sse_response(b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"' + CANARY.encode() + b'"}}]}}]}\n\n'),
    )
    return paths


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_key_is_in_no_error_text_repr_args_or_traceback(provider: str) -> None:
    for name, handler in error_paths(provider).items():
        wire = Wire(handler)
        error = await must_fail(adapter(provider, wire), make_req(provider))
        shown = [str(error), repr(error), repr(error.args), "".join(traceback.format_exception(error)), repr(error.__cause__), repr(error.__context__)]
        for text in shown:
            assert CANARY not in text, f"{name}: key leaked into error output"
        assert "invalid key" not in str(error) and "ReadError" not in str(error)  # no provider text either
        assert error.provider == provider and error.model_key == MODEL_KEYS[provider]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_key_is_in_no_log_line_on_any_error_path(provider: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # the most talkative level, on every logger
    for handler in error_paths(provider).values():
        await must_fail(adapter(provider, Wire(handler)), make_req(provider))
    await collect(adapter(provider, Wire(lambda r: sse_response(fixture(OK_FIXTURE[provider])))), make_req(provider))
    assert CANARY not in caplog.text
    assert all(CANARY not in str(rec.__dict__) for rec in caplog.records)


async def test_a_provider_that_echoes_the_key_in_a_good_answer_is_not_a_problem_we_create() -> None:
    # The answer text is the provider's; the adapter adds nothing of its own to events.
    wire = Wire(lambda r: sse_response(stream_of("groq", "ok")))
    events, _ = await collect(adapter("groq", wire), make_req("groq"))
    assert all(CANARY not in repr(e) for e in events)


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# Errors: every HTTP status, in one table (plan 7.4)
# ═════════════════════════════════════════════════════════════════════════════════════════════════════

STATUS_TABLE: dict[int, tuple[type[LLMError], type[LLMError] | None]] = {
    # status: (groq / gemini, openrouter when it differs)
    400: (LLMBadRequest, None),
    401: (LLMAuthError, None),
    402: (LLMUnavailable, None),  # out of credit on our account: another provider may still work
    403: (LLMAuthError, LLMContentBlocked),  # OpenRouter: moderation or guardrail block
    404: (LLMUnavailable, None),  # model gone, or no ZDR-eligible route (plan 7.4: the chain moves on)
    408: (LLMTimeout, None),
    413: (LLMBadRequest, None),
    422: (LLMBadRequest, None),
    424: (LLMUnavailable, None),
    429: (LLMRateLimited, None),
    498: (LLMUnavailable, None),  # Groq flex tier at capacity
    499: (LLMUnavailable, None),
    500: (LLMUnavailable, None),
    502: (LLMUnavailable, None),
    503: (LLMUnavailable, None),
    504: (LLMUnavailable, None),
    418: (LLMBadRequest, None),  # any other 4xx is our fault
    307: (LLMUnavailable, None),  # redirects are never followed
    100: (LLMUnavailable, None),
    204: (LLMUnavailable, None),  # a success without a stream is not an answer
}


def expected_error(provider: str, status: int) -> type[LLMError]:
    default, openrouter = STATUS_TABLE[status]
    return openrouter if provider == "openrouter" and openrouter else default


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", sorted(STATUS_TABLE))
async def test_every_http_status_maps_to_the_planned_error(provider: str, status: int) -> None:
    wire = Wire(lambda r: httpx.Response(status, content=b'{"error":{"message":"x"}}'))
    error = await must_fail(adapter(provider, wire), make_req(provider))
    assert type(error) is expected_error(provider, status)
    assert (error.provider, error.model_key) == (provider, MODEL_KEYS[provider])


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", [500, 599])
async def test_any_5xx_is_unavailable(provider: str, status: int) -> None:
    wire = Wire(lambda r: httpx.Response(status))
    assert isinstance(await must_fail(adapter(provider, wire), make_req(provider)), LLMUnavailable)


def test_the_status_table_covers_what_the_provider_docs_list() -> None:
    documented = {400, 401, 402, 403, 404, 408, 413, 422, 424, 429, 498, 499, 500, 502, 503}  # groq + openrouter error pages
    assert documented <= set(STATUS_TABLE)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("header", "expected"),
    [("3", 3.0), ("0", 0.0), ("1.5", 1.5), ("120", 120.0), ("99999", 3600.0), ("-5", None), ("abc", None), ("nan", None), ("inf", None), ("", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None), ("1e400", None)],
)
async def test_429_carries_retry_after_in_seconds(provider: str, header: str, expected: float | None) -> None:
    wire = Wire(lambda r: httpx.Response(429, headers={"retry-after": header}))
    error = await must_fail(adapter(provider, wire), make_req(provider))
    assert isinstance(error, LLMRateLimited)
    assert error.retry_after_s == expected


async def test_429_without_retry_after_has_none() -> None:
    error = await must_fail(adapter("groq", Wire(lambda r: httpx.Response(429))), make_req("groq"))
    assert isinstance(error, LLMRateLimited) and error.retry_after_s is None


def test_parse_retry_after_directly() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("7 seconds") is None


def test_error_for_status_is_pure() -> None:
    ctx = ErrorContext(provider="groq", model_key="groq:openai/gpt-oss-120b")
    error = error_for_status(ctx, 429, {"retry-after": "4"})
    assert isinstance(error, LLMRateLimited) and error.retry_after_s == 4.0


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# Errors inside a 200 stream: malformed, blocked, tool calls, mid-stream failure
# ═════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "body",
    [
        b"data: {not json}\n\n",
        b"data: [1, 2]\n\n",
        b'data: "text"\n\n',
        b"data: \xff\xfe\n\n",
        b"data:\n\n",
        b"",  # an empty 200
        b": only a comment\n\n",
    ],
)
async def test_a_malformed_or_empty_stream_is_unavailable(provider: str, body: bytes) -> None:
    error = await must_fail(adapter(provider, Wire(lambda r: sse_response(body))), make_req(provider))
    assert isinstance(error, LLMUnavailable)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_stream_that_ends_before_the_finish_is_unavailable_after_the_partial_text(provider: str) -> None:
    body = chunk("Partial") if provider != "gemini" else gem("Partial")
    events, error = await collect(adapter(provider, Wire(lambda r: sse_response(body))), make_req(provider))
    assert isinstance(error, LLMUnavailable)
    assert events == [TokenEvent(text="Partial")]  # the student already saw it; the chain marks the answer interrupted


async def test_done_marker_without_a_finish_reason_is_unavailable() -> None:
    error = await must_fail(adapter("groq", Wire(lambda r: sse_response(chunk("Hi") + b"data: [DONE]\n\n"))), make_req("groq"))
    assert isinstance(error, LLMUnavailable)


async def test_an_event_that_is_cut_off_is_not_an_answer() -> None:
    body = chunk("Hi") + b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'  # no blank line
    error = await must_fail(adapter("groq", Wire(lambda r: sse_response(body))), make_req("groq"))
    assert isinstance(error, LLMUnavailable)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_connection_that_drops_after_some_tokens_is_unavailable_with_the_tokens_kept(provider: str) -> None:
    first = chunk("One") if provider != "gemini" else gem("One")
    response, _ = slow_response([(0, first)], then_raise=httpx.ReadError("connection reset"))
    events, error = await collect(adapter(provider, Wire(lambda r: response)), make_req(provider))
    assert isinstance(error, LLMUnavailable)
    assert events == [TokenEvent(text="One")]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_line_that_never_ends_is_unavailable(provider: str) -> None:
    response, _ = slow_response([(0, b"data: " + b"x" * 400_000)])
    assert isinstance(await must_fail(adapter(provider, Wire(lambda r: response)), make_req(provider)), LLMUnavailable)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"choices":[{"delta":{"tool_calls":[{"id":"1","function":{"name":"x","arguments":"{}"}}]},"finish_reason":null}]}',
        b'{"choices":[{"delta":{"function_call":{"name":"x"}},"finish_reason":null}]}',
        b'{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        b'{"choices":[{"delta":{},"finish_reason":"function_call"}]}',
    ],
)
@pytest.mark.parametrize("provider", ["groq", "openrouter"])
async def test_a_tool_call_in_the_answer_is_a_bad_request(provider: str, payload: bytes) -> None:
    # The model was given no tools (plan 5.6), so a tool call is something the plan never allows.
    error = await must_fail(adapter(provider, Wire(lambda r: sse_response(b"data: " + payload + b"\n\n"))), make_req(provider))
    assert isinstance(error, LLMBadRequest)


@pytest.mark.parametrize("finish", ["MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS"])
async def test_gemini_tool_finishes_are_a_bad_request(finish: str) -> None:
    error = await must_fail(adapter("gemini", Wire(lambda r: sse_response(gem("", finish)))), make_req("gemini"))
    assert isinstance(error, LLMBadRequest)


async def test_gemini_function_call_part_is_a_bad_request() -> None:
    body = b'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"x","args":{}}}]},"index":0}]}\n\n'
    assert isinstance(await must_fail(adapter("gemini", Wire(lambda r: sse_response(body))), make_req("gemini")), LLMBadRequest)


@pytest.mark.parametrize("provider", ["groq", "openrouter"])
async def test_content_filter_finish_is_blocked(provider: str) -> None:
    body = chunk("Par") + chunk("", "content_filter")
    events, error = await collect(adapter(provider, Wire(lambda r: sse_response(body))), make_req(provider))
    assert isinstance(error, LLMContentBlocked)
    assert events == [TokenEvent(text="Par")]


@pytest.mark.parametrize("finish", ["SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION", "IMAGE_SAFETY"])
async def test_gemini_blocking_finishes_are_blocked(finish: str) -> None:
    assert isinstance(await must_fail(adapter("gemini", Wire(lambda r: sse_response(gem("", finish)))), make_req("gemini")), LLMContentBlocked)


async def test_gemini_blocked_prompt_is_blocked_with_no_tokens() -> None:
    events, error = await collect(adapter("gemini", Wire(lambda r: sse_response(fixture("gemini_blocked_prompt.sse")))), make_req("gemini"))
    assert isinstance(error, LLMContentBlocked) and events == []


@pytest.mark.parametrize("finish", ["OTHER", "LANGUAGE", "MALFORMED_RESPONSE", "SOMETHING_NEW"])
async def test_gemini_other_finishes_are_unavailable(finish: str) -> None:
    assert isinstance(await must_fail(adapter("gemini", Wire(lambda r: sse_response(gem("", finish)))), make_req("gemini")), LLMUnavailable)


async def test_gemini_unspecified_block_reason_is_not_a_block() -> None:
    body = b'data: {"promptFeedback":{"blockReason":"BLOCK_REASON_UNSPECIFIED"}}\n\n' + gem("Hi") + gem("", "STOP")
    events, error = await collect(adapter("gemini", Wire(lambda r: sse_response(body))), make_req("gemini"))
    assert error is None and events[0] == TokenEvent(text="Hi")


async def test_an_unknown_finish_reason_is_unavailable() -> None:
    assert isinstance(await must_fail(adapter("groq", Wire(lambda r: sse_response(chunk("Hi", "abort")))), make_req("groq")), LLMUnavailable)


async def test_openrouter_midstream_error_keeps_the_partial_text_and_maps_the_code() -> None:
    events, error = await collect(adapter("openrouter", Wire(lambda r: sse_response(fixture("openrouter_midstream_error.sse")))), make_req("openrouter"))
    assert isinstance(error, LLMUnavailable)  # code 502 in the error object
    assert events == [TokenEvent(text="Partial")]


@pytest.mark.parametrize(
    ("code", "expected"),
    [(429, LLMRateLimited), (401, LLMAuthError), (400, LLMBadRequest), (503, LLMUnavailable), (408, LLMTimeout), ("x", LLMUnavailable), (None, LLMUnavailable), (True, LLMUnavailable), (99999, LLMUnavailable)],
)
@pytest.mark.parametrize("provider", ["groq", "openrouter"])
async def test_an_error_object_in_a_chunk_maps_by_its_code(provider: str, code: Any, expected: type[LLMError]) -> None:
    body = chunk("Hi") + b"data: " + json.dumps({"error": {"code": code, "message": "m"}}).encode() + b"\n\n"
    events, error = await collect(adapter(provider, Wire(lambda r: sse_response(body))), make_req(provider))
    assert type(error) is expected
    assert events == [TokenEvent(text="Hi")]


@pytest.mark.parametrize("error_value", ["boom", ["x"], 5])
async def test_an_error_that_is_not_an_object_is_still_an_error(error_value: Any) -> None:
    body = b"data: " + json.dumps({"error": error_value}).encode() + b"\n\n"
    assert isinstance(await must_fail(adapter("groq", Wire(lambda r: sse_response(body))), make_req("groq")), LLMUnavailable)


@pytest.mark.parametrize(
    ("status_name", "expected"),
    [("RESOURCE_EXHAUSTED", LLMRateLimited), ("UNAVAILABLE", LLMUnavailable), ("DEADLINE_EXCEEDED", LLMUnavailable), ("INVALID_ARGUMENT", LLMBadRequest), ("PERMISSION_DENIED", LLMAuthError), ("UNAUTHENTICATED", LLMAuthError), ("rate_limit_exceeded", LLMRateLimited), ("quota_exceeded", LLMRateLimited), ("authentication", LLMAuthError), ("brand_new_code", LLMUnavailable)],
)
async def test_gemini_error_names_map_like_status_codes(status_name: str, expected: type[LLMError]) -> None:
    body = b"data: " + json.dumps({"error": {"code": status_name, "status": status_name, "message": "m"}}).encode() + b"\n\n"
    assert type(await must_fail(adapter("gemini", Wire(lambda r: sse_response(body))), make_req("gemini"))) is expected


@pytest.mark.parametrize("provider", ["groq", "openrouter"])
@pytest.mark.parametrize("bad", [b'{"choices":"x"}', b'{"choices":[1]}', b'{"choices":[{"delta":"x"}]}', b'{"choices":[{"delta":{"content":5}}]}', b'{"choices":[{"delta":{"content":{"a":1}}}]}'])
async def test_chunks_of_the_wrong_shape_are_unavailable(provider: str, bad: bytes) -> None:
    assert isinstance(await must_fail(adapter(provider, Wire(lambda r: sse_response(b"data: " + bad + b"\n\n"))), make_req(provider)), LLMUnavailable)


async def test_a_chunk_with_nothing_we_know_is_skipped() -> None:
    body = b'data: {"id":"x","object":"ping"}\n\n' + chunk("Hi") + chunk("", "stop") + b"data: [DONE]\n\n"
    events, error = await collect(adapter("groq", Wire(lambda r: sse_response(body))), make_req("groq"))
    assert error is None and events[0] == TokenEvent(text="Hi")


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# Timeouts: connect, first token, idle, total (plan 7.4)
# ═════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_connect_timeout_goes_to_httpx_and_the_others_cannot_be_longer_than_total(provider: str) -> None:
    wire = Wire(lambda r: sse_response(stream_of(provider, "Hi")))
    await collect(adapter(provider, wire), make_req(provider, timeouts=make_timeouts(connect=3.0, first_token=7.0, idle=9.0, total=21.0)))
    seen = wire.timeouts[0]
    assert seen["connect"] == 3.0
    assert seen["pool"] == 3.0 and seen["write"] == 3.0
    assert seen["read"] == 21.0  # our own deadlines are tighter; this only bounds a single read


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t")])
async def test_httpx_timeouts_are_llm_timeouts(provider: str, exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    assert isinstance(await must_fail(adapter(provider, Wire(handler)), make_req(provider)), LLMTimeout)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("exc", [httpx.ConnectError("c"), httpx.ReadError("r"), httpx.RemoteProtocolError("p"), httpx.DecodingError("d"), httpx.UnsupportedProtocol("u"), httpx.TooManyRedirects("m")])
async def test_other_transport_failures_are_unavailable(provider: str, exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    assert isinstance(await must_fail(adapter(provider, Wire(handler)), make_req(provider)), LLMUnavailable)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_first_token_timeout_when_the_headers_are_slow(provider: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return sse_response(stream_of(provider, "Hi"))

    req = make_req(provider, timeouts=make_timeouts(first_token=0.1, idle=0.2, total=1.0))
    events, error = await collect(adapter(provider, Wire(handler)), req)
    assert isinstance(error, LLMTimeout) and events == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_first_token_timeout_when_the_body_is_slow(provider: str) -> None:
    first = chunk("Hi") if provider != "gemini" else gem("Hi")
    response, stream = slow_response([(2, first)])
    req = make_req(provider, timeouts=make_timeouts(first_token=0.1, idle=0.2, total=1.0))
    events, error = await collect(adapter(provider, Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout) and events == []
    assert stream.closed  # the connection was let go


async def test_comment_lines_do_not_count_as_the_first_token() -> None:
    # OpenRouter sends ": OPENROUTER PROCESSING" while it waits for a provider. That is not a token.
    response, _ = slow_response([(0.05, b": OPENROUTER PROCESSING\n\n")] * 8 + [(0, chunk("Hi"))])
    req = make_req("openrouter", timeouts=make_timeouts(first_token=0.2, idle=0.5, total=2.0))
    events, error = await collect(adapter("openrouter", Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout) and events == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_idle_timeout_between_tokens_keeps_the_tokens_already_sent(provider: str) -> None:
    make = chunk if provider != "gemini" else gem
    response, stream = slow_response([(0, make("One")), (2, make("Two"))])
    req = make_req(provider, timeouts=make_timeouts(first_token=0.5, idle=0.15, total=3.0))
    events, error = await collect(adapter(provider, Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout)
    assert events == [TokenEvent(text="One")]
    assert stream.closed


THINKING = b'data: {"choices":[{"index":0,"delta":{"reasoning":"hmm"},"finish_reason":null}]}\n\n'


async def test_chunks_with_no_answer_text_do_not_reset_either_clock() -> None:
    # A reasoning model streams its thinking first. That is not a token for the student (plan 7.4: first
    # token, gap between tokens), so it must neither satisfy the first-token limit nor reset the idle one.
    response, _ = slow_response([(0.05, THINKING)] * 8 + [(0, chunk("Hi"))])
    req = make_req("groq", timeouts=make_timeouts(first_token=0.2, idle=0.5, total=2.0))
    events, error = await collect(adapter("groq", Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout) and events == []
    response, _ = slow_response([(0, chunk("One"))] + [(0.05, THINKING)] * 10 + [(0, chunk("Two"))])
    req = make_req("groq", timeouts=make_timeouts(first_token=0.5, idle=0.2, total=3.0))
    events, error = await collect(adapter("groq", Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout) and events == [TokenEvent(text="One")]


async def test_comment_lines_do_not_reset_the_idle_clock() -> None:
    response, _ = slow_response([(0, chunk("One"))] + [(0.05, b": OPENROUTER PROCESSING\n\n")] * 10 + [(0, chunk("Two"))])
    req = make_req("openrouter", timeouts=make_timeouts(first_token=0.5, idle=0.2, total=3.0))
    events, error = await collect(adapter("openrouter", Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout) and events == [TokenEvent(text="One")]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_total_timeout_stops_a_stream_that_keeps_sending_tokens(provider: str) -> None:
    make = chunk if provider != "gemini" else gem
    response, stream = slow_response([(0.05, make("t"))] * 40)
    req = make_req(provider, timeouts=make_timeouts(first_token=0.3, idle=0.3, total=0.4))
    events, error = await collect(adapter(provider, Wire(lambda r: response)), req)
    assert isinstance(error, LLMTimeout)
    assert 1 <= len(events) < 40 and all(isinstance(e, TokenEvent) for e in events)
    assert stream.closed


async def test_a_fast_stream_is_not_cut_by_generous_timeouts() -> None:
    response, _ = slow_response([(0.01, chunk("a")), (0.01, chunk("b")), (0.01, chunk("", "stop")), (0, b"data: [DONE]\n\n")])
    req = make_req("groq", timeouts=make_timeouts(first_token=1.0, idle=1.0, total=2.0))
    events, error = await collect(adapter("groq", Wire(lambda r: response)), req)
    assert error is None and events[-1] == DoneEvent(finish_reason="stop")


# ═════════════════════════════════════════════════════════════════════════════════════════════════════
# Connection handling
# ═════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_the_response_is_closed_when_the_reader_stops_early() -> None:
    response, stream = slow_response([(0, chunk("One")), (0, chunk("Two")), (0, chunk("", "stop"))])
    agen = adapter("groq", Wire(lambda r: response)).stream(make_req("groq"))
    first = await anext(agen)
    assert first == TokenEvent(text="One")
    await agen.aclose()  # type: ignore[attr-defined]
    assert stream.closed


async def test_the_response_is_closed_after_an_error_status() -> None:
    stream = Slow([(0, b'{"error":{}}')])
    wire = Wire(lambda r: httpx.Response(500, stream=stream))
    await must_fail(adapter("groq", wire), make_req("groq"))
    assert stream.closed


async def test_an_injected_client_is_not_closed_by_the_adapter() -> None:
    wire = Wire(lambda r: sse_response(stream_of("groq", "Hi")))
    client = adapter("groq", wire)
    await collect(client, make_req("groq"))
    await client.aclose()  # type: ignore[attr-defined]
    assert not wire.http.is_closed


async def test_an_owned_client_is_closed_by_aclose_and_is_made_once() -> None:
    client = GroqClient(api_key=SecretStr(CANARY))
    first = client._get_http()  # noqa: SLF001
    assert client._get_http() is first  # noqa: SLF001  one shared client per provider (plan 7.2)
    assert first.follow_redirects is False
    assert not first.event_hooks["request"] and not first.event_hooks["response"]
    await client.aclose()
    assert first.is_closed
