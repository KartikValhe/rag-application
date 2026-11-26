"""
app/services/chat_history.py
────────────────────────────
The message history of one conversation: GET /api/v1/chat/conversations/{id}/messages (docs/phase6-plan.md 8.1,
8.2, step 6a-12a).

Rules (owner, 6a-12 brief decision 1 and the 6a-12a brief):
  - only the caller's own conversation (generic 404 otherwise, the same body as an id that does not exist);
  - the caller's CURRENT read scope is rebuilt and narrowed to the conversation's class and subject on every call,
    like the message route (generic 404 when nothing is left). List, get and delete do not re-check (plan 4.2);
  - citations come back in the student shape for every role (label, title, location, heading, snippet): never a
    document, version or chunk id, a file kind, a score or a flag mark. The staff shape of 8.2 is deferred;
  - a pending answer older than CHAT_PENDING_STALE_SECONDS reads as interrupted and partial (C28, read time only,
    the same rule as the 409 of the message route: chat_messages.read_time_view);
  - a blanked citation (C21: snippet and heading_path '') is returned as it is stored: empty text, never an error;
  - paging: `before` is a message id of THIS conversation; the page holds the `limit` messages just before it, oldest
    first. A malformed id, a non-canonical spelling, or an id that is not a message of this conversation (another
    conversation, another user, another tenant, none at all) is the same CursorError (422 invalid_cursor).

Every query names the tenant and the user itself; the chat_owner policy of the session is the second line. Nothing
here logs: the rows are the caller's own content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import (
    MESSAGE_ROLE_ASSISTANT,
    Conversation,
    Message,
    MessageCitation,
    MessageFeedback,
)
from app.db.models.user import User
from app.services import chat_feedback, chat_messages, document_scope, retrieval

DEFAULT_PAGE: Final = 30
MAX_PAGE: Final = 50
_UUID_TEXT_CHARS: Final = 36


class CursorError(ValueError):
    """`before` is not a message of this conversation (or not an id at all)."""


@dataclass(frozen=True, slots=True)
class CitationView:
    label: str
    document_title: str
    location_kind: str
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    snippet: str


@dataclass(frozen=True, slots=True)
class MessageView:
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    route: str | None
    outcome: str | None
    language: str | None
    partial: bool
    citations: tuple[CitationView, ...]
    feedback: chat_feedback.FeedbackView | None


def parse_before(value: str) -> uuid.UUID:
    """The message id of a `before` value: only the canonical lowercase form this API writes."""
    if len(value) != _UUID_TEXT_CHARS:
        raise CursorError
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise CursorError from None
    if str(parsed) != value:
        raise CursorError
    return parsed


async def _check_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> None:
    """The generic 404 unless the conversation is the caller's AND its class and subject are still in the caller's
    current read scope."""
    conv = (await db.execute(
        select(Conversation.class_id, Conversation.subject_id).where(
            Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id, Conversation.id == conversation_id)
    )).one_or_none()
    if conv is None:
        raise document_scope.not_found()
    scope = await retrieval.build_retrieval_scope(db, user, purpose="read")
    scope.narrow(conv.class_id, conv.subject_id)  # nothing left: the generic 404


async def read_page(
    db: AsyncSession, user: User, conversation_id: uuid.UUID, *, before: str | None, limit: int
) -> tuple[list[MessageView], bool]:
    """One page of the caller's conversation and whether older messages exist. Raises the generic 404 (not yours,
    not found, out of scope) before CursorError."""
    await _check_conversation(db, user, conversation_id)
    own = (Message.tenant_id == user.tenant_id, Message.user_id == user.id, Message.conversation_id == conversation_id)

    stmt_outcome, stmt_partial = chat_messages.read_time_view(Message)
    stmt = select(
        Message.id, Message.role, Message.content, Message.created_at, Message.route, stmt_outcome.label("outcome"),
        Message.language, stmt_partial.label("partial"),
    ).where(*own)
    if before is not None:
        cursor_id = parse_before(before)
        cursor = (await db.execute(
            select(Message.created_at, Message.id).where(*own, Message.id == cursor_id)
        )).one_or_none()
        if cursor is None:
            raise CursorError
        stmt = stmt.where(tuple_(Message.created_at, Message.id) < tuple_(cursor.created_at, cursor.id))
    rows = (await db.execute(stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1))).all()
    has_more = len(rows) > limit
    page = list(reversed(rows[:limit]))

    answer_ids = [r.id for r in page if r.role == MESSAGE_ROLE_ASSISTANT]
    citations: dict[uuid.UUID, list[CitationView]] = {}
    feedback: dict[uuid.UUID, chat_feedback.FeedbackView] = {}
    if answer_ids:
        for c in (await db.execute(
            select(MessageCitation.message_id, MessageCitation.label, MessageCitation.document_title,
                   MessageCitation.location_kind, MessageCitation.page_start, MessageCitation.page_end,
                   MessageCitation.heading_path, MessageCitation.snippet)
            .where(MessageCitation.tenant_id == user.tenant_id, MessageCitation.user_id == user.id,
                   MessageCitation.message_id.in_(answer_ids))
            .order_by(MessageCitation.message_id, MessageCitation.ordinal)
        )).all():
            citations.setdefault(c.message_id, []).append(CitationView(
                label=c.label, document_title=c.document_title, location_kind=c.location_kind,
                page_start=c.page_start, page_end=c.page_end, heading_path=c.heading_path, snippet=c.snippet,
            ))
        for f in (await db.execute(
            select(MessageFeedback.message_id, MessageFeedback.rating, MessageFeedback.comment,
                   MessageFeedback.updated_at)
            .where(MessageFeedback.tenant_id == user.tenant_id, MessageFeedback.user_id == user.id,
                   MessageFeedback.message_id.in_(answer_ids))
        )).all():
            feedback[f.message_id] = chat_feedback.view(f.rating, f.comment, f.updated_at)

    views = [
        MessageView(
            id=r.id, role=r.role, content=r.content, created_at=r.created_at, route=r.route, outcome=r.outcome,
            language=r.language, partial=bool(r.partial), citations=tuple(citations.get(r.id, ())),
            feedback=feedback.get(r.id),
        )
        for r in page
    ]
    return views, has_more
