"""
app/services/chat_feedback.py
─────────────────────────────
Thumbs up or down on an answer: PUT and DELETE /api/v1/chat/messages/{message_id}/feedback (docs/phase6-plan.md 8.1,
8.2, 9.1, step 6a-12a).

Rules (owner, 6a-12a brief):
  - only an ASSISTANT message of the caller can be rated: a question, another user's answer, another tenant's or an id
    that does not exist is the generic 404 (same body for every case);
  - the class scope is NOT re-checked (the rating is the caller's own data, like delete; plan 4.2 names only the
    history and citation routes for the re-check);
  - one row per user and answer: PUT upserts it, DELETE removes it (204 also when there was none);
  - the comment is cleaned like a question (clean_text) and is 0 to FEEDBACK_COMMENT_MAX (500) characters AFTER
    cleaning; empty becomes null. Cleaning can make a text longer (NFC), so the limit is checked on the cleaned text
    (422 comment_too_long), never left to the database CHECK;
  - feedback is not audited (plan 9.6: the rows are the record) and its comment never reaches a log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import (
    FEEDBACK_COMMENT_MAX,
    MESSAGE_ROLE_ASSISTANT,
    Message,
    MessageFeedback,
)
from app.db.models.user import User
from app.services.document_text import clean_text

Rating = Literal["up", "down"]
RATING_VALUES: Final[dict[str, int]] = {"up": 1, "down": -1}
_RATING_NAMES: Final[dict[int, Rating]] = {1: "up", -1: "down"}

ERROR_MESSAGES: Final[dict[str, str]] = {
    "comment_too_long": "Your comment is too long. Please make it shorter.",
}


@dataclass(frozen=True, slots=True)
class FeedbackView:
    rating: Rating
    comment: str | None
    updated_at: datetime


def view(rating: int, comment: str | None, updated_at: datetime) -> FeedbackView:
    return FeedbackView(rating=_RATING_NAMES[int(rating)], comment=comment, updated_at=updated_at)


def clean_comment(raw: str | None) -> str | None:
    """The comment as stored: cleaned, None when nothing is left, 422 comment_too_long past the limit."""
    if raw is None:
        return None
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    if len(cleaned) > FEEDBACK_COMMENT_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "comment_too_long", "message": ERROR_MESSAGES["comment_too_long"]},
        )
    return cleaned


async def is_own_answer(db: AsyncSession, user: User, message_id: uuid.UUID) -> bool:
    """True when `message_id` is an assistant message of the caller."""
    found = (await db.execute(
        select(Message.id).where(
            Message.tenant_id == user.tenant_id, Message.user_id == user.id, Message.id == message_id,
            Message.role == MESSAGE_ROLE_ASSISTANT,
        )
    )).scalar_one_or_none()
    return found is not None


async def upsert(db: AsyncSession, user: User, message_id: uuid.UUID, rating: Rating, comment: str | None) -> FeedbackView:
    """Insert or replace the caller's rating of one of their answers (the caller checked is_own_answer in the same
    transaction). The row's user is always the caller."""
    values = {"rating": RATING_VALUES[rating], "comment": comment}
    new_row = insert(MessageFeedback).values(tenant_id=user.tenant_id, message_id=message_id, user_id=user.id, **values)
    stmt = new_row.on_conflict_do_update(
        index_elements=[MessageFeedback.tenant_id, MessageFeedback.message_id, MessageFeedback.user_id],
        set_={**values, "updated_at": func.clock_timestamp()},
    ).returning(MessageFeedback.rating, MessageFeedback.comment, MessageFeedback.updated_at)
    row = (await db.execute(stmt)).one()
    return view(row.rating, row.comment, row.updated_at)


async def delete_own(db: AsyncSession, user: User, message_id: uuid.UUID) -> None:
    """Remove the caller's rating of one of their answers, if there is one."""
    await db.execute(
        delete(MessageFeedback).where(
            MessageFeedback.tenant_id == user.tenant_id, MessageFeedback.user_id == user.id,
            MessageFeedback.message_id == message_id,
        )
    )
