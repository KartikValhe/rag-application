"""
tests/test_chat_message_api.py
──────────────────────────────
The message route POST /api/v1/chat/conversations/{id}/messages (step 6a-9b, plan 2.1, 8.1-8.6, 9.1, 11.1).

Plan 11.1 puts these in "the message parts of test_chat_api.py"; they are a file of their own (that file holds the
conversation routes of 6a-9a and is already long), tiered db + security so the gate runs them.

Rules checked here:
  - check order (8.1): permission (403) -> chat_disabled (503) -> own conversation (404) -> scope rebuilt on EVERY
    message (404, owner decision 1) -> text checks (422) -> idempotency (409) -> limits (429/503) -> T1 -> stream;
    a refusal before the limits never uses a window;
  - T1 inserts the question and the pending assistant row in one transaction (a T1 failure leaves neither); T3
    updates THAT row (the id of `meta`); the stream format is exactly 8.3 and the stored text equals the streamed text;
  - a repeated client_request_id never creates a second assistant row (sequential, while streaming, two at once) and
    answers 409 with the existing assistant id and its outcome (a stale pending row reads as interrupted);
  - the four limits of 7.7 with Retry-After, a rejection consumes nothing, Redis down is 503, slots are released;
  - a disconnect at every stage leaves exactly one assistant row (cancelled, partial, the text the client got), no
    slot and no checked-out connection; a row left pending can be marked interrupted by app_chat_maint and nothing
    else, and a finished row cannot be touched by it (the real roles);
  - no connection is checked out while an event is sent or a slot is taken; question and answer text never reach a
    log; a router error is logged with ids, the error class and the router version only.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
import structlog
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, event, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client, slots
from app.core.authz import GENERIC_NOT_FOUND
from app.core.config import Settings, settings
from app.core.tenant_db import chat_session_context
from app.db.engine import engine as app_engine
from app.db.models.academic import ClassSubject, Enrollment, TeacherAssignment
from app.db.models.chat import Message
from app.main import app
from app.services import chat_conversations, chat_messages, chat_router, chat_schedule, chat_sse
from tests.conftest import _test_engine
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.chat_world import (
    A_LECTURES,
    ROOM_CANARY,
    NoModelSpy,
    conversation,
    conversation_row,
    messages_path,
    post_message,
    publish_timetables,
    raw_json,
    rows,
)
from tests.fixtures.documents import auth
from tests.fixtures.sse_client import SseResult, parse_sse, stream_request
from tests.security import chat_assertions as ca

pytestmark = pytest.mark.usefixtures("fixed_clock")

NOT_FOUND = {"detail": GENERIC_NOT_FOUND}
QUESTION = "what is my next lecture"
RAG_QUESTION = "what is photosynthesis"
ANSWER = "Next lecture: Monday 2026-11-23\n- 10:00-11:00 Math (10-A), room R-101, World t1"


@pytest.fixture
async def world(academic_world: AcademicWorld, raw_db: AsyncSession) -> AcademicWorld:
    await publish_timetables(raw_db, academic_world.a, A_LECTURES)
    return academic_world


def _keys(w: WorldTenant, user_id: uuid.UUID) -> dict[str, str]:
    return {
        "minute": f"{chat_messages.RATE_KEY_PREFIX}{user_id}",
        "day": f"{chat_messages.DAY_KEY_PREFIX}{user_id}",
        "active_user": f"{chat_messages.ACTIVE_USER_KEY_PREFIX}{user_id}",
        "active_tenant": f"{chat_messages.ACTIVE_TENANT_KEY_PREFIX}{w.id}",
    }


async def _counts(w: WorldTenant, user_id: uuid.UUID) -> dict[str, int]:
    return {name: await slots.default_manager.count(key) for name, key in _keys(w, user_id).items()}


def _events(resp: Any) -> list[Any]:
    return parse_sse(resp.content)


def _names(events: list[Any]) -> list[str]:
    return [e.name for e in events if e.name != "ping"]


def _text(events: list[Any]) -> str:
    return "".join(e.data["text"] for e in events if e.name == "token")


class Gate:
    """Blocks a coroutine of the turn until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def wrap(self, real: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def gated(*args: Any, **kwargs: Any) -> Any:
            self.entered.set()
            await self.release.wait()
            return await real(*args, **kwargs)

        return gated


def when_set(flag: asyncio.Event, action: Callable[[], None]) -> None:
    """Run `action` once `flag` is set, in the background. on_chunk runs inside the server's send(), and the stream
    only goes on after send() returns, so waiting for the turn inside on_chunk itself would never end."""

    async def run() -> None:
        await asyncio.wait_for(flag.wait(), 10)
        action()

    asyncio.ensure_future(run())


@pytest.fixture
def schedule_gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    gate = Gate()
    monkeypatch.setattr(chat_schedule, "answer", gate.wrap(chat_schedule.answer))
    return gate


async def _stream(w: WorldTenant, user: Any, conv_id: uuid.UUID, content: str = QUESTION, **kwargs: Any) -> SseResult:
    return await stream_request(app, "POST", messages_path(conv_id), headers=auth(w, user),
                                body=raw_json(content, kwargs.pop("client_request_id", None)), **kwargs)


# ── The happy path, T1 and T3 ─────────────────────────────────────────────────


async def test_schedule_turn_stream_format_and_the_stored_rows_equal_the_stream(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    activity_before = conv.last_message_at
    request_id = uuid.uuid4()
    resp = await post_message(client, w, w.s1, conv.id, f"  {QUESTION}​  ", request_id)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["connection"] == "keep-alive"
    assert "content-encoding" not in resp.headers
    events = _events(resp)
    assert _names(events) == ["meta", "token", "done"]
    meta, token, done = (e.data for e in events if e.name != "ping")
    assert set(meta) == {"message_id", "user_message_id", "route", "language"}
    assert meta["route"] == "schedule" and meta["language"] == "en"
    assert token == {"text": ANSWER}
    assert set(done) == {"outcome", "partial", "usage", "latency_ms"}
    assert done["outcome"] == "schedule_answered" and done["partial"] is False
    assert set(done["latency_ms"]) == {"first_token", "total"}
    assert all(isinstance(v, int) and v >= 0 for v in done["latency_ms"].values())

    question, answer = await rows(raw_db, conv.id)
    assert (question.role, question.content, question.client_request_id) == ("user", QUESTION, request_id)
    assert question.id == uuid.UUID(meta["user_message_id"]) and question.reply_to_id is None and question.outcome is None
    assert answer.id == uuid.UUID(meta["message_id"]) and answer.reply_to_id == question.id
    assert (answer.role, answer.content, answer.outcome, answer.partial) == ("assistant", ANSWER, "schedule_answered", False)
    assert answer.completed_at is not None and question.created_at < answer.created_at
    assert answer.latency_ms is not None and {"router", "total"} <= set(answer.latency_ms)
    assert answer.user_id == w.s1.id and question.user_id == w.s1.id
    c = await conversation_row(raw_db, conv.id)
    assert c.message_count == 2 and c.title == QUESTION and c.last_message_at > activity_before


async def test_meta_message_id_is_the_pending_row_that_t1_inserted(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    seen: list[tuple[str, str | None, str]] = []
    real = chat_schedule.answer

    async def look(*args: Any, **kwargs: Any) -> Any:
        seen.extend((m.role, m.outcome, m.content) for m in await rows(raw_db, conv.id))
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_schedule, "answer", look)
    resp = await post_message(client, w, w.s1, conv.id, QUESTION)
    assert seen == [("user", None, QUESTION), ("assistant", "pending", "")]
    stored = await rows(raw_db, conv.id)
    assert str(stored[1].id) == _events(resp)[0].data["message_id"] and stored[1].outcome == "schedule_answered"


async def test_a_t1_failure_leaves_neither_row_and_frees_every_slot(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(chat_messages, "_insert_assistant_row", broken)
    resp = await post_message(client, w, w.s1, conv.id, QUESTION)
    assert resp.status_code == 500, resp.text
    assert resp.json() == {"detail": {"code": "internal_error", "message": chat_messages.ERROR_MESSAGES["internal_error"]}}
    assert await rows(raw_db, conv.id) == []
    assert (await conversation_row(raw_db, conv.id)).message_count == 0
    assert await _counts(w, w.s1.id) == {"minute": 0, "day": 0, "active_user": 0, "active_tenant": 0}


async def test_the_title_is_the_first_question_only_and_counters_follow_the_rows(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    long_first = "what is my next lecture " + "please " * 20
    assert (await post_message(client, w, w.s1, conv.id, long_first)).status_code == 200
    assert (await post_message(client, w, w.s1, conv.id, "today's timetable")).status_code == 200
    c = await conversation_row(raw_db, conv.id)
    assert c.title == long_first.strip()[:60] and c.message_count == 4
    assert len(await rows(raw_db, conv.id)) == 4


async def test_with_the_rag_flag_off_a_rag_question_gets_answer_unavailable_and_no_model_call(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    """CHAT_RAG_ENABLED is false by default (6a-10c keeps it so): the RAG answer is switched off for the deployment,
    so a rag question gets the fixed 7.5 text and nothing is embedded, searched or sent to a model."""
    w = world.a
    assert settings.CHAT_RAG_ENABLED is False
    conv = await conversation(raw_db, w, w.s1)
    resp = await post_message(client, w, w.s1, conv.id, RAG_QUESTION)
    assert resp.status_code == 200
    events = _events(resp)
    assert _names(events) == ["meta", "error"]
    assert events[0].data["route"] == "rag"
    assert events[-1].data == {"code": "answer_unavailable",
                               "message": chat_messages.STREAM_ERROR_MESSAGES["en"]["answer_unavailable"],
                               "partial": False}
    assert chat_messages.STREAM_ERROR_MESSAGES["en"]["answer_unavailable"] == (
        "The assistant is not available right now. Please try again later."  # plan 7.5, fixed text
    )
    _, answer = await rows(raw_db, conv.id)
    assert (answer.route, answer.outcome, answer.content, answer.partial) == ("rag", "error", "", False)
    assert answer.rule_ids == [] and answer.router_confidence == "low"


def test_the_rag_flag_can_be_switched_on_and_stays_off_by_default() -> None:
    """Owner, 6a-10 brief answer 2: 6a-10c removes the start-up refusal; the default stays false."""
    assert Settings(CHAT_RAG_ENABLED=True).CHAT_RAG_ENABLED is True
    assert Settings().CHAT_RAG_ENABLED is False
    with pytest.raises(ValidationError):  # the other chat checks still hold
        Settings(CHAT_MAX_INPUT_CHARS=600, RETRIEVAL_MAX_QUERY_CHARS=500)


# ── Check order ───────────────────────────────────────────────────────────────


async def test_the_permission_check_comes_first(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    for target in (conv.id, uuid.uuid4()):
        resp = await post_message(client, w, w.viewer, target, "")
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "permission_denied"
    anon = await client.post(messages_path(conv.id), headers={"X-Tenant-Slug": w.tenant.slug},
                             json={"content": QUESTION, "client_request_id": str(uuid.uuid4())})
    assert anon.status_code == 401


async def test_another_users_or_an_unknown_conversation_is_404_before_any_text_check(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = world.a, world.b
    conv = await conversation(raw_db, a, a.s1)
    callers = [(a, a.admin), (a, a.t1), (a, a.t2), (a, a.s2), (b, b.admin), (b, b.s1)]
    for w, user in callers:
        for content in (QUESTION, "", "x" * 501):
            resp = await post_message(client, w, user, conv.id, content)
            assert resp.status_code == 404 and resp.json() == NOT_FOUND, (user.email, content[:5], resp.text)
        unknown = await post_message(client, w, user, uuid.uuid4(), QUESTION)
        assert unknown.status_code == 404 and unknown.json() == NOT_FOUND
        assert (await _counts(w, user.id))["minute"] == 0
    assert await rows(raw_db, conv.id) == []


async def test_the_scope_is_rebuilt_on_every_message(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Owner decision 1: the message route re-checks the CURRENT read scope on every message; history, metadata and
    delete do not (6a-9a), so the student can still read and delete their own conversation."""
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    assert (await post_message(client, w, w.s1, conv.id, QUESTION)).status_code == 200
    await raw_db.execute(delete(Enrollment).where(Enrollment.tenant_id == w.id, Enrollment.user_id == w.s1.id,
                                                  Enrollment.class_id == w.classes["10-A"].id))
    await raw_db.commit()
    resp = await post_message(client, w, w.s1, conv.id, "today's timetable")
    assert resp.status_code == 404 and resp.json() == NOT_FOUND
    assert len(await rows(raw_db, conv.id)) == 2
    assert (await _counts(w, w.s1.id))["minute"] == 1  # the refused message used no window
    headers = auth(w, w.s1)
    assert (await client.get(f"/api/v1/chat/conversations/{conv.id}", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/v1/chat/conversations/{conv.id}", headers=headers)).status_code == 204


async def test_a_removed_teacher_pair_or_a_subject_no_longer_offered_is_404_on_the_next_message(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    teacher_conv = await conversation(raw_db, w, w.t1, "10-A", "Math")
    student_conv = await conversation(raw_db, w, w.s1, "10-A", "English")
    assert (await post_message(client, w, w.t1, teacher_conv.id, QUESTION)).status_code == 200
    assert (await post_message(client, w, w.s1, student_conv.id, QUESTION)).status_code == 200
    await raw_db.execute(delete(TeacherAssignment).where(
        TeacherAssignment.tenant_id == w.id, TeacherAssignment.user_id == w.t1.id,
        TeacherAssignment.class_id == w.classes["10-A"].id, TeacherAssignment.subject_id == w.subjects["Math"].id))
    await raw_db.execute(delete(ClassSubject).where(
        ClassSubject.tenant_id == w.id, ClassSubject.class_id == w.classes["10-A"].id,
        ClassSubject.subject_id == w.subjects["English"].id))
    await raw_db.commit()
    for user, conv in ((w.t1, teacher_conv), (w.s1, student_conv)):
        resp = await post_message(client, w, user, conv.id, QUESTION)
        assert resp.status_code == 404 and resp.json() == NOT_FOUND
        assert len(await rows(raw_db, conv.id)) == 2


@pytest.mark.parametrize(("content", "code"), [
    ("", "invalid_message"),
    ("   ​‍\t ", "invalid_message"),
    ("?!... ,,, ---", "invalid_message"),
    ("x" * 501, "message_too_long"),
    ("ज" * 501, "message_too_long"),
])
async def test_text_checks_give_the_fixed_422_and_use_nothing(
    content: str, code: str, client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    resp = await post_message(client, w, w.s1, conv.id, content)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": {"code": code, "message": chat_messages.ERROR_MESSAGES[code]}}
    assert await rows(raw_db, conv.id) == []
    assert await _counts(w, w.s1.id) == {"minute": 0, "day": 0, "active_user": 0, "active_tenant": 0}


@pytest.mark.parametrize("content", ["a" * 500, "​" * 3000 + "next lecture", "  " + "b" * 500 + "  "])
async def test_the_limit_is_500_characters_after_cleaning(
    content: str, client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    resp = await post_message(client, w, w.s1, conv.id, content)
    assert resp.status_code == 200, resp.text
    assert (await rows(raw_db, conv.id))[0].content == content.replace("​", "").strip()


def test_the_input_limit_defaults_to_500_and_never_exceeds_the_retrieval_limit() -> None:
    assert type(settings).model_fields["CHAT_MAX_INPUT_CHARS"].default == 500
    with pytest.raises(ValidationError):
        Settings(CHAT_MAX_INPUT_CHARS=600, RETRIEVAL_MAX_QUERY_CHARS=500)


@pytest.mark.parametrize("body", [
    {"content": QUESTION},
    {"client_request_id": str(uuid.uuid4())},
    {"content": QUESTION, "client_request_id": "not-a-uuid"},
    {"content": QUESTION, "client_request_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4())},
    {"content": QUESTION, "client_request_id": str(uuid.uuid4()), "role": "assistant"},
    {"content": "x" * 4001, "client_request_id": str(uuid.uuid4())},
    {"content": 42, "client_request_id": str(uuid.uuid4())},
])
async def test_the_body_schema_is_strict(body: dict[str, Any], client: AsyncClient, world: AcademicWorld,
                                         raw_db: AsyncSession) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    resp = await client.post(messages_path(conv.id), headers=auth(w, w.s1), json=body)
    assert resp.status_code == 422, resp.text
    assert await rows(raw_db, conv.id) == []


async def test_chat_disabled_refuses_messages_with_503(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id, chat_enabled) VALUES (:t, false) "
                              "ON CONFLICT (tenant_id) DO UPDATE SET chat_enabled = false"), {"t": w.id})
    await raw_db.commit()
    resp = await post_message(client, w, w.s1, conv.id, QUESTION)
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"code": "chat_disabled", "message": chat_conversations.ERROR_MESSAGES["chat_disabled"]}}
    assert await rows(raw_db, conv.id) == [] and (await _counts(w, w.s1.id))["minute"] == 0


# ── Idempotency ───────────────────────────────────────────────────────────────


def _duplicate(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert set(detail) == {"code", "message", "message_id", "outcome"}
    assert detail["code"] == "duplicate_request"
    assert detail["message"] == chat_messages.ERROR_MESSAGES["duplicate_request"]
    return dict(detail)


async def test_a_repeated_request_returns_409_with_the_existing_answer_and_adds_nothing(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_MINUTE", 2)
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    first = await post_message(client, w, w.s1, conv.id, QUESTION, request_id)
    message_id = _events(first)[0].data["message_id"]
    for _ in range(4):
        detail = _duplicate(await post_message(client, w, w.s1, conv.id, QUESTION, request_id))
        assert detail["message_id"] == message_id and detail["outcome"] == "schedule_answered"
    assert len(await rows(raw_db, conv.id)) == 2 and (await conversation_row(raw_db, conv.id)).message_count == 2
    # the duplicates used no window: one more new question still fits in a window of two
    assert (await post_message(client, w, w.s1, conv.id, "today's timetable")).status_code == 200


async def test_a_duplicate_is_409_even_when_every_window_is_full(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_MINUTE", 1)
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    assert (await post_message(client, w, w.s1, conv.id, QUESTION, request_id)).status_code == 200
    assert (await post_message(client, w, w.s1, conv.id, QUESTION)).status_code == 429
    _duplicate(await post_message(client, w, w.s1, conv.id, QUESTION, request_id))


async def test_a_duplicate_while_the_first_is_still_streaming_says_pending(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate, client: AsyncClient
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    first = asyncio.create_task(_stream(w, w.s1, conv.id, client_request_id=request_id))
    await asyncio.wait_for(schedule_gate.entered.wait(), 10)
    detail = _duplicate(await post_message(client, w, w.s1, conv.id, QUESTION, request_id))
    assert detail["outcome"] == "pending"
    schedule_gate.release.set()
    result = await first
    assert result.first("meta")["message_id"] == detail["message_id"]
    after = _duplicate(await post_message(client, w, w.s1, conv.id, QUESTION, request_id))
    assert after == {**detail, "outcome": "schedule_answered"}
    assert len(await rows(raw_db, conv.id)) == 2


async def test_two_identical_requests_at_once_create_exactly_one_assistant_row(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both pass the idempotency read before either inserts (a barrier on the slot step); the unique index decides,
    the loser answers 409 with the winner's assistant id and gives back every slot it took."""
    w = world.a
    monkeypatch.setattr(settings, "CHAT_STREAMS_PER_USER", 2)
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    barrier = asyncio.Barrier(2)
    real = slots.default_manager

    class BarrierSlots:
        async def acquire(self, limits: Any, **kwargs: Any) -> Any:
            await asyncio.wait_for(barrier.wait(), 10)
            return await real.acquire(limits, **kwargs)

        async def release(self, lease: Any) -> None:
            await real.release(lease)

    monkeypatch.setattr(chat_messages, "_slots", lambda: BarrierSlots())
    one, two = await asyncio.gather(post_message(client, w, w.s1, conv.id, QUESTION, request_id),
                                    post_message(client, w, w.s1, conv.id, QUESTION, request_id))
    assert sorted((one.status_code, two.status_code)) == [200, 409], (one.text, two.text)
    winner, loser = (one, two) if one.status_code == 200 else (two, one)
    assert _duplicate(loser)["message_id"] == _events(winner)[0].data["message_id"]
    stored = await rows(raw_db, conv.id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert await _counts(w, w.s1.id) == {"minute": 1, "day": 1, "active_user": 0, "active_tenant": 0}


async def test_the_same_client_request_id_in_another_conversation_is_a_new_question(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    one, two = await conversation(raw_db, w, w.s1), await conversation(raw_db, w, w.s1, "11-A", None)
    request_id = uuid.uuid4()
    assert (await post_message(client, w, w.s1, one.id, QUESTION, request_id)).status_code == 200
    assert (await post_message(client, w, w.s1, two.id, QUESTION, request_id)).status_code == 200


@pytest.mark.parametrize(("age", "expected"), [(119, "pending"), (121, "interrupted")])
async def test_a_stale_pending_row_is_reported_as_interrupted_at_read_time(
    age: int, expected: str, client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    created = (await raw_db.execute(text("SELECT now() - make_interval(secs => :s)"), {"s": age})).scalar_one()
    question = Message(tenant_id=w.id, conversation_id=conv.id, user_id=w.s1.id, role="user", content=QUESTION,
                       client_request_id=request_id, created_at=created)
    raw_db.add(question)
    await raw_db.flush()
    answer = Message(tenant_id=w.id, conversation_id=conv.id, user_id=w.s1.id, role="assistant", content="",
                     reply_to_id=question.id, outcome="pending", created_at=created)
    raw_db.add(answer)
    await raw_db.commit()
    detail = _duplicate(await post_message(client, w, w.s1, conv.id, QUESTION, request_id))
    assert detail == {**detail, "message_id": str(answer.id), "outcome": expected}
    assert (await rows(raw_db, conv.id))[1].outcome == "pending"  # read time only: nothing was written


async def test_the_unique_index_refuses_a_second_answer_for_the_real_app_tenant_role(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    assert (await post_message(client, w, w.s1, conv.id, QUESTION)).status_code == 200
    question = (await rows(raw_db, conv.id))[0]
    with pytest.raises(IntegrityError):
        async with chat_session_context(w.id, w.s1.id) as db:
            await db.execute(insert(Message).values(
                tenant_id=w.id, conversation_id=conv.id, user_id=w.s1.id, role="assistant", content="",
                reply_to_id=question.id, outcome="pending"))
    assert len(await rows(raw_db, conv.id)) == 2


# ── Limits ────────────────────────────────────────────────────────────────────


def _limited(resp: Any, code: str) -> int:
    assert resp.status_code == 429, resp.text
    detail = resp.json()["detail"]
    assert detail == {"code": code, "message": chat_messages.ERROR_MESSAGES[code],
                      "retry_after_seconds": detail["retry_after_seconds"]}
    retry = detail["retry_after_seconds"]
    assert isinstance(retry, int) and retry >= 1 and resp.headers["Retry-After"] == str(retry)
    return int(retry)


def test_the_limit_defaults_are_the_plan_values() -> None:
    fields = type(settings).model_fields
    assert fields["CHAT_MESSAGES_PER_USER_PER_MINUTE"].default == 6
    assert fields["CHAT_MESSAGES_PER_USER_PER_DAY"].default == 40
    assert fields["CHAT_STREAMS_PER_USER"].default == 1
    assert fields["CHAT_STREAMS_PER_TENANT"].default == 10
    assert fields["CHAT_STREAM_SLOT_TTL_SECONDS"].default == 90
    assert fields["CHAT_STREAM_HEARTBEAT_SECONDS"].default == 15
    assert fields["CHAT_STREAM_MAX_SECONDS"].default == 60
    assert fields["CHAT_PENDING_STALE_SECONDS"].default == 120


async def test_per_minute_limit(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_MINUTE", 2)
    conv = await conversation(raw_db, w, w.s1)
    for _ in range(2):
        assert (await post_message(client, w, w.s1, conv.id, QUESTION)).status_code == 200
    assert _limited(await post_message(client, w, w.s1, conv.id, QUESTION), "chat_rate_limit_exceeded") <= 60
    assert len(await rows(raw_db, conv.id)) == 4
    # the window is the user's own
    other = await conversation(raw_db, w, w.s2, "10-B", None)
    assert (await post_message(client, w, w.s2, other.id, QUESTION)).status_code == 200


async def test_daily_limit(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_DAY", 2)
    conv = await conversation(raw_db, w, w.s1)
    for _ in range(2):
        assert (await post_message(client, w, w.s1, conv.id, QUESTION)).status_code == 200
    assert _limited(await post_message(client, w, w.s1, conv.id, QUESTION), "chat_daily_limit_reached") <= 86_400


async def test_one_stream_per_user_and_a_rejection_consumes_nothing(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    first = asyncio.create_task(_stream(w, w.s1, conv.id))
    await asyncio.wait_for(schedule_gate.entered.wait(), 10)
    for _ in range(3):
        _limited(await post_message(client, w, w.s1, conv.id, "today's timetable"), "chat_stream_in_progress")
    assert await _counts(w, w.s1.id) == {"minute": 1, "day": 1, "active_user": 1, "active_tenant": 1}
    schedule_gate.release.set()
    await first
    assert await _counts(w, w.s1.id) == {"minute": 1, "day": 1, "active_user": 0, "active_tenant": 0}
    assert (await post_message(client, w, w.s1, conv.id, "today's timetable")).status_code == 200
    assert len(await rows(raw_db, conv.id)) == 4


async def test_tenant_stream_cap_gives_chat_busy(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession,
                                                 schedule_gate: Gate, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_STREAMS_PER_TENANT", 1)
    conv = await conversation(raw_db, w, w.s1)
    other = await conversation(raw_db, w, w.s2, "10-B", None)
    first = asyncio.create_task(_stream(w, w.s1, conv.id))
    await asyncio.wait_for(schedule_gate.entered.wait(), 10)
    _limited(await post_message(client, w, w.s2, other.id, QUESTION), "chat_busy")
    assert await _counts(w, w.s2.id) == {"minute": 0, "day": 0, "active_user": 0, "active_tenant": 1}
    schedule_gate.release.set()
    await first
    # another tenant is not affected by tenant a's cap
    b = world.b
    b_conv = await conversation(raw_db, b, b.s1)
    assert (await post_message(client, b, b.s1, b_conv.id, QUESTION)).status_code == 200


async def test_redis_down_outside_development_is_503_and_inserts_nothing(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)

    async def broken() -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(redis_client, "get_redis", broken)
    resp = await post_message(client, w, w.s1, conv.id, QUESTION)
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"code": "service_unavailable", "message": chat_conversations.UNAVAILABLE_MESSAGE}}
    assert await rows(raw_db, conv.id) == []


# ── Body limit (8.5) ──────────────────────────────────────────────────────────


def _padded(size: int, content: str) -> bytes:
    raw = json.dumps({"content": content, "client_request_id": str(uuid.uuid4())}).encode()
    assert len(raw) <= size
    return raw[:-1] + b" " * (size - len(raw)) + b"}"


async def test_the_body_limit_is_32_kb_at_the_boundary(client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    headers = {**auth(w, w.s1), "Content-Type": "application/json"}
    at = await client.post(messages_path(conv.id), headers=headers, content=_padded(32 * 1024, QUESTION))
    assert at.status_code == 200, at.text
    over = await client.post(messages_path(conv.id), headers=headers, content=_padded(32 * 1024 + 1, QUESTION))
    assert over.status_code == 413 and over.json()["detail"]["code"] == "file_too_large"
    # the schema maximum (4,000 characters) written as \\uXXXX escapes fits under the limit, and is then too long
    escaped = json.dumps({"content": "ज" * 4000, "client_request_id": str(uuid.uuid4())}, ensure_ascii=True).encode()
    assert len(escaped) < 32 * 1024
    long = await client.post(messages_path(conv.id), headers=headers, content=escaped)
    assert long.status_code == 422 and long.json()["detail"]["code"] == "message_too_long"


# ── Heartbeat and the stream cap ──────────────────────────────────────────────


async def test_heartbeats_are_sent_while_the_answer_is_being_made(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_STREAM_HEARTBEAT_SECONDS", 0.02)
    conv = await conversation(raw_db, w, w.s1)

    async def release_after_two_pings(index: int, chunk: bytes) -> None:
        if chunk == chat_sse.PING and index >= 3:
            schedule_gate.release.set()

    result = await _stream(w, w.s1, conv.id, on_chunk=release_after_two_pings)
    names = [e.name for e in result.events]
    assert names[0] == "meta" and names[-2:] == ["token", "done"]
    assert names.count("ping") >= 2 and set(names[1:-2]) == {"ping"}
    assert result.first("token")["text"] == ANSWER


async def test_the_stream_cap_ends_with_stream_interrupted(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    monkeypatch.setattr(settings, "CHAT_STREAM_MAX_SECONDS", 0.2)
    conv = await conversation(raw_db, w, w.s1)
    result = await _stream(w, w.s1, conv.id)
    assert result.names == ["meta", "error"]
    assert result.first("error") == {"code": "stream_interrupted",
                                     "message": chat_messages.STREAM_ERROR_MESSAGES["en"]["stream_interrupted"],
                                     "partial": True}
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.partial, answer.content) == ("interrupted", True, "")
    assert (await _counts(w, w.s1.id))["active_user"] == 0
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]


# ── Cancellation at every stage (8.6) ─────────────────────────────────────────


async def _assert_clean(w: WorldTenant, user_id: uuid.UUID) -> None:
    counts = await _counts(w, user_id)
    assert counts["active_user"] == 0 and counts["active_tenant"] == 0, counts
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]


async def test_disconnect_before_meta_marks_the_t1_row_cancelled(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    gone = asyncio.Event()
    gone.set()  # the browser is gone before the first byte
    result = await _stream(w, w.s1, conv.id, disconnect=gone)
    assert result.chunks == []
    question, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.partial, answer.content) == ("cancelled", True, "")
    assert answer.completed_at is not None
    await _assert_clean(w, w.s1.id)


async def test_disconnect_while_the_answer_is_being_made(world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    gone = asyncio.Event()

    def leave_after_meta(index: int, _chunk: bytes) -> None:
        if index == 1:
            when_set(schedule_gate.entered, gone.set)

    result = await _stream(w, w.s1, conv.id, disconnect=gone, on_chunk=leave_after_meta)
    assert result.names == ["meta"]
    stored = await rows(raw_db, conv.id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert (stored[1].outcome, stored[1].partial, stored[1].content) == ("cancelled", True, "")
    assert str(stored[1].id) == result.first("meta")["message_id"]
    await _assert_clean(w, w.s1.id)


async def test_disconnect_during_persistence_keeps_the_text_the_client_got(
    world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    gate = Gate()
    monkeypatch.setattr(chat_messages, "_finish_row", gate.wrap(chat_messages._finish_row))
    gone = asyncio.Event()

    def leave_during_t3(index: int, _chunk: bytes) -> None:
        if index == 2:  # meta, token
            when_set(gate.entered, gone.set)

    result = await _stream(w, w.s1, conv.id, disconnect=gone, on_chunk=leave_during_t3)
    assert result.names == ["meta", "token"]
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.partial, answer.content) == ("cancelled", True, ANSWER)
    await _assert_clean(w, w.s1.id)


async def test_a_disconnect_after_done_changes_nothing(world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    result = await _stream(w, w.s1, conv.id, disconnect_after=3)
    assert result.names == ["meta", "token", "done"]
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.partial, answer.content) == ("schedule_answered", False, ANSWER)
    await _assert_clean(w, w.s1.id)


async def test_a_t3_failure_ends_with_internal_error_and_the_row_is_closed_as_error(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("t3 failed")

    monkeypatch.setattr(chat_messages, "_finish_row", broken)
    events = _events(await post_message(client, w, w.s1, conv.id, QUESTION))
    assert _names(events) == ["meta", "token", "error"]
    assert events[-1].data == {"code": "internal_error",
                               "message": chat_messages.STREAM_ERROR_MESSAGES["en"]["internal_error"], "partial": False}
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.content) == ("error", ANSWER)
    await _assert_clean(w, w.s1.id)


async def test_the_real_roles_cancelled_rows_are_final_and_a_left_pending_row_can_only_be_marked_interrupted(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 3. T1 and T3 run as app_tenant (the API's own login). A cancelled stream is closed as
    `cancelled` by the shielded step and app_chat_maint can no longer touch it. If that step fails too, the row stays
    `pending` (slots are still released) and app_chat_maint can mark it `interrupted` and do nothing else."""
    w = world.a
    gone = asyncio.Event()
    gone.set()
    closed = await conversation(raw_db, w, w.s1)
    await _stream(w, w.s1, closed.id, disconnect=gone)  # the schedule gate stays shut: the disconnect always wins
    closed_answer = (await rows(raw_db, closed.id))[1]
    assert closed_answer.outcome == "cancelled"

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database gone during the shielded step")

    monkeypatch.setattr(chat_messages, "_mark_unfinished", broken)
    left = await conversation(raw_db, w, w.s1, "11-A", None)
    await _stream(w, w.s1, left.id, disconnect=gone)
    left_answer = (await rows(raw_db, left.id))[1]
    assert (left_answer.outcome, left_answer.content) == ("pending", "")
    await _assert_clean(w, w.s1.id)

    mark = ("UPDATE app.messages SET outcome = 'interrupted', partial = true, completed_at = now() WHERE id = :id",
            {"id": left_answer.id})
    async with _test_engine.connect() as conn, conn.begin():
        marked = await ca.run_as(conn, "app_chat_maint", mark, tenant=w.id)
        assert marked.ok("marking the left pending row").rowcount == 1
        after = await ca.run_as(conn, "app_chat_maint", [mark, (
            "SELECT outcome, partial, content, role FROM app.messages WHERE id = :id", {"id": left_answer.id})], tenant=w.id)
        assert after.ok("read back").rows == [("interrupted", True, "", "assistant")]
        for column, value in (("content", "'x'"), ("role", "'user'"), ("route", "'rag'"), ("user_id", "gen_random_uuid()")):
            probe = await ca.run_as(conn, "app_chat_maint", (
                f"UPDATE app.messages SET {column} = {value} WHERE id = :id", {"id": left_answer.id}), tenant=w.id)
            assert probe.denied, (column, probe)
        finished = await ca.run_as(conn, "app_chat_maint", (
            "UPDATE app.messages SET outcome = 'interrupted' WHERE id = :id", {"id": closed_answer.id}), tenant=w.id)
        assert finished.ok("marking a cancelled row").rowcount == 0
        other_tenant = await ca.run_as(conn, "app_chat_maint", mark, tenant=world.b.id)
        assert other_tenant.ok("marking from another tenant").rowcount == 0


async def test_a_server_cancel_during_the_closing_step_still_closes_the_row(
    world: AcademicWorld, raw_db: AsyncSession, schedule_gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing step is shielded (plan 8.6): when an enclosing scope cancels the request while it runs (the way
    BaseHTTPMiddleware's task group or a server shutdown does), it still closes the row and releases the slots."""
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    closing = Gate()
    monkeypatch.setattr(chat_messages, "_mark_unfinished", closing.wrap(chat_messages._mark_unfinished))
    gone = asyncio.Event()
    gone.set()
    scopes: list[anyio.CancelScope] = []

    async def request() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await _stream(w, w.s1, conv.id, disconnect=gone)

    task = asyncio.ensure_future(request())
    await asyncio.wait_for(closing.entered.wait(), 10)
    scopes[0].cancel()
    await asyncio.sleep(0.05)
    closing.release.set()
    await asyncio.wait_for(task, 10)
    answer = (await rows(raw_db, conv.id))[1]
    assert (answer.outcome, answer.partial) == ("cancelled", True)
    await _assert_clean(w, w.s1.id)


# ── Router error, logs and connections ────────────────────────────────────────


async def test_a_router_error_is_logged_with_ids_error_class_and_version_only(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    no_model_calls: NoModelSpy,
) -> None:
    w = world.a
    canary = f"QCANARY{uuid.uuid4().hex}"
    conv = await conversation(raw_db, w, w.s1)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise KeyError(f"next lecture {canary}")

    monkeypatch.setattr(chat_router, "_decide", broken)
    with structlog.testing.capture_logs() as captured:
        resp = await post_message(client, w, w.s1, conv.id, f"next lecture {canary}")
    events = _events(resp)
    assert events[0].data["route"] == "rag" and _names(events) == ["meta", "error"]
    errors = [e for e in captured if e.get("event") == "chat_router_error"]
    assert len(errors) == 1
    assert set(errors[0]) == {"event", "log_level", "tenant_id", "conversation_id", "message_id", "error_class",
                              "router_version"}
    assert errors[0]["error_class"] == "KeyError" and errors[0]["router_version"] == chat_router.ROUTER_VERSION
    assert errors[0]["conversation_id"] == str(conv.id) and errors[0]["message_id"] == events[0].data["message_id"]
    assert canary not in str(captured)
    answer = (await rows(raw_db, conv.id))[1]
    assert answer.rule_ids == ["miss.router_error"]


async def test_question_and_answer_text_never_reach_any_log(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every path of this step: schedule answer (the answer holds ROOM_CANARY), rag, 422, 409, 404, 429, a cancel and
    a router error. Plan 9.5 point 2 and owner decision 2."""
    w = world.a
    question = f"when is physics QCANARY{uuid.uuid4().hex}"
    conv = await conversation(raw_db, w, w.s1)
    caplog.set_level("DEBUG")
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_MINUTE", 4)
    with structlog.testing.capture_logs() as captured:
        request_id = uuid.uuid4()
        answered = await post_message(client, w, w.s1, conv.id, "when is physics", request_id)
        assert ROOM_CANARY in _text(_events(answered))
        await post_message(client, w, w.s1, conv.id, question)
        await post_message(client, w, w.s1, conv.id, question + "x" * 500)
        await post_message(client, w, w.s1, conv.id, question, request_id)
        await post_message(client, w, w.s2, conv.id, question)
        gone = asyncio.Event()
        gone.set()
        await _stream(w, w.s1, conv.id, question, disconnect=gone)
        def broken(*_a: Any, **_k: Any) -> Any:
            raise ValueError(question)

        monkeypatch.setattr(chat_router, "_decide", broken)
        await post_message(client, w, w.s1, conv.id, question)
        await post_message(client, w, w.s1, conv.id, question)  # over the window now
    logged = str(captured) + caplog.text
    assert "QCANARY" not in logged and ROOM_CANARY not in logged
    turns = [e for e in captured if e.get("event") == "chat_turn"]
    assert turns and all("user_id" not in e for e in turns)


async def test_one_chat_turn_log_line_with_ids_and_numbers(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    with structlog.testing.capture_logs() as captured:
        events = _events(await post_message(client, w, w.s1, conv.id, QUESTION))
    turns = [e for e in captured if e.get("event") == "chat_turn"]
    assert len(turns) == 1
    turn = turns[0]
    assert set(turn) == {"event", "log_level", "tenant_id", "conversation_id", "message_id", "route", "intent",
                         "rule_ids", "outcome", "language", "partial", "latency_ms", "router_version"}
    assert turn["message_id"] == events[0].data["message_id"] and turn["outcome"] == "schedule_answered"
    assert turn["tenant_id"] == str(w.id) and turn["partial"] is False


async def test_no_connection_is_checked_out_while_an_event_is_sent_or_a_slot_is_taken(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world.a
    conv = await conversation(raw_db, w, w.s1)
    at_event: list[int] = []
    at_slot: list[int] = []
    real_event = chat_sse.event
    real_slots = slots.default_manager

    def spy_event(name: str, data: Any) -> bytes:
        at_event.append(int(app_engine.pool.checkedout()))  # type: ignore[attr-defined]
        return real_event(name, data)

    class SpySlots:
        async def acquire(self, limits: Any, **kwargs: Any) -> Any:
            at_slot.append(int(app_engine.pool.checkedout()))  # type: ignore[attr-defined]
            return await real_slots.acquire(limits, **kwargs)

        async def release(self, lease: Any) -> None:
            at_slot.append(int(app_engine.pool.checkedout()))  # type: ignore[attr-defined]
            await real_slots.release(lease)

    monkeypatch.setattr(chat_sse, "event", spy_event)
    monkeypatch.setattr(chat_messages, "_slots", lambda: SpySlots())
    peak = {"current": 0, "max": 0}

    def checkout(*_a: Any) -> None:
        peak["current"] += 1
        peak["max"] = max(peak["max"], peak["current"])

    def checkin(*_a: Any) -> None:
        peak["current"] -= 1

    event.listen(app_engine.sync_engine, "checkout", checkout)
    event.listen(app_engine.sync_engine, "checkin", checkin)
    try:
        for question in (QUESTION, RAG_QUESTION):
            assert (await post_message(client, w, w.s1, conv.id, question)).status_code == 200
    finally:
        event.remove(app_engine.sync_engine, "checkout", checkout)
        event.remove(app_engine.sync_engine, "checkin", checkin)
    assert at_event and set(at_event) == {0}, at_event
    assert at_slot and set(at_slot) == {0}, at_slot
    assert peak["max"] == 1, f"{peak['max']} connections were checked out at once"
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]


async def test_a_teacher_and_an_admin_use_their_own_conversations(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    for user in (w.t1, w.admin):
        conv = await conversation(raw_db, w, user)
        resp = await post_message(client, w, user, conv.id, QUESTION)
        assert resp.status_code == 200 and _names(_events(resp))[-1] == "done"
        stored = await rows(raw_db, conv.id)
        assert {m.user_id for m in stored} == {user.id}


async def test_the_question_is_stored_cleaned_and_another_users_rows_are_untouched(
    client: AsyncClient, world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = world.a
    mine = await conversation(raw_db, w, w.s1)
    theirs = await conversation(raw_db, w, w.s2, "10-B", None)
    assert (await post_message(client, w, w.s2, theirs.id, QUESTION)).status_code == 200
    before = [(m.id, m.content, m.outcome) for m in await rows(raw_db, theirs.id)]
    assert (await post_message(client, w, w.s1, mine.id, "  next​ lecture  ")).status_code == 200
    assert (await rows(raw_db, mine.id))[0].content == "next lecture"
    assert [(m.id, m.content, m.outcome) for m in await rows(raw_db, theirs.id)] == before
