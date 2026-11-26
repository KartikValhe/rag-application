"""
tests/test_sse_transport.py
───────────────────────────
F11 and plan 8.7: the chat stream through the FULL middleware stack (CORS, security headers, body guard and the
BaseHTTPMiddleware-based TenantResolutionMiddleware), driven like uvicorn drives it (tests/fixtures/sse_client.py).

  - not buffered: the `meta` event reaches the client while the answer is still being made, and every event is its own
    body message;
  - a client disconnect reaches the generator: the waiting work is cancelled, the stream ends, the shielded step closes
    the row as `cancelled` and the slots are released;
  - the response still carries the request id and the security headers.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.middleware import TenantResolutionMiddleware
from app.db.engine import engine as app_engine
from app.main import app
from app.services import chat_messages, chat_schedule
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.chat_world import (
    A_LECTURES,
    conversation,
    messages_path,
    publish_timetables,
    raw_json,
    rows,
)
from tests.fixtures.documents import auth
from tests.fixtures.sse_client import stream_request

pytestmark = pytest.mark.usefixtures("fixed_clock")


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    g = Gate()
    real = chat_schedule.answer

    async def gated(*args: Any, **kwargs: Any) -> Any:
        g.entered.set()
        try:
            await g.release.wait()
        except asyncio.CancelledError:
            g.cancelled.set()
            raise
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_schedule, "answer", gated)
    return g


def test_the_stack_under_test_is_the_real_one() -> None:
    names = [m.cls.__name__ for m in app.user_middleware]  # type: ignore[attr-defined]
    assert names == ["CORSMiddleware", "SecurityHeadersMiddleware", "BodyLimitMiddleware", "TenantResolutionMiddleware"]
    assert any(m.cls is TenantResolutionMiddleware for m in app.user_middleware)  # type: ignore[attr-defined]


async def test_meta_arrives_while_the_answer_is_still_being_made(
    academic_world: AcademicWorld, raw_db: AsyncSession, gate: Gate
) -> None:
    w = academic_world.a
    await publish_timetables(raw_db, w, A_LECTURES)
    conv = await conversation(raw_db, w, w.s1)
    chunks_while_blocked: list[int] = []
    received: list[bytes] = []

    async def watch() -> None:
        # The answer is blocked inside the gate: whatever the client holds now was not buffered until the end.
        await asyncio.wait_for(gate.entered.wait(), 10)
        await asyncio.sleep(0.05)
        chunks_while_blocked.append(len(received))
        gate.release.set()

    watcher = asyncio.ensure_future(watch())
    result = await stream_request(app, "POST", messages_path(conv.id), headers=auth(w, w.s1),
                                  body=raw_json("what is my next lecture"),
                                  on_chunk=lambda _i, chunk: received.append(chunk))
    await watcher
    assert result.status == 200
    assert chunks_while_blocked == [1], "the first event was held back until the answer was ready"
    assert received[0].startswith(b"event: meta\n")
    assert [c.split(b"\n", 1)[0] for c in result.chunks] == [b"event: meta", b"event: token", b"event: done"]
    assert result.header("x-request-id") and result.header("x-content-type-options") == "nosniff"
    assert result.header("content-type") == "text/event-stream; charset=utf-8"
    assert result.header("x-accel-buffering") == "no" and result.header("cache-control") == "no-store"


async def test_a_disconnect_reaches_the_generator_through_every_middleware(
    academic_world: AcademicWorld, raw_db: AsyncSession, gate: Gate
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    gone = asyncio.Event()

    async def leave_once_blocked() -> None:
        await asyncio.wait_for(gate.entered.wait(), 10)
        gone.set()

    leaver = asyncio.ensure_future(leave_once_blocked())

    def leave_after_meta(_index: int, _chunk: bytes) -> None:
        return None

    result = await stream_request(app, "POST", messages_path(conv.id), headers=auth(w, w.s1),
                                  body=raw_json("what is my next lecture"), disconnect=gone,
                                  on_chunk=leave_after_meta, timeout=10)
    await leaver
    assert result.disconnected and result.names == ["meta"]
    assert gate.cancelled.is_set(), "the waiting work was not cancelled: the disconnect never reached the generator"
    assert not gate.release.is_set()
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.partial) == ("cancelled", True)
    from app.core import slots

    assert await slots.default_manager.count(f"{chat_messages.ACTIVE_USER_KEY_PREFIX}{w.s1.id}") == 0
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]
