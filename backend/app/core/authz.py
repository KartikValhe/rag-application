"""
app/core/authz.py
─────────────────
Phase 2 Policy Layer: Role-Based Access Control and Scoped Authorization.

Guarantees:
  1. Default-deny: Every route must declare a permission.
  2. Permission check: User's assigned roles must possess the declared permission.
  3. Scope check: A teacher can act only on assigned classes/subjects; a student
     only on enrolled classes.
  4. Timing & enumeration defense: Returns generic 404 for both 'not found'
     and 'not yours' so attackers cannot probe resource IDs.
"""

import uuid
from enum import StrEnum
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_platform_admin, get_current_user, get_tenant_session
from app.db.models.academic import Class, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, RolePermission, UserRole
from app.db.models.tenant import PlatformAdmin
from app.db.models.user import User

GENERIC_NOT_FOUND = {
    "code": "not_found",
    "message": "Resource not found.",
}

# Standard permission catalogue for Phase 2
ALL_SYSTEM_PERMISSIONS = {
    "user:manage",
    "role:manage",
    "class:manage",
    "subject:manage",
    "enrollment:manage",
    "assignment:manage",
    "document:upload",
    "document:delete",
    "timetable:manage",
    "timetable:publish",
    "timetable:view",
    "chat:use",
    "analytics:view",
    "branding:manage",
    "audit:view",
}


class ScopeType(StrEnum):
    CLASS = "class"
    CLASS_SUBJECT = "class_subject"


class Scope:
    """
    Defines the academic boundary requirement for a route.
    """
    def __init__(
        self,
        scope_type: ScopeType,
        class_id_param: str = "class_id",
        subject_id_param: str = "subject_id",
    ) -> None:
        self.scope_type = scope_type
        self.class_id_param = class_id_param
        self.subject_id_param = subject_id_param


async def load_user_permissions(user: User, db: AsyncSession) -> set[str]:
    """
    Load all permissions granted to a user across all their assigned roles.
    If the user has `is_admin=True` or has the 'tenant_admin' role, they possess
    all system permissions.
    """
    if user.is_admin:
        return set(ALL_SYSTEM_PERMISSIONS)

    # Fetch user roles
    stmt = (
        select(Role.name, RolePermission.permission_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .where(UserRole.tenant_id == user.tenant_id, UserRole.user_id == user.id)
    )
    result = await db.execute(stmt)
    rows = result.all()

    perms: set[str] = set()
    for role_name, perm_id in rows:
        if role_name == "tenant_admin":
            return set(ALL_SYSTEM_PERMISSIONS)
        if perm_id:
            perms.add(perm_id)

    return perms


async def verify_scope(
    user: User,
    scope: Scope,
    request: Request,
    db: AsyncSession,
) -> None:
    """
    Verify that the user is authorized to act on the specific class / subject.
    Raises generic 404 if the resource does not exist OR the user is not assigned/enrolled.
    """
    # Extract class_id
    class_id_raw = request.path_params.get(scope.class_id_param) or request.query_params.get(
        scope.class_id_param
    )
    if not class_id_raw:
        # Fail closed: A scoped endpoint must provide the scoping parameter
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "scope_resolution_failed",
                "message": f"Required scope parameter '{scope.class_id_param}' missing from request.",
            },
        )

    try:
        class_id = uuid.UUID(str(class_id_raw))
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND) from None

    # 1. Verify class exists in this tenant
    class_res = await db.execute(
        select(Class.id).where(Class.tenant_id == user.tenant_id, Class.id == class_id)
    )
    if class_res.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # 2. If scope is CLASS_SUBJECT, verify subject_id is present and exists in this tenant
    subject_id: uuid.UUID | None = None
    if scope.scope_type == ScopeType.CLASS_SUBJECT:
        subject_id_raw = request.path_params.get(
            scope.subject_id_param
        ) or request.query_params.get(scope.subject_id_param)
        if not subject_id_raw:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "scope_resolution_failed",
                    "message": f"Required scope parameter '{scope.subject_id_param}' missing from request.",
                },
            )

        try:
            subject_id = uuid.UUID(str(subject_id_raw))
        except (ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND) from None

        sub_res = await db.execute(
            select(Subject.id).where(Subject.tenant_id == user.tenant_id, Subject.id == subject_id)
        )
        if sub_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # Check user roles to determine assignment vs enrollment
    roles_res = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == user.tenant_id, UserRole.user_id == user.id)
    )
    role_names = set(roles_res.scalars().all())

    # Tenant admins have organization-wide scope across all valid tenant resources
    if user.is_admin or "tenant_admin" in role_names:
        return

    # Teacher scope: assigned to class (and subject if CLASS_SUBJECT scope)
    if "teacher" in role_names:
        if scope.scope_type == ScopeType.CLASS:
            ta_res = await db.execute(
                select(TeacherAssignment.id).where(
                    TeacherAssignment.tenant_id == user.tenant_id,
                    TeacherAssignment.user_id == user.id,
                    TeacherAssignment.class_id == class_id,
                )
            )
            if ta_res.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
            return

        if scope.scope_type == ScopeType.CLASS_SUBJECT:
            ta_res = await db.execute(
                select(TeacherAssignment.id).where(
                    TeacherAssignment.tenant_id == user.tenant_id,
                    TeacherAssignment.user_id == user.id,
                    TeacherAssignment.class_id == class_id,
                    TeacherAssignment.subject_id == subject_id,
                )
            )
            if ta_res.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
            return

    # Student scope: enrolled in class
    if "student" in role_names:
        enr_res = await db.execute(
            select(Enrollment.id).where(
                Enrollment.tenant_id == user.tenant_id,
                Enrollment.user_id == user.id,
                Enrollment.class_id == class_id,
            )
        )
        if enr_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
        return

    # If user has neither teacher nor student role, deny with generic 404
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)


