"""
app/db/models/chat.py
─────────────────────
Phase 6 chat tables (docs/phase6-plan.md 9.1, migration 0013):

  - Conversation:           one chat of one user about one class (and optionally one subject).
  - Message:                a question (role 'user') or an answer (role 'assistant'). The answer row exists from the
                            first transaction on with outcome 'pending' and is updated when the answer is done.
  - MessageCitation:        server-built snapshot of a cited source (no FK to chunks or documents on purpose).
  - MessageFeedback:        one rating per user and answer.
  - ChatCitationBlankQueue: outbox written in a document purge; the worker blanks the citations of that document.

Every table has tenant_id, a composite primary key that starts with tenant_id and composite foreign keys. Row-level
security is created by the migration: tenant_isolation TO app_tenant, the RESTRICTIVE chat_owner policy (user_id =
app.user_id) TO app_tenant, and the chat_maint policies TO app_chat_maint (plan 9.2, C26). The constants below are
used by both these models and the migration, so the CHECK constraints and column grants cannot drift from the code.
"""

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    REAL,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

# ── Shared constants (also imported by migration 0013) ───────────────────────

MESSAGE_ROLE_USER: Final = "user"
MESSAGE_ROLE_ASSISTANT: Final = "assistant"
MESSAGE_ROLES: Final = (MESSAGE_ROLE_USER, MESSAGE_ROLE_ASSISTANT)
MESSAGE_ROUTES: Final = ("schedule", "clarify", "rag")
OUTCOME_PENDING: Final = "pending"
MESSAGE_OUTCOMES: Final = (
    OUTCOME_PENDING,
    "answered",
    "not_found",
    "refused_by_model",
    "no_valid_citations",
    "schedule_answered",
    "schedule_no_timetable",
    "clarify",
    "fallback_retrieval_only",
    "budget_retrieval_only",
    "interrupted",
    "cancelled",
    "truncated",
    "blocked_leak",
    "error",
)
MESSAGE_LANGUAGES: Final = ("en", "mr", "hi", "unknown")
ROUTER_CONFIDENCES: Final = ("high", "low")
CITATION_LOCATION_KINDS: Final = ("page", "slide", "section")

CONVERSATION_TITLE_MAX: Final = 80
MESSAGE_CONTENT_MAX: Final = 10_000
CITATION_SNIPPET_MAX: Final = 300
FEEDBACK_COMMENT_MAX: Final = 500

# Columns app_tenant may UPDATE (plan 9.2, 6a-8 narrowing): what T1/T3 of the request flow and the shielded cancel
# step write. Everything else of a row is fixed once it is inserted.
CONVERSATION_TENANT_UPDATE_COLUMNS: Final = ("title", "last_message_at", "message_count")
MESSAGE_TENANT_UPDATE_COLUMNS: Final = (
    "content",
    "route",
    "outcome",
    "partial",
    "intent",
    "rule_ids",
    "router_confidence",
    "language",
    "retrieval_query",
    "provider",
    "model_key",
    "prompt_version",
    "prompt_tokens",
    "completion_tokens",
    "tokens_estimated",
    "latency_ms",
    "retrieval",
    "invalid_citation_count",
    "flagged_sources_used",
    "needs_review",
    "completed_at",
)
FEEDBACK_TENANT_UPDATE_COLUMNS: Final = ("rating", "comment", "updated_at")

# Columns the chat maintenance role (app_chat_maint, worker only) may UPDATE (plan 9.2, C22).
MESSAGE_MAINT_UPDATE_COLUMNS: Final = ("outcome", "partial", "completed_at")
CITATION_MAINT_UPDATE_COLUMNS: Final = ("snippet", "heading_path")


def _tenant_fk() -> Any:
    return ForeignKey("app.tenants.id", ondelete="CASCADE")


# ── Models ───────────────────────────────────────────────────────────────────


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Target of the messages composite FK, so a message's user_id always equals its conversation's.
        UniqueConstraint("tenant_id", "user_id", "id", name="uq_conversations_tenant_user_id"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["app.users.tenant_id", "app.users.id"], ondelete="CASCADE",
            name="fk_conversations_user",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "class_id"], ["app.classes.tenant_id", "app.classes.id"], ondelete="CASCADE",
            name="fk_conversations_class",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"], ["app.subjects.tenant_id", "app.subjects.id"],
            ondelete="SET NULL (subject_id)", name="fk_conversations_subject",
        ),
        Index("ix_conversations_user_last_message", "tenant_id", "user_id", text("last_message_at DESC")),
        Index("ix_conversations_last_message", "tenant_id", "last_message_at"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _tenant_fk(), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "id", name="uq_messages_tenant_user_id"),
        UniqueConstraint("tenant_id", "conversation_id", "id", name="uq_messages_tenant_conversation_id"),
        UniqueConstraint(
            "tenant_id", "conversation_id", "client_request_id", name="uq_messages_client_request"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "conversation_id"],
            ["app.conversations.tenant_id", "app.conversations.user_id", "app.conversations.id"],
            ondelete="CASCADE", name="fk_messages_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "reply_to_id"],
            ["app.messages.tenant_id", "app.messages.conversation_id", "app.messages.id"],
            ondelete="CASCADE", name="fk_messages_reply_to",
        ),
        # At most one assistant row per user message (plan 9.1, review 6-0b).
        Index(
            "ux_messages_assistant_reply", "tenant_id", "reply_to_id", unique=True,
            postgresql_where=text(f"role = '{MESSAGE_ROLE_ASSISTANT}'"),
        ),
        Index("ix_messages_conversation_created", "tenant_id", "conversation_id", "created_at"),
        Index("ix_messages_pending_created", "tenant_id", "created_at", postgresql_where=text(f"outcome = '{OUTCOME_PENDING}'")),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _tenant_fk(), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    router_confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    retrieval_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    latency_ms: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    retrieval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    invalid_citation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    flagged_sources_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    guardrail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageCitation(Base):
    __tablename__ = "message_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "message_id"],
            ["app.messages.tenant_id", "app.messages.user_id", "app.messages.id"],
            ondelete="CASCADE", name="fk_message_citations_message",
        ),
        Index("ix_message_citations_document", "tenant_id", "document_id"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _tenant_fk(), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(8), nullable=False)
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Plain snapshot references, deliberately not FKs: chunks are replaced at every publish (plan 9.1).
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_title: Mapped[str] = mapped_column(String(200), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    location_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    similarity: Mapped[float | None] = mapped_column(REAL, nullable=True)


class MessageFeedback(Base):
    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_message_feedback_message_user"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "message_id"],
            ["app.messages.tenant_id", "app.messages.user_id", "app.messages.id"],
            ondelete="CASCADE", name="fk_message_feedback_message",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _tenant_fk(), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatCitationBlankQueue(Base):
    """Outbox of document purges (C21). app_tenant may only INSERT (without RETURNING); the worker's maintenance
    role reads a row, blanks the citations of that document and deletes the row."""

    __tablename__ = "chat_citation_blank_queue"
    __table_args__ = (
        Index("ix_chat_citation_blank_queue_created", "tenant_id", "created_at"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _tenant_fk(), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # A snapshot reference: the document row is already gone when the worker reads this row, so no FK.
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
