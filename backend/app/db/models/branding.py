"""
app/db/models/branding.py
─────────────────────────
Tenant-scoped white-label branding and design tokens.

Enforces:
  - Row Level Security (RLS) via composite key (tenant_id, id).
  - One branding profile per tenant.
  - Incrementing version integer for cache busting.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class TenantBranding(Base):
    """
    White-label theme configuration and assets for a tenant.
    """
    __tablename__ = "tenant_branding"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_branding_tenant_id"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tokens: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    logo_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    favicon_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    tagline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    footer_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=func.now(),
        nullable=False,
    )

