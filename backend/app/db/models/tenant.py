"""
app/db/models/tenant.py
───────────────────────
Platform-level tables: tenants and platform_admins.

These tables have no `tenant_id` column. Access differs per table:

  platform_admins  no RLS. Only the `app_platform` DB role may read or write it.
  tenants          RLS enabled and forced (migration 0010), plus column grants:
      app_tenant    its own row only (id = app.tenant_id). Reads id, slug, status, timezone and
                    updates timezone. Everything else (name, kind, registration_mode, db_dsn,
                    doc_limits, created_at) is unreadable and raises "permission denied", and it
                    can neither insert nor delete tenants.
      app_platform  every row. Updates name, kind, registration_mode, status, db_dsn, timezone and
                    doc_limits; never id or slug, and never inserts an explicit id.
      app_worker    every row, id column only (the sweeper lists tenants).
  So code running as app_tenant must select only (slug, status, timezone) columns, never the whole
  Tenant entity; code that needs the other columns uses the platform session.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Tenant(Base):
    """
    One row per organisation (school, college, company, etc.).

    slug      — becomes the subdomain ({slug}.platform.com) and must be
                URL-safe (lowercase, hyphens only).
    kind      — purely informational; used to customise UI copy.
    db_dsn    — NULL = shared database (this Postgres instance). Never readable by app_tenant.
                Set to a separate DSN to move a large tenant to its own DB
                without any application code change (Phase 10 escape hatch).
    """
    __tablename__ = "tenants"
    # Override base so we can add CHECK constraints at the table level.
    __table_args__ = (
        CheckConstraint(
            "registration_mode IN ('open', 'approval', 'invite_only')",
            name="tenants_registration_mode_check",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended', 'archived')",
            name="tenants_status_check",
        ),
        CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$'",
            name="tenants_slug_format_check",
        ),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="school | college | institute | class | company | other",
    )
    registration_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="invite_only",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="active",
    )
    # Escape hatch: a non-NULL DSN routes all app queries for this tenant to a
    # dedicated database instead of the shared one.  NULL = shared (default).
    db_dsn: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default="Asia/Kolkata",
    )
    # Per-tenant document limits (overrides of the platform defaults, see design A2).
    # Written only by the superadmin endpoint; validated against hard platform ceilings.
    doc_limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class PlatformAdmin(Base):
    """
    Superadmin identities.  Completely separate from the tenant `users` table;
    authenticated with a different JWT secret (PLATFORM_SECRET_KEY).
    """
    __tablename__ = "platform_admins"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

