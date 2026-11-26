"""
app/services/chat_sse.py
────────────────────────
The SSE writer of the chat message route (docs/phase6-plan.md 8.3, step 6a-9b). Pure: no database, no network.

  event(name, data)   b"event: <name>\\ndata: <one-line JSON>\\n\\n" for one of the six plan events
  PING                the heartbeat comment line, sent after CHAT_STREAM_HEARTBEAT_SECONDS of quiet
  heartbeat_while     awaits one piece of work while yielding a heartbeat tick every `interval` seconds, and stops it
                      at the stream deadline (StreamDeadline)

The JSON is written with json.dumps, which escapes every line break inside a string (\\n, \\r), so whatever text an
event carries it stays on its one `data:` line and cannot start a fake event. U+2028 and U+2029 are not SSE line
breaks. Text is sent as UTF-8, not \\u-escaped.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

import anyio

EVENT_NAMES: Final = ("meta", "token", "replace", "citations", "error", "done")
PING: Final = b": ping\n\n"
MEDIA_TYPE: Final = "text/event-stream; charset=utf-8"
# Plan 8.3 and 8.7. No compression is ever applied (the app has no gzip middleware; the reverse proxy must not add one).
HEADERS: Final[dict[str, str]] = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


class StreamDeadline(Exception):  # noqa: N818 (a signal, like StopIteration)
    """The stream reached CHAT_STREAM_MAX_SECONDS while it waited for work."""


def event(name: str, data: Mapping[str, Any]) -> bytes:
    """One SSE event. Only the plan's event names exist."""
    if name not in EVENT_NAMES:
        raise ValueError("unknown SSE event name")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n".encode()


@asynccontextmanager
async def heartbeat_while[T](
    work: Awaitable[T], box: list[T], *, interval: float, deadline: float
) -> AsyncGenerator[AsyncIterator[None]]:
    """
    Run `work` as a task. The iterator it gives yields None (send a PING) after every `interval` seconds of waiting
    and ends when the work is done; the work's result is appended to `box`, its exception is raised. Past `deadline`
    (the event loop's clock) the work is cancelled and StreamDeadline is raised.

    Leaving the block for any reason (a client disconnect cancels the stream, an early break) cancels the work and
    WAITS for it to finish, shielded, so a database transaction inside it has given its connection back before the
    caller goes on.
    """
    task: asyncio.Future[T] = asyncio.ensure_future(work)
    loop = asyncio.get_running_loop()

    async def beats() -> AsyncIterator[None]:
        while True:
            remaining = max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({task}, timeout=min(interval, remaining))
            if done:
                box.append(task.result())
                return
            if deadline - loop.time() <= 0:
                raise StreamDeadline
            yield None

    try:
        yield beats()
    finally:
        if not task.done():
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(task, return_exceptions=True)
