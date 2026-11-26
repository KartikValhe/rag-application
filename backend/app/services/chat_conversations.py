"""
app/services/chat_conversations.py
──────────────────────────────────
The conversation part of the chat API (docs/phase6-plan.md 8.1, step 6a-9a): checks and queries behind
create, list, get and delete of /api/v1/chat/conversations.

Rules:
  - Every function runs in the caller's chat_session_context (app.tenant_id and app.user_id set), so the RESTRICTIVE
    chat_owner policy already limits every query to the caller's own rows. The queries ALSO name the tenant and the
    user themselves: the policy is the second line of defence, not the only one.
  - A conversation is created only for a (class, subject) the caller may READ (build_retrieval_scope, the Phase 5
    pair rule: students their enrolled classes x the subjects offered there, teachers their own pairs, admins every
    offered pair). Anything else is the generic 404, the same body as an id that does not exist.
  - The hourly creation window is taken only after that check (C15: a probe never uses it up) and with no database
    connection checked out (the caller closes its transaction first).
  - get, list and delete are owner-only and do NOT re-check the class scope: a student who left a class can still read
    and delete their own chats. The message route (6a-9b) rebuilds the scope on every message (plan 4.2).
  - Nothing here logs or returns text other than the caller's own conversation titles; errors carry fixed texts.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import slots
from app.core.config import settings
from app.db.models.academic import Class, Subject
from app.db.models.chat import Conversation, Message
from app.db.models.user import User
from app.services import document_scope, retrieval, tenant_settings

CONVERSATION_RATE_KEY_PREFIX: Final = "chat:conv:user:"
CONVERSATION_RATE_WINDOW_SECONDS: Final = 3600
MAX_CURSOR_CHARS: Final = 128
DEFAULT_PAGE: Final = 20
MAX_PAGE: Final = 50

# One fixed text per code (plan 8.4): never a name, an id, SQL or the user's text.
ERROR_MESSAGES: Final[dict[str, str]] = {
    "chat_disabled": "The study assistant is switched off for your organisation.",
    "conversation_rate_limit_exceeded": "You have started too many new chats in the last hour. Please wait and try again.",
    "invalid_cursor": "This list link is not valid any more. Reload the list.",
}
UNAVAILABLE_MESSAGE: Final = "The study assistant is temporarily unavailable. Please try again in a few minutes."


def chat_error(status_code: int, code: str, **extra: Any) -> HTTPException:
    """The standard {"detail": {"code", "message"}} error with the fixed message for `code`."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": ERROR_MESSAGES[code], **extra})


def _slots() -> slots.SlotManager:
    return slots.default_manager


@dataclass(frozen=True)
class ConversationView:
    """One conversation as the routes return it (names read at the time of the request)."""

    id: uuid.UUID
    class_id: uuid.UUID
    class_name: str
    subject_id: uuid.UUID | None
    subject_name: str | None
    title: str | None
    created_at: datetime
    last_message_at: datetime
    message_count: int


@dataclass(frozen=True)
class CreationTarget:
    """A (class, subject) that passed the scope check, with its names."""

    class_id: uuid.UUID
    class_name: str
    subject_id: uuid.UUID | None
    subject_name: str | None


# ── Create ────────────────────────────────────────────────────────────────────


async def creation_target(
    db: AsyncSession, user: User, class_id: uuid.UUID, subject_id: uuid.UUID | None
) -> CreationTarget:
    """503 chat_disabled when the tenant switched chat off; the generic 404 unless the pair is in the caller's read
    scope. Runs in the caller's chat session."""
    if not await tenant_settings.read_chat_enabled(db, user.tenant_id):
        raise chat_error(status.HTTP_503_SERVICE_UNAVAILABLE, "chat_disabled")
    # The only copy of the scope rule (Phase 5 pairs); a narrowing that leaves nothing raises the generic 404.
    await retrieval.build_retrieval_scope(db, user, purpose="read", class_id=class_id, subject_id=subject_id)
    class_name = (await db.execute(
        select(Class.name).where(Class.tenant_id == user.tenant_id, Class.id == class_id)
    )).scalar_one_or_none()
    subject_name: str | None = None
    if subject_id is not None:
        subject_name = (await db.execute(
            select(Subject.name).where(Subject.tenant_id == user.tenant_id, Subject.id == subject_id)
        )).scalar_one_or_none()
        if subject_name is None:
            raise document_scope.not_found()
    if class_name is None:
        raise document_scope.not_found()
    return CreationTarget(class_id, class_name, subject_id, subject_name)


