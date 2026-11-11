"""
app/db/models/__init__.py
─────────────────────────
Re-export all models so Alembic's `target_metadata` can discover them.
Import order does not matter; SQLAlchemy's MetaData will collect everything.
"""

from app.db.models.academic import (
    Class,
    ClassSubject,
    Enrollment,
    Subject,
    TeacherAssignment,
)
from app.db.models.audit import AuditLog
from app.db.models.auth import EmailToken, Invitation, RefreshToken
from app.db.models.base import Base
from app.db.models.branding import TenantBranding
from app.db.models.rbac import (
    Permission,
    Role,
    RolePermission,
    UserRole,
)
from app.db.models.tenant import PlatformAdmin, Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User

__all__ = [
    "Base",
    "Tenant",
    "PlatformAdmin",
    "User",
    "RefreshToken",
    "Invitation",
    "EmailToken",
    "Permission",
    "Role",
    "RolePermission",
    "UserRole",
    "Class",
    "Subject",
    "ClassSubject",
    "Enrollment",
    "TeacherAssignment",
    "AuditLog",
    "TenantBranding",
    "Timetable",
    "TimetableEntry",
]
