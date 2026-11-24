"""
app/llm/transport.py
────────────────────
The part every adapter shares (docs/phase6-plan.md 7.2-7.4, 7.6): one httpx call, the four timeouts, the SSE
reading loop and the order of events. The adapters (groq.py, openrouter.py, gemini.py) only say WHAT to send
(URL, headers, body) and how ONE chunk of their stream reads (a parse function returning a `Chunk`).

Rules this module keeps:
  * the key is read from the settings object only when a call starts and goes into one request header; it is
    never in a URL, a body, an error, a repr or a log line;
  * nothing is retried here (the chain owns retries) and a redirect is never followed;
  * our own errors are raised OUTSIDE the `except` that caught the httpx error, so no httpx exception (whose
    text could hold anything) hangs on them as `__context__`;
  * timeouts: `connect` goes to httpx; first token (request sent to first token), idle (gap between two
    tokens; keep-alive comments and non-text chunks do not reset it) and total (the whole answer) are
    deadlines checked before every read. All of them end in LLMTimeout;
  * event order out: TokenEvents as they come, then at most one UsageEvent, then one DoneEvent. The Done is
    held until the stream ends because the providers send usage AFTER the finish chunk. A stream that ends
    before any finish reason is LLMUnavailable (truncated), and so is a connection that breaks half-way.
    Once a finish reason has arrived the answer is complete: a stall or a drop after it is not an error.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self

import httpx
from pydantic import SecretStr

from app.llm.errmap import ErrorContext, error_for_status
from app.llm.errors import LLMAuthError, LLMBadRequest, LLMError, LLMTimeout, LLMUnavailable
from app.llm.providers import split_model_key
from app.llm.sse import SSEError, SSEParser
from app.llm.types import (
    DoneEvent,
    FinishReason,
    LLMEvent,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)

if TYPE_CHECKING:
    from app.core.config import Settings

# A usage number above this is not believed (a broken provider must not empty the budget).
MAX_REPORTED_TOKENS: Final = 10_000_000
_MAX_KEY_LENGTH_FOR_ERRORS: Final = 160


@dataclass(frozen=True, slots=True)
class Chunk:
    """What one SSE event of a provider's stream means."""

    text: str = ""
    finish: FinishReason | None = None
    usage: tuple[int, int] | None = None  # (prompt_tokens, completion_tokens), as the provider reported them


ParseChunk = Callable[[ErrorContext, str], Chunk]


