"""
app/schemas/chat.py
───────────────────
Request and response models of the chat API (docs/phase6-plan.md 8.2). Step 6a-9a: the conversation routes; 6a-9b:
the message request (the answer is an SSE stream, app/services/chat_sse.py); feedback and settings join in 6a-12.

Every model forbids unknown fields (extra="forbid"), so a client can never send a tenant, a user or a pair list,
and every key is always present in a response (null when empty).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NamedRef(_Strict):
    """A class or subject: id and display name."""

    id: uuid.UUID
    name: str


class ConversationCreateRequest(_Strict):
    """POST /api/v1/chat/conversations. The pair must be in the caller's read scope (else the generic 404); it is
    fixed for the life of the conversation and can only narrow the caller's scope, never widen it (plan 4.2)."""

    class_id: uuid.UUID
    subject_id: uuid.UUID | None = None


class ConversationResponse(_Strict):
    """One conversation of the caller. `class` is a Python keyword, so the field is `class_` with the alias
    "class" (the JSON key). `title` is null until the first question (6a-9b sets it)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: uuid.UUID
    class_: NamedRef = Field(alias="class")
    subject: NamedRef | None
    title: str | None
    created_at: datetime
    last_message_at: datetime
    message_count: int


class MessageCreateRequest(_Strict):
    """POST /api/v1/chat/conversations/{id}/messages (step 6a-9b). `content` is checked again after cleaning (1 to
    CHAT_MAX_INPUT_CHARS characters, 422 invalid_message / message_too_long); 4,000 is only the schema ceiling.
    `client_request_id` is chosen by the client once per question and reused on a retry (409 duplicate_request)."""

    content: str = Field(max_length=4000)
    client_request_id: uuid.UUID


class ConversationListResponse(_Strict):
    """GET /api/v1/chat/conversations: the caller's own, newest activity first. `next_cursor` is null on the last
    page; pass it back unchanged as `cursor` for the next one."""

    items: list[ConversationResponse]
    next_cursor: str | None
