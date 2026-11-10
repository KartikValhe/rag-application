"""
app/db/models/rbac.py
─────────────────────
Role-Based Access Control models:
  - Permission: static platform-wide catalog of capabilities.
  - Role: tenant-scoped role definition (system or custom).
  - RolePermission: many-to-many junction mapping roles to permissions.
  - UserRole: many-to-many junction mapping users to roles.

All tenant-scoped tables enforce composite PK / FK and Postgres RLS.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class Permission(Base):
    """
    Static platform-wide permission catalog.
    Not tenant-scoped; readable across tenants.
    """
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class Role(Base):
    """
    Tenant-scoped role.
    System roles (tenant_admin, teacher, student) cannot be deleted or renamed.
    Custom roles can be created/edited by tenant admins.
    """
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class RolePermission(Base):
    """
    Junction mapping a role to a permission within a tenant.
    """
    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["app.roles.tenant_id", "app.roles.id"],
            ondelete="CASCADE",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    permission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("app.permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserRole(Base):
    """
    Junction assigning a user to a role within their tenant.
    """
    __tablename__ = "user_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["app.roles.tenant_id", "app.roles.id"],
            ondelete="CASCADE",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

