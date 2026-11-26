"""
tests/test_chat_sse.py
──────────────────────
The SSE writer of the chat message route (step 6a-9b, plan 8.3): exact bytes of an event and of the heartbeat, one
line of JSON per event whatever the text holds, the fixed event names, and the heartbeat helper that awaits one piece
of work, pings while it waits and stops at the stream deadline. Pure: no database, Redis or network.
"""

import asyncio
import json
from typing import Any

import pytest

from app.services import chat_sse


def test_event_bytes_are_exactly_event_line_data_line_blank_line() -> None:
    out = chat_sse.event("token", {"text": "Hello"})
    assert out == b'event: token\ndata: {"text":"Hello"}\n\n'


@pytest.mark.parametrize("text", [
    "line one\nline two", "a\r\nb", "carriage\rreturn", "data: fake\n\nevent: done", "  ", "पुढचा तास",
    "tab\tand \"quotes\" and \\ backslash", "</script>", "\x00\x1f",
])
def test_any_text_stays_on_one_data_line_and_round_trips(text: str) -> None:
    out = chat_sse.event("token", {"text": text})
    assert out.endswith(b"\n\n")
    lines = out[:-2].split(b"\n")
    assert len(lines) == 2 and lines[0] == b"event: token" and lines[1].startswith(b"data: ")
    assert b"\r" not in out
    assert json.loads(lines[1][len(b"data: "):].decode("utf-8")) == {"text": text}


def test_non_ascii_text_is_sent_as_utf8_not_escaped() -> None:
    assert "पुढचा".encode() in chat_sse.event("token", {"text": "पुढचा"})


def test_only_the_plan_event_names_exist() -> None:
    assert chat_sse.EVENT_NAMES == ("meta", "token", "replace", "citations", "error", "done")
    with pytest.raises(ValueError):
        chat_sse.event("tokens", {"text": "x"})


def test_heartbeat_is_a_comment_line() -> None:
    assert chat_sse.PING == b": ping\n\n"


def test_stream_headers_and_media_type() -> None:
    assert chat_sse.MEDIA_TYPE == "text/event-stream; charset=utf-8"
    assert chat_sse.HEADERS == {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


async def test_heartbeat_while_pings_until_the_work_is_done_and_returns_its_result() -> None:
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "result"

    box: list[Any] = []
    pings = 0
    loop = asyncio.get_running_loop()
    async with chat_sse.heartbeat_while(work(), box, interval=0.01, deadline=loop.time() + 5) as beats:
        async for _ in beats:
            pings += 1
            if pings == 3:
                gate.set()
    assert box == ["result"] and pings >= 3


async def test_heartbeat_while_gives_no_ping_when_the_work_is_quick() -> None:
    async def work() -> int:
        return 7

    box: list[Any] = []
    loop = asyncio.get_running_loop()
    async with chat_sse.heartbeat_while(work(), box, interval=5, deadline=loop.time() + 5) as beats:
        pings = [b async for b in beats]
    assert pings == [] and box == [7]


async def test_heartbeat_while_raises_stream_deadline_and_cancels_the_work() -> None:
    cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    box: list[Any] = []
    loop = asyncio.get_running_loop()
    with pytest.raises(chat_sse.StreamDeadline):
        async with chat_sse.heartbeat_while(work(), box, interval=0.01, deadline=loop.time() + 0.05) as beats:
            async for _ in beats:
                pass
    assert cancelled.is_set() and box == []


async def test_heartbeat_while_passes_the_works_exception_on() -> None:
    async def work() -> None:
        raise KeyError("boom")

    loop = asyncio.get_running_loop()
    with pytest.raises(KeyError):
        async with chat_sse.heartbeat_while(work(), [], interval=1, deadline=loop.time() + 5) as beats:
            async for _ in beats:
                pass


async def test_closing_the_heartbeat_early_cancels_the_work_and_waits_for_it() -> None:
    """A client disconnect closes the stream while it waits: the work (a database transaction) must be cancelled AND
    finished (its connection given back) before the close returns."""
    finished = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    loop = asyncio.get_running_loop()
    async with chat_sse.heartbeat_while(work(), [], interval=0.01, deadline=loop.time() + 5) as beats:
        async for _ in beats:
            break
    assert finished.is_set()
