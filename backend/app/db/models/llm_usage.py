"""
app/db/models/llm_usage.py
──────────────────────────
One row per LLM call (docs/phase6-plan.md 9.1, migration 0012): numbers, names and codes only, no content of any
kind (no question, answer, prompt, title or chunk text). Written by app/services/llm_usage.record_usage.

PK (tenant_id, id); RLS ENABLE + FORCE with the tenant_isolation policy. app_tenant may SELECT and INSERT only (no
UPDATE, no DELETE: the retention delete belongs to the chat maintenance role of migration 0013). `user_id` has a
composite FK (tenant_id, user_id) -> users ON DELETE SET NULL (user_id); `message_id` has no FK on purpose, so the
cost record survives the deletion of its conversation. Both are stated by the migration only.
"""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

USAGE_PURPOSES: Final = ("answer", "condense")
USAGE_OUTCOMES: Final = (
    "ok",
    "rate_limited",
    "timeout",
    "unavailable",
    "bad_request",
    "blocked",
    "auth_error",
    "cancelled",
    "interrupted",
)

# Column sizes, shared with the migration's CHECKs and the service's validation.
PURPOSE_MAX: Final = 16
PROVIDER_MAX: Final = 32
MODEL_KEY_MAX: Final = 200
OUTCOME_MAX: Final = 20
# Postgres CHECK patterns: the same shape as app/llm/providers.split_model_key (no spaces, no free text).
PROVIDER_PATTERN: Final = "^[A-Za-z][A-Za-z0-9_-]{0,31}$"
MODEL_KEY_PATTERN: Final = "^[A-Za-z][A-Za-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$"


class LLMUsageEvent(Base):
    __tablename__ = "llm_usage_events"
    __table_args__ = ({"schema": "app"},)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    purpose: Mapped[str] = mapped_column(String(PURPOSE_MAX), nullable=False)
    provider: Mapped[str] = mapped_column(String(PROVIDER_MAX), nullable=False)
    model_key: Mapped[str] = mapped_column(String(MODEL_KEY_MAX), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(OUTCOME_MAX), nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