def reported_count(value: object) -> int | None:
    """A token count a provider reported, or None when it is not a plain integer in a believable range."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_REPORTED_TOKENS:
        return None
    return value


def load_json(data: str) -> object | None:
    """json.loads that answers None for text that is not JSON (the caller raises its own error)."""
    try:
        value: object = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return value


# ── one open response ────────────────────────────────────────────────────────────────────────────────


class Stream:
    """An open 200 response. `next_data()` gives the data of the next SSE event, or None at the end."""

    def __init__(self, response: httpx.Response, ctx: ErrorContext, timeouts: LLMTimeouts, started: float) -> None:
        self._bytes = response.aiter_bytes().__aiter__()
        self._ctx = ctx
        self._timeouts = timeouts
        self._started = started
        self._parser = SSEParser()
        self._queue: deque[str] = deque()
        self._eof = False
        self._last_token: float | None = None

    def mark_token(self) -> None:
        self._last_token = asyncio.get_running_loop().time()

    def _seconds_left(self) -> float:
        now = asyncio.get_running_loop().time()
        total_deadline = self._started + self._timeouts.total
        if self._last_token is None:
            wait_deadline = self._started + self._timeouts.first_token
        else:
            wait_deadline = self._last_token + self._timeouts.idle
        return min(wait_deadline, total_deadline) - now

    def _fail(self, kind: type[LLMError]) -> LLMError:
        return kind(provider=self._ctx.provider, model_key=self._ctx.model_key)

    async def next_data(self) -> str | None:
        while not self._queue:
            if self._eof:
                return None
            left = self._seconds_left()
            if left <= 0:
                raise self._fail(LLMTimeout)
            failure: LLMError | None = None
            piece: bytes | None = None
            try:
                async with asyncio.timeout(left):
                    piece = await anext(self._bytes)
            except StopAsyncIteration:
                self._eof = True
            except TimeoutError:
                failure = self._fail(LLMTimeout)
            except httpx.TimeoutException:
                failure = self._fail(LLMTimeout)
            except httpx.HTTPError:
                failure = self._fail(LLMUnavailable)
            if failure is not None:
                raise failure
            if piece is not None:
                try:
                    self._queue.extend(self._parser.feed(piece))
                except SSEError:
                    failure = self._fail(LLMUnavailable)
                if failure is not None:
                    raise failure
        return self._queue.popleft()


@asynccontextmanager
async def open_stream(
    http: httpx.AsyncClient, ctx: ErrorContext, url: str, headers: dict[str, str], body: bytes, timeouts: LLMTimeouts
) -> AsyncIterator[Stream]:
    """POST, wait for the response headers (inside the first-token limit) and check the status.
    The response is always closed when the block ends, also when the reader stops early."""
    started = asyncio.get_running_loop().time()
    request = http.build_request(
        "POST",
        url,
        headers=headers,
        content=body,
        timeout=httpx.Timeout(connect=timeouts.connect, read=timeouts.total, write=timeouts.connect, pool=timeouts.connect),
    )
    failure: LLMError | None = None
    response: httpx.Response | None = None
    try:
        async with asyncio.timeout(min(timeouts.first_token, timeouts.total)):
            response = await http.send(request, stream=True, follow_redirects=False)
    except TimeoutError:
        failure = LLMTimeout(provider=ctx.provider, model_key=ctx.model_key)
    except httpx.TimeoutException:
        failure = LLMTimeout(provider=ctx.provider, model_key=ctx.model_key)
    except httpx.HTTPError:
        failure = LLMUnavailable(provider=ctx.provider, model_key=ctx.model_key)
    if failure is not None:
        raise failure
    assert response is not None
    try:
        if response.status_code != 200:
            raise error_for_status(ctx, response.status_code, response.headers)
        yield Stream(response, ctx, timeouts, started)
    finally:
        await response.aclose()


async def read_events(
    stream: Stream, ctx: ErrorContext, parse: ParseChunk, done_sentinel: str | None
) -> AsyncIterator[LLMEvent]:
    """Turn the chunks of an open stream into tokens, one optional usage and one done."""
    finish: FinishReason | None = None
    usage: tuple[int, int] | None = None
    while True:
        try:
            data = await stream.next_data()
        except (LLMTimeout, LLMUnavailable):
            if finish is None:
                raise
            break  # the answer was complete; only the closing bytes are missing
        if data is None or (done_sentinel is not None and data == done_sentinel):
            break
        chunk = parse(ctx, data)
        if chunk.usage is not None:
            usage = chunk.usage
        if chunk.text:
            stream.mark_token()
            yield TokenEvent(text=chunk.text)
        if chunk.finish is not None and finish is None:
            finish = chunk.finish
    if finish is None:
        raise LLMUnavailable(provider=ctx.provider, model_key=ctx.model_key)
    if usage is not None:
        yield UsageEvent(prompt_tokens=usage[0], completion_tokens=usage[1], estimated=False)
    yield DoneEvent(finish_reason=finish)


# ── the base of the three adapters ───────────────────────────────────────────────────────────────────


class HTTPAdapter:
    """What the three adapters have in common. A subclass sets `provider`, `KEY_SETTING`, `DONE_SENTINEL`
    and the four hooks `_url`, `_headers`, `_body` and `_parse`."""

    provider: str  # a plain attribute, as the LLMClient protocol declares it
    KEY_SETTING: ClassVar[str]
    DONE_SENTINEL: ClassVar[str | None] = None

    __slots__ = ("_api_key", "_http", "_owns_http")

    def __init__(self, api_key: SecretStr, *, http: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._http = http
        self._owns_http = http is None

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.AsyncClient | None = None) -> Self:
        key = getattr(settings, cls.KEY_SETTING)
        return cls(api_key=key if isinstance(key, SecretStr) else SecretStr(""), http=http)

    def __repr__(self) -> str:  # no key, no URL: nothing a log line could pick up
        return f"{type(self).__name__}()"

    # ── hooks ──
    def _url(self, model_id: str, ctx: ErrorContext) -> str:
        raise NotImplementedError

    def _headers(self, key: str) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, req: LLMRequest, model_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, ctx: ErrorContext, data: str) -> Chunk:
        raise NotImplementedError

    # ── the connection ──
    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:  # one shared client per provider, HTTP/1.1 keep-alive, no event hooks (plan 7.2)
            self._http = httpx.AsyncClient(
                http2=False,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30.0),
            )
        return self._http

    async def aclose(self) -> None:
        """Closes the client this adapter made. A client handed in by the caller is the caller's to close."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # ── the call ──
    def _checked_key(self, ctx: ErrorContext) -> str:
        key = self._api_key.get_secret_value()
        if not key or not all(0x21 <= ord(c) <= 0x7E for c in key):  # empty, or not a plain header value
            raise LLMAuthError(provider=ctx.provider, model_key=ctx.model_key)
        return key

    def _context_and_model_id(self, req: LLMRequest) -> tuple[ErrorContext, str]:
        failure: LLMError | None = None
        try:
            provider, model_id = split_model_key(req.model_key)
        except ValueError:
            provider, model_id = "", ""
        if provider != self.provider or len(req.model_key) > _MAX_KEY_LENGTH_FOR_ERRORS:
            failure = LLMBadRequest(provider=self.provider)  # a key of another provider, or a malformed one
        if failure is not None:
            raise failure
        return ErrorContext(provider=self.provider, model_key=req.model_key), model_id

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        ctx, model_id = self._context_and_model_id(req)
        key = self._checked_key(ctx)
        url = self._url(model_id, ctx)
        failure: LLMError | None = None
        payload = b""
        try:
            payload = json.dumps(self._body(req, model_id), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:  # text with a lone surrogate cannot be sent as UTF-8
            failure = LLMBadRequest(provider=ctx.provider, model_key=ctx.model_key)
        if failure is not None:
            raise failure
        async with open_stream(self._get_http(), ctx, url, self._headers(key), payload, req.timeouts) as stream:
            async for event in read_events(stream, ctx, self._parse, self.DONE_SENTINEL):
                yield event