def require(permission: str, scope: Scope | None = None) -> Any:
    """
    FastAPI dependency: checks role permissions AND (if given) the class/subject scope.
    """
    async def dep(
        request: Request,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_tenant_session),
    ) -> User:
        # 1. Does any of user's roles include this permission?
        user_perms = await load_user_permissions(user, db)
        if permission not in user_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "permission_denied",
                    "message": f"Missing required permission: '{permission}'.",
                },
            )

        # 2. Is user authorized for THIS class/subject?
        if scope:
            await verify_scope(user, scope, request, db)

        return user

    # Tag the dependency with declared permission metadata for Default-Deny auditing
    setattr(dep, "__declared_permission__", permission)  # noqa: B010
    setattr(dep, "__declared_scope__", scope)  # noqa: B010
    return dep


def public_endpoint() -> Any:
    """Explicitly tags a public unauthenticated endpoint."""
    async def dep() -> None:
        return None

    setattr(dep, "__declared_permission__", "public")  # noqa: B010
    return dep


def platform_endpoint() -> Any:
    """Explicitly tags a platform superadmin endpoint."""
    async def dep(admin: PlatformAdmin = Depends(get_current_platform_admin)) -> PlatformAdmin:
        return admin

    setattr(dep, "__declared_permission__", "platform:superadmin")  # noqa: B010
    return dep


def get_route_declared_permission(route: Any) -> str | None:
    """Extract the declared permission from a route, if present."""
    # 1. Check endpoint function attributes
    endpoint = getattr(route, "endpoint", None)
    if endpoint and hasattr(endpoint, "__declared_permission__"):
        val = getattr(endpoint, "__declared_permission__", None)
        if isinstance(val, str):
            return val

    # 2. Check route-level dependencies
    for dep in getattr(route, "dependencies", []):
        call = getattr(dep, "dependency", None) or dep
        if hasattr(call, "__declared_permission__"):
            val = getattr(call, "__declared_permission__", None)
            if isinstance(val, str):
                return val

    # 3. Check parameter/dependant dependencies
    dependant = getattr(route, "dependant", None)
    if dependant:
        for sub_dep in getattr(dependant, "dependencies", []):
            call = getattr(sub_dep, "call", None) or sub_dep
            if hasattr(call, "__declared_permission__"):
                val = getattr(call, "__declared_permission__", None)
                if isinstance(val, str):
                    return val

    return None

