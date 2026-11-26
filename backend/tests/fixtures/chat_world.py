"""
tests/fixtures/chat_world.py
────────────────────────────
Shared setup for the message route tests (step 6a-9b): published timetables on the academic world, a fixed clock,
conversations, request helpers and spies that prove that no embedding, retrieval or LLM call happens.

The clock: Monday 2026-11-23 09:30 in Asia/Kolkata (the world tenants' timezone), so "next lecture" is the 10:00 one.

Published timetables (tenant a; tenant b has its own 10-A timetable with a canary room, never visible to tenant a):
  10-A  Mon 10:00-11:00 Math (T1, room R-101)    Mon 11:00-12:00 Physics (T1, room <ROOM_CANARY>)
        Tue 09:00-10:00 English (no teacher, no room)
  10-B  Mon 10:00-11:00 Physics (T2, room R-201)
  11-A  Mon 12:00-13:00 Chemistry (T2, room R-301)    Wed 09:00-10:00 English (no teacher, room R-302)
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import Conversation, Message
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth

CONV = "/api/v1/chat/conversations"
# Monday 2026-11-23 09:30 Asia/Kolkata
FIXED_NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
ROOM_CANARY = f"ROOM-{uuid.uuid4().hex[:10]}"
B_ROOM_CANARY = f"BROOM-{uuid.uuid4().hex[:10]}"


def messages_path(conversation_id: uuid.UUID | str) -> str:
    return f"{CONV}/{conversation_id}/messages"


def message_body(content: str, client_request_id: uuid.UUID | str | None = None) -> dict[str, Any]:
    return {"content": content, "client_request_id": str(client_request_id or uuid.uuid4())}


@dataclass(frozen=True)
class Lecture:
    class_name: str
    day: int
    start: str
    end: str
    subject: str
    teacher: str | None
    room: str | None


A_LECTURES = (
    Lecture("10-A", 0, "10:00", "11:00", "Math", "t1", "R-101"),
    Lecture("10-A", 0, "11:00", "12:00", "Physics", "t1", ROOM_CANARY),
    Lecture("10-A", 1, "09:00", "10:00", "English", None, None),
    Lecture("10-B", 0, "10:00", "11:00", "Physics", "t2", "R-201"),
    Lecture("11-A", 0, "12:00", "13:00", "Chemistry", "t2", "R-301"),
    Lecture("11-A", 2, "09:00", "10:00", "English", None, "R-302"),
)
B_LECTURES = (Lecture("10-A", 0, "10:00", "11:00", "Math", "t1", B_ROOM_CANARY),)


async def publish_timetables(raw_db: AsyncSession, w: WorldTenant, lectures: tuple[Lecture, ...]) -> None:
    """One published timetable per class named in `lectures`, with those entries."""
    by_class: dict[str, list[Lecture]] = {}
    for lec in lectures:
        by_class.setdefault(lec.class_name, []).append(lec)
    for class_name, items in by_class.items():
        tt = Timetable(tenant_id=w.id, class_id=w.classes[class_name].id, status="published")
        raw_db.add(tt)
        await raw_db.flush()
        for lec in items:
            raw_db.add(TimetableEntry(
                tenant_id=w.id, timetable_id=tt.id, class_id=w.classes[class_name].id,
                subject_id=w.subjects[lec.subject].id,
                teacher_id=getattr(w, lec.teacher).id if lec.teacher else None,
                day_of_week=lec.day, start_time=time.fromisoformat(lec.start), end_time=time.fromisoformat(lec.end),
                room=lec.room,
            ))
    await raw_db.commit()


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    from app.services import chat_messages

    monkeypatch.setattr(chat_messages, "clock", lambda: FIXED_NOW)
    return FIXED_NOW


async def conversation(
    raw_db: AsyncSession, w: WorldTenant, user: User, class_name: str = "10-A", subject_name: str | None = "Math"
) -> Conversation:
    conv = Conversation(
        tenant_id=w.id, user_id=user.id, class_id=w.classes[class_name].id,
        subject_id=w.subjects[subject_name].id if subject_name else None,
    )
    raw_db.add(conv)
    await raw_db.commit()
    await raw_db.refresh(conv)
    return conv


async def post_message(
    client: AsyncClient, w: WorldTenant, user: User, conversation_id: uuid.UUID | str, content: str,
    client_request_id: uuid.UUID | str | None = None,
) -> Any:
    return await client.post(messages_path(conversation_id), headers=auth(w, user),
                             json=message_body(content, client_request_id))


def raw_json(content: str, client_request_id: uuid.UUID | str | None = None) -> bytes:
    return json.dumps(message_body(content, client_request_id)).encode()


async def rows(raw_db: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Every message row of a conversation, oldest first (the superuser session sees all), read fresh."""
    await raw_db.commit()  # end the session's snapshot so rows committed by the API are visible
    res = await raw_db.execute(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.role.desc()).execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def conversation_row(raw_db: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    await raw_db.commit()
    return (await raw_db.execute(
        select(Conversation).where(Conversation.id == conversation_id).execution_options(populate_existing=True)
    )).scalar_one()


@dataclass
class NoModelSpy:
    """Counts every call that would embed a query, search, or reach an LLM."""

    calls: list[str]


@pytest.fixture
def no_model_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[NoModelSpy]:
    """Replace the embedding, retrieval and LLM entry points with recorders (plan 11.1: zero LLM and zero retriever
    calls on the schedule path; 6a-9b: none at all, the RAG path is a fixed answer)."""
    from app.llm import chain
    from app.llm.fake import FakeLLM
    from app.services import query_embedding, retrieval

    spy = NoModelSpy(calls=[])

    def recorder(name: str) -> Any:
        def record(*_a: Any, **_k: Any) -> Any:
            spy.calls.append(name)
            raise AssertionError(f"{name} was called")

        return record

    for target, name in (
        (retrieval, "search_for_chat"), (retrieval, "search"), (retrieval, "embed_query"),
        (query_embedding, "process_query_embedder"), (query_embedding, "guarded_query_embedder"),
        (chain, "answer_stream"), (chain, "default_chain"), (chain.LLMChain, "stream"), (FakeLLM, "stream"),
    ):
        if hasattr(target, name):
            monkeypatch.setattr(target, name, recorder(f"{getattr(target, '__name__', target)}.{name}"))
    yield spy
    assert spy.calls == [], f"model or retrieval calls happened: {spy.calls}"


def world_a(academic_world: AcademicWorld) -> WorldTenant:
    return academic_world.a
