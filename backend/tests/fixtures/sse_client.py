"""
tests/fixtures/sse_client.py
────────────────────────────
A small streaming ASGI client for the chat message route (step 6a-9b, plan 8.6, 8.7, F11).

httpx's ASGITransport runs the app to the end and buffers the whole body before it returns, so it can neither show
that an event arrives before the stream is finished nor disconnect in the middle. This client calls the full app
(every middleware) directly, like uvicorn does:

  - the ASGI scope says spec_version "2.3", the value uvicorn 0.54 sends (so Starlette listens for http.disconnect);
  - every body message the app sends is recorded with the time it arrived;
  - the client "disconnects" when the test sets `disconnect` or after `disconnect_after` body chunks: receive() then
    returns http.disconnect, exactly what a server does when the browser aborts the fetch. Like uvicorn, a receive()
    after the disconnect returns http.disconnect at once (so Request.is_disconnected(), which gives receive no time
    to wait, sees it);
  - `spec_version="2.4"` (step 6a-10c) is the newer ASGI spec, under which Starlette does NOT listen for the
    disconnect and relies on send() failing; this client drops sends after a disconnect instead (as uvicorn does),
    so only the route's own is_disconnected() check can notice that the browser is gone.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

SPEC_VERSION = "2.3"


@dataclass
class SseEvent:
    name: str  # "ping" for a comment line
    data: Any


@dataclass
class SseResult:
    status: int = 0
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    chunk_times: list[float] = field(default_factory=list)
    finished_at: float = 0.0
    disconnected: bool = False

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.decode("latin-1").lower() == name.lower():
                return value.decode("latin-1")
        return None

    def header_values(self, name: str) -> list[str]:
        return [v.decode("latin-1") for k, v in self.headers if k.decode("latin-1").lower() == name.lower()]

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def events(self) -> list[SseEvent]:
        return parse_sse(self.body)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.events if e.name != "ping"]

    def first(self, name: str) -> Any:
        for e in self.events:
            if e.name == name:
                return e.data
        raise AssertionError(f"no {name!r} event in {self.names}")


def parse_sse(body: bytes) -> list[SseEvent]:
    """Strict parser: every block is either one comment line or exactly `event: <name>` + `data: <one-line JSON>`."""
    text = body.decode("utf-8")
    if not text:
        return []
    assert text.endswith("\n\n"), f"the stream does not end with a blank line: {text[-40:]!r}"
    out: list[SseEvent] = []
    for block in text[:-2].split("\n\n"):
        lines = block.split("\n")
        if len(lines) == 1 and lines[0].startswith(":"):
            assert lines[0] == ": ping", lines[0]
            out.append(SseEvent("ping", None))
            continue
        assert len(lines) == 2, f"an event must be exactly two lines: {lines!r}"
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), lines
        out.append(SseEvent(lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


async def stream_request(
    app: Callable[..., Awaitable[None]],
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes,
    content_type: str = "application/json",
    disconnect: asyncio.Event | None = None,
    disconnect_after: int | None = None,
    on_chunk: Callable[[int, bytes], Awaitable[None] | None] | None = None,
    timeout: float = 20.0,
    spec_version: str = SPEC_VERSION,
) -> SseResult:
    """Run one request through `app` and return what the client saw. See the module docstring."""
    result = SseResult()
    gone = disconnect or asyncio.Event()
    done = asyncio.Event()
    body_sent = False
    raw = [(b"host", b"testserver"), (b"content-type", content_type.encode()),
           (b"content-length", str(len(body)).encode())]
    raw += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if gone.is_set():  # already gone: answered at once, like uvicorn
            result.disconnected = True
            return {"type": "http.disconnect"}
        waiters =[asyncio.ensure_future(gone.wait()), asyncio.ensure_future(done.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        result.disconnected = result.disconnected or gone.is_set()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if gone.is_set():
            return  # like uvicorn: after a disconnect, sends are dropped
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                result.chunks.append(chunk)
                result.chunk_times.append(time.monotonic())
                if on_chunk is not None:
                    maybe = on_chunk(len(result.chunks), chunk)
                    if maybe is not None:
                        await maybe
                if disconnect_after is not None and len(result.chunks) >= disconnect_after:
                    gone.set()
            if not message.get("more_body", False):
                done.set()

    await asyncio.wait_for(app(scope, receive, send), timeout)
    result.finished_at = time.monotonic()
    done.set()
    return result
