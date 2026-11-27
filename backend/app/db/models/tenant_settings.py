"""
app/db/models/tenant_settings.py
────────────────────────────────
One row of chat settings per tenant (docs/phase6-plan.md 9.1, migration 0012).

The primary key is the tenant itself. RLS ENABLE + FORCE with the tenant_isolation policy. app_tenant may SELECT,
INSERT and UPDATE, never DELETE (the row lives as long as the tenant: tenants CASCADE). app_platform and app_worker
have no grant, so the row of a new tenant is written in that tenant's own context (app/services/tenant_settings.py).

The schema is created by the migration, not from this model: `updated_by` has a composite FK
(tenant_id, updated_by) -> users ON DELETE SET NULL (updated_by), which only the migration states.
"""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

# Q4 and C35 (owner, 6a-12b): chat retention is optional per tenant. NULL (the default, also for a tenant with no row)
# means keep everything; a period is a whole number of days from 30 to 3650 and must be set by a tenant admin on purpose.
CHAT_RETENTION_DAYS_MIN: Final = 30
CHAT_RETENTION_DAYS_MAX: Final = 3650


class TenantSettings(Base):
    __tablename__ = "tenant_settings"
    __table_args__ = ({"schema": "app"},)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    chat_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # NULL = never delete (no server default: nothing may fill in a number). Migration 0014 made it nullable.
    chat_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL = the platform chain; {} = no LLM at all (retrieval-only); otherwise it can only narrow the chain.
    allowed_llm_providers: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # NULL = the platform default (LLM_TENANT_DAILY_TOKENS); never above LLM_PLATFORM_DAILY_TOKENS.
    chat_daily_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
