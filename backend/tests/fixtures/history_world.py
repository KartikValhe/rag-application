"""
tests/fixtures/history_world.py
───────────────────────────────
Shared setup for the history and feedback tests (step 6a-12a): stored question/answer rows, citation snapshots and
feedback rows written straight into the chat tables through the superuser session (no RLS), so a test controls
every column, including created_at.

Times: `exchange` puts each question at `at` and its answer one millisecond later. A test that needs the read-time
rule for stale pending rows (C28) uses `set_age`, which moves created_at relative to the DATABASE clock, the clock
the rule uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import Message, MessageCitation, MessageFeedback
from app.db.models.user import User
from tests.fixtures.academic_world import WorldTenant

CONV = "/api/v1/chat/conversations"
BASE_TIME = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
# The fields of CitationResponse (plan 8.2): the student shape, for every role (staff shape deferred).
CITATION_KEYS = {"label", "document_title", "location_kind", "page_start", "page_end", "heading_path", "snippet"}
MESSAGE_KEYS = {"id", "role", "content", "created_at", "route", "outcome", "language", "citations", "feedback", "partial"}
FEEDBACK_KEYS = {"rating", "comment", "updated_at"}


def history_path(conversation_id: uuid.UUID | str) -> str:
    return f"{CONV}/{conversation_id}/messages"


def feedback_path(message_id: uuid.UUID | str) -> str:
    return f"/api/v1/chat/messages/{message_id}/feedback"


@dataclass(frozen=True)
class Exchange:
    question: Message
    answer: Message


async def exchange(
    raw_db: AsyncSession, w: WorldTenant, user: User, conversation_id: uuid.UUID, question: str, answer: str, *,
    at: datetime, outcome: str = "answered", route: str = "rag", language: str = "en", partial: bool = False,
) -> Exchange:
    """One question and its answer row, committed."""
    q = Message(
        tenant_id=w.id, conversation_id=conversation_id, user_id=user.id, role="user", content=question,
        client_request_id=uuid.uuid4(), created_at=at,
    )
    raw_db.add(q)
    await raw_db.flush()
    a = Message(
        tenant_id=w.id, conversation_id=conversation_id, user_id=user.id, role="assistant", content=answer,
        reply_to_id=q.id, outcome=outcome, route=route, language=language, partial=partial,
        created_at=at + timedelta(milliseconds=1),
    )
    raw_db.add(a)
    await raw_db.commit()
    await raw_db.refresh(q)
    await raw_db.refresh(a)
    return Exchange(q, a)


async def citation(
    raw_db: AsyncSession, w: WorldTenant, user: User, message: Message, *, label: str = "S1", ordinal: int = 1,
    title: str = "Intro to Physics", file_kind: str = "pdf", location_kind: str = "page", page_start: int | None = 3,
    page_end: int | None = 3, heading_path: str | None = "Motion > Speed", snippet: str = "Speed is distance over time.",
    flagged: bool = False,
) -> MessageCitation:
    row = MessageCitation(
        tenant_id=w.id, message_id=message.id, user_id=user.id, label=label, ordinal=ordinal, chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(), version_id=uuid.uuid4(), document_title=title, version_no=1, file_kind=file_kind,
        location_kind=location_kind, page_start=page_start, page_end=page_end, heading_path=heading_path,
        snippet=snippet, flagged=flagged, similarity=0.91,
    )
    raw_db.add(row)
    await raw_db.commit()
    return row


async def feedback(
    raw_db: AsyncSession, w: WorldTenant, user: User, message: Message, rating: int = 1, comment: str | None = None
) -> MessageFeedback:
    row = MessageFeedback(tenant_id=w.id, message_id=message.id, user_id=user.id, rating=rating, comment=comment)
    raw_db.add(row)
    await raw_db.commit()
    return row


async def set_age(raw_db: AsyncSession, message_id: uuid.UUID, seconds: float) -> None:
    """created_at = the database's now() minus `seconds` (the clock of the read-time stale rule)."""
    await raw_db.execute(
        text("UPDATE app.messages SET created_at = now() - make_interval(secs => :s) WHERE id = :id"),
        {"s": seconds, "id": message_id},
    )
    await raw_db.commit()


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]
