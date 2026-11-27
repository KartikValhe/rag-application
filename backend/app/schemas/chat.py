"""
app/schemas/chat.py
───────────────────
Request and response models of the chat API (docs/phase6-plan.md 8.2). Step 6a-9a: the conversation routes; 6a-9b:
the message request (the answer is an SSE stream, app/services/chat_sse.py); 6a-12a: history, feedback and the admin
chat settings.

Every model forbids unknown fields (extra="forbid"), so a client can never send a tenant, a user or a pair list,
and every key is always present in a response (null when empty).
"""

import uuid
from datetime import datetime
from typing import Literal

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


# ── History and feedback (step 6a-12a) ────────────────────────────────────────


class CitationResponse(_Strict):
    """A cited source as the server stored it (plan 6.4, 8.2). The same shape for every role: never a document,
    version or chunk id, a file kind, a score or a flag mark (the staff shape of 8.2 is deferred). `snippet` and
    `heading_path` are '' once the document was purged (C21). All text is untrusted: show it as plain text."""

    label: str
    document_title: str
    location_kind: Literal["page", "slide", "section"]
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    snippet: str


class FeedbackRequest(_Strict):
    """PUT /api/v1/chat/messages/{id}/feedback. `comment` is cleaned and then 0-500 characters (422
    comment_too_long); 4,000 is only the schema ceiling."""

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4000)


class FeedbackResponse(_Strict):
    rating: Literal["up", "down"]
    comment: str | None
    updated_at: datetime


class MessageResponse(_Strict):
    """One stored message. `route` and `outcome` are null for a question. An answer still being written has outcome
    'pending' and empty content; one left pending longer than CHAT_PENDING_STALE_SECONDS reads as 'interrupted' and
    partial (C28). `feedback` is the caller's own rating of an answer, or null."""

    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    route: str | None
    outcome: str | None
    language: str | None
    citations: list[CitationResponse]
    feedback: FeedbackResponse | None
    partial: bool


class MessageListResponse(_Strict):
    """GET /api/v1/chat/conversations/{id}/messages: one page, oldest first. For older messages pass the first
    item's id as `before`; `has_more` is false on the oldest page."""

    items: list[MessageResponse]
    has_more: bool


# ── Admin chat settings (step 6a-12a) ─────────────────────────────────────────


class ChatSettingsResponse(_Strict):
    """GET and PATCH /api/v1/admin/chat/settings: the tenant's effective values (the defaults when it has no row).
    `allowed_llm_providers` null = the platform chain, [] = no LLM (retrieval-only); `daily_token_budget` null = the
    platform default. `effective_daily_token_budget` is the limit the chat applies today."""

    chat_enabled: bool
    retention_days: int | None  # null: keep everything (the default); otherwise whole days from 30 to 3650
    allowed_llm_providers: list[str] | None
    daily_token_budget: int | None
    effective_daily_token_budget: int


class ChatSettingsUpdateRequest(_Strict):
    """PATCH /api/v1/admin/chat/settings, as documented in the OpenAPI schema. The route checks the body itself
    (app/services/chat_settings.parse_update), so that every refusal is a stable code: at least one field; booleans
    and whole numbers only (no strings, no 1.0); retention_days null (keep everything) or 30-3650; allowed_llm_providers null or exact provider
    names of models.json without duplicates (provider_not_allowed); daily_token_budget null or 1 up to the platform
    ceiling (token_budget_invalid, token_budget_above_platform_ceiling); anything else is invalid_setting."""

    chat_enabled: bool | None = None
    retention_days: int | None = None
    allowed_llm_providers: list[str] | None = None
    daily_token_budget: int | None = None
