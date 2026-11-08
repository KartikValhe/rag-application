"""
app/db/models/auth.py
─────────────────────
Tenant-scoped auth tables: refresh tokens, invitations, email tokens.

All have:
  - tenant_id NOT NULL  (enforced by the composite FK and RLS)
  - RLS policy applied by Alembic migration
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class RefreshToken(Base):
    """
    Hashed refresh tokens.  The raw token is sent to the client once and
    never stored.  Storing the hash means a DB dump cannot be used to
    forge sessions.

    One user may have multiple active refresh tokens (e.g. multiple devices).
    Logout revokes the specific token used; all tokens can be revoked by
    deleting rows where user_id matches.
    """
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Composite FK ensures the token belongs to a user in the same tenant.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # SHA-256 hex digest of the raw token.
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    # Optional: track which device / IP issued this token (Phase 9 audit).
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invitation(Base):
    """
    Pending invitations sent by a tenant admin.

    When the invited user clicks the link (token in the URL), the token is
    consumed: the invitation status changes to `accepted` and a user row is
    created with status=`active`.
    """
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'expired', 'cancelled')",
            name="invitations_status_check",
        ),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # Placeholder for Phase 2 RBAC; stored as a string now ("student", "teacher", etc.).
    role_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="student")
    # SHA-256 of the raw URL token.
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    invited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class EmailToken(Base):
    """
    One-time tokens for email verification and password reset.

    purpose: 'verify_email' | 'reset_password'
    After use the row is deleted (not marked used) so there is no replay window.
    """
    __tablename__ = "email_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name="email_tokens_purpose_check",
        ),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

