"""
app/db/models/user.py
─────────────────────
Tenant-scoped user table.

Key design decisions (see docs/adr/001-isolation.md):
  - Composite primary key (tenant_id, id) so child tables can use
    composite foreign keys and a row in tenant A can never point at
    a row in tenant B.
  - email is UNIQUE per tenant, not globally.  The same human in two
    organisations has two separate accounts.
  - RLS policy is created by the Alembic migration, not here.
    SQLAlchemy does not model RLS; Alembic does it via raw SQL.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class User(Base):
    """
    A user belonging to exactly one tenant.

    status values:
      pending    — registered via `approval` mode; waiting for admin approval.
      active     — can log in.
      deactivated — soft-deleted; cannot log in.
    """
    __tablename__ = "users"
    __table_args__ = (
        # email unique within a tenant (the same address is allowed across tenants).
        UniqueConstraint("tenant_id", "email", name="users_tenant_email_uq"),
        CheckConstraint(
            "status IN ('pending', 'active', 'deactivated')",
            name="users_status_check",
        ),
        {"schema": "app"},
    )

    # Composite primary key enables composite foreign keys in child tables.
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
    # citext gives case-insensitive comparison without manual lower().
    # The column type is set via raw DDL in the migration because SQLAlchemy
    # doesn't have a built-in citext type.
    email: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL for future SSO-only users who have no password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # is_admin: True = tenant admin.  Phase 2 replaces this with RBAC roles.
    is_admin: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