async def check_creation_rate(user_id: uuid.UUID) -> None:
    """Take one slot of the user's rolling hourly window (CHAT_CONVERSATIONS_PER_USER_PER_HOUR). 429
    conversation_rate_limit_exceeded with Retry-After when it is full; 503 service_unavailable when Redis cannot be
    reached outside development (fail closed). Called with no transaction open."""
    limit = settings.CHAT_CONVERSATIONS_PER_USER_PER_HOUR
    try:
        await _slots().acquire(
            [slots.SlotLimit(f"{CONVERSATION_RATE_KEY_PREFIX}{user_id}", limit, CONVERSATION_RATE_WINDOW_SECONDS)],
            unavailable_message=UNAVAILABLE_MESSAGE,
        )
    except slots.SlotRejected as rej:
        retry = max(1, rej.retry_after_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "conversation_rate_limit_exceeded",
                    "message": ERROR_MESSAGES["conversation_rate_limit_exceeded"], "retry_after_seconds": retry},
            headers={"Retry-After": str(retry)},
        ) from None


async def insert_conversation(db: AsyncSession, user: User, target: CreationTarget) -> ConversationView:
    """Insert the caller's conversation (the row's user is always the caller, never a value from the request)."""
    row = (await db.execute(
        insert(Conversation)
        .values(tenant_id=user.tenant_id, user_id=user.id, class_id=target.class_id, subject_id=target.subject_id)
        .returning(Conversation.id, Conversation.title, Conversation.created_at, Conversation.last_message_at,
                   Conversation.message_count)
    )).one()
    return ConversationView(
        id=row.id, class_id=target.class_id, class_name=target.class_name, subject_id=target.subject_id,
        subject_name=target.subject_name, title=row.title, created_at=row.created_at,
        last_message_at=row.last_message_at, message_count=row.message_count,
    )


# ── Read ──────────────────────────────────────────────────────────────────────


def _view_select(user: User) -> Any:
    return (
        select(
            Conversation.id, Conversation.class_id, Class.name.label("class_name"), Conversation.subject_id,
            Subject.name.label("subject_name"), Conversation.title, Conversation.created_at,
            Conversation.last_message_at, Conversation.message_count,
        )
        .join(Class, and_(Class.tenant_id == Conversation.tenant_id, Class.id == Conversation.class_id))
        .outerjoin(Subject, and_(Subject.tenant_id == Conversation.tenant_id, Subject.id == Conversation.subject_id))
        .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id)
    )


def _to_view(row: Any) -> ConversationView:
    return ConversationView(
        id=row.id, class_id=row.class_id, class_name=row.class_name, subject_id=row.subject_id,
        subject_name=row.subject_name, title=row.title, created_at=row.created_at,
        last_message_at=row.last_message_at, message_count=row.message_count,
    )


async def get_own(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> ConversationView | None:
    """The caller's conversation, or None (not found and not yours are the same)."""
    row = (await db.execute(_view_select(user).where(Conversation.id == conversation_id))).one_or_none()
    return None if row is None else _to_view(row)


class CursorError(ValueError):
    """The cursor is not one this module wrote."""


def encode_cursor(last_message_at: datetime, conversation_id: uuid.UUID) -> str:
    raw = f"{last_message_at.isoformat()}|{conversation_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """The (last_message_at, id) of a cursor from encode_cursor. Anything else raises CursorError: a cursor that does
    not encode back to exactly the same text, a time without a time zone, more or fewer than two parts."""
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise CursorError
    try:
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        ts_text, id_text = raw.split("|")
        ts = datetime.fromisoformat(ts_text)
        conversation_id = uuid.UUID(id_text)
    except (ValueError, binascii.Error):
        raise CursorError from None
    if ts.tzinfo is None or encode_cursor(ts, conversation_id) != cursor:
        raise CursorError
    return ts, conversation_id


async def list_own(
    db: AsyncSession, user: User, *, class_id: uuid.UUID | None, cursor: str | None, limit: int
) -> tuple[list[ConversationView], str | None]:
    """The caller's conversations, newest activity first (ties by id), one page after `cursor`. Raises CursorError."""
    stmt = _view_select(user)
    if class_id is not None:
        stmt = stmt.where(Conversation.class_id == class_id)
    if cursor:
        ts, last_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(ts, last_id))
    stmt = stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    views = [_to_view(r) for r in rows[:limit]]
    more = len(rows) > limit
    next_cursor = encode_cursor(views[-1].last_message_at, views[-1].id) if more and views else None
    return views, next_cursor


# ── Delete ────────────────────────────────────────────────────────────────────


async def delete_own(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> int | None:
    """Hard-delete the caller's conversation (messages, citations and feedback go with it by cascade). Returns the
    number of messages it held, or None when there is no such conversation of the caller."""
    message_count = (await db.execute(
        select(func.count()).select_from(Message).where(
            Message.tenant_id == user.tenant_id, Message.user_id == user.id, Message.conversation_id == conversation_id,
        )
    )).scalar_one()
    deleted = (await db.execute(
        delete(Conversation)
        .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id, Conversation.id == conversation_id)
        .returning(Conversation.id)
    )).scalar_one_or_none()
    return None if deleted is None else int(message_count)
