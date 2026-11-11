"""
app/services/rbac.py
────────────────────
Role-Based Access Control and Privilege Escalation Prevention.

Rules enforced:
  1. Nobody can grant or assign permissions they do not possess.
  2. System roles (tenant_admin, teacher, student) cannot be modified or deleted.
  3. The last active tenant admin cannot be removed, unassigned, or deactivated.
  4. All mutations record audit log entries with sanitized details.
"""

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import ALL_SYSTEM_PERMISSIONS, load_user_permissions
from app.db.models.auth import RefreshToken
from app.db.models.rbac import Permission, Role, RolePermission, UserRole
from app.db.models.user import User
from app.services.audit import record_audit_event

SYSTEM_ROLE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "tenant_admin": {
        "description": "Full administrative access to manage the organization",
        "permissions": sorted(ALL_SYSTEM_PERMISSIONS),
    },
    "teacher": {
        "description": "Teacher with access to assigned classes and subjects",
        "permissions": [
            "class:manage",
            "subject:manage",
            "document:upload",
            "document:delete",
            "timetable:manage",
            "timetable:publish",
            "timetable:view",
            "chat:use",
        ],
    },
    "student": {
        "description": "Student learner with access to enrolled classes and chat assistant",
        "permissions": [
            "timetable:view",
            "chat:use",
        ],
    },
}


async def seed_tenant_system_roles(tenant_id: uuid.UUID, db: AsyncSession) -> None:
    """Ensure standard system roles and permissions exist for a given tenant."""
    for role_name, role_data in SYSTEM_ROLE_DEFINITIONS.items():
        existing = await db.execute(
            select(Role).where(Role.tenant_id == tenant_id, Role.name == role_name)
        )
        role = existing.scalar_one_or_none()
        if not role:
            role = Role(
                tenant_id=tenant_id,
                name=role_name,
                description=role_data["description"],
                is_system=True,
            )
            db.add(role)
            await db.flush()

            for perm_id in role_data["permissions"]:
                db.add(
                    RolePermission(
                        tenant_id=tenant_id,
                        role_id=role.id,
                        permission_id=perm_id,
                    )
                )
            await db.flush()


async def count_active_tenant_admins(tenant_id: uuid.UUID, db: AsyncSession) -> int:
    """Count how many active users currently hold the tenant_admin role or is_admin flag."""
    admin_role_stmt = select(Role.id).where(Role.tenant_id == tenant_id, Role.name == "tenant_admin")
    admin_role_id = (await db.execute(admin_role_stmt)).scalar_one_or_none()

    stmt = (
        select(func.count(func.distinct(User.id)))
        .outerjoin(UserRole, UserRole.user_id == User.id)
        .where(
            User.tenant_id == tenant_id,
            User.status == "active",
            (User.is_admin.is_(True)) | (UserRole.role_id == admin_role_id if admin_role_id else False),
        )
    )
    res = await db.execute(stmt)
    return res.scalar_one() or 0


async def list_permissions(db: AsyncSession) -> list[Permission]:
    """List the static catalog of available system permissions."""
    res = await db.execute(select(Permission).order_by(Permission.category, Permission.id))
    return list(res.scalars().all())


async def list_roles(tenant_id: uuid.UUID, db: AsyncSession) -> list[Role]:
    """List all system and custom roles for the tenant."""
    res = await db.execute(
        select(Role).where(Role.tenant_id == tenant_id).order_by(Role.is_system.desc(), Role.name)
    )
    return list(res.scalars().all())


async def get_role_permissions(tenant_id: uuid.UUID, role_id: uuid.UUID, db: AsyncSession) -> set[str]:
    """Return all permission IDs attached to a specific role."""
    stmt = select(RolePermission.permission_id).where(
        RolePermission.tenant_id == tenant_id,
        RolePermission.role_id == role_id,
    )
    res = await db.execute(stmt)
    return set(res.scalars().all())


async def create_custom_role(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    name: str,
    description: str | None = None,
    permission_ids: list[str],
    db: AsyncSession,
    request: Request | None = None,
) -> Role:
    """
    Create a custom role with a subset of permissions.
    Prevents privilege escalation: caller can only grant permissions they currently have.
    """
    clean_name = name.strip()
    if clean_name.lower() in ("tenant_admin", "teacher", "student"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "reserved_role_name", "message": "Cannot create a role with a reserved system name."},
        )

    # 1. Privilege Escalation Check: caller must have all permissions they are trying to grant
    caller_perms = await load_user_permissions(caller, db)
    for perm in permission_ids:
        if perm not in ALL_SYSTEM_PERMISSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_permission", "message": f"Unknown permission: '{perm}'."},
            )
        if perm not in caller_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "privilege_escalation",
                    "message": f"Cannot grant permission '{perm}' which you do not possess.",
                },
            )

    # 2. Duplicate check
    existing = await db.execute(
        select(Role).where(Role.tenant_id == tenant_id, func.lower(Role.name) == clean_name.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "role_exists", "message": f"Role '{clean_name}' already exists in this tenant."},
        )

    role = Role(
        tenant_id=tenant_id,
        name=clean_name,
        description=description,
        is_system=False,
    )
    db.add(role)
    await db.flush()

    for perm_id in set(permission_ids):
        db.add(RolePermission(tenant_id=tenant_id, role_id=role.id, permission_id=perm_id))
    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        actor_id=caller.id,
        action="role:create",
        target_type="role",
        target_id=str(role.id),
        details={"name": role.name, "permissions": permission_ids},
        request=request,
        db=db,
    )
    return role


async def update_custom_role(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    role_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    permission_ids: list[str] | None = None,
    db: AsyncSession,
    request: Request | None = None,
) -> Role:
    """
    Update a custom role's name, description, or permissions.
    Rejects modification of system roles and prevents privilege escalation.
    """
    res = await db.execute(select(Role).where(Role.tenant_id == tenant_id, Role.id == role_id))
    role = res.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "role_not_found", "message": "Role not found."},
        )

    # System role immutability
    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "system_role_immutable", "message": "System roles cannot be modified."},
        )

    caller_perms = await load_user_permissions(caller, db)

    if name:
        clean_name = name.strip()
        if clean_name.lower() in ("tenant_admin", "teacher", "student"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "reserved_role_name", "message": "Cannot rename to a reserved system name."},
            )
        role.name = clean_name

    if description is not None:
        role.description = description

    if permission_ids is not None:
        # Check privilege escalation
        for perm in permission_ids:
            if perm not in caller_perms:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "privilege_escalation",
                        "message": f"Cannot grant permission '{perm}' which you do not possess.",
                    },
                )

        # Replace permissions
        await db.execute(
            delete(RolePermission).where(
                RolePermission.tenant_id == tenant_id, RolePermission.role_id == role.id
            )
        )
        for perm_id in set(permission_ids):
            db.add(RolePermission(tenant_id=tenant_id, role_id=role.id, permission_id=perm_id))

    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        actor_id=caller.id,
        action="role:update",
        target_type="role",
        target_id=str(role.id),
        details={"name": role.name, "permissions": permission_ids},
        request=request,
        db=db,
    )
    return role


async def delete_custom_role(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    role_id: uuid.UUID,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    """
    Delete a custom role. System roles can never be deleted.
    """
    res = await db.execute(select(Role).where(Role.tenant_id == tenant_id, Role.id == role_id))
    role = res.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "role_not_found", "message": "Role not found."},
        )

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "system_role_immutable", "message": "System roles cannot be deleted."},
        )

    await db.delete(role)
    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        actor_id=caller.id,
        action="role:delete",
        target_type="role",
        target_id=str(role_id),
        details={"name": role.name},
        request=request,
        db=db,
    )


async def assign_user_role(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession,
    request: Request | None = None,
) -> UserRole:
    """
    Assign a role to a user.
    Prevents privilege escalation: caller cannot assign a role granting permissions
    they themselves do not have.
    """
    # 1. Target user exists
    user_res = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.id == target_user_id)
    )
    target_user = user_res.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    # 2. Target role exists
    role_res = await db.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.id == role_id)
    )
    role = role_res.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "role_not_found", "message": "Role not found."},
        )

    # 3. Privilege Escalation Check: caller must have all permissions granted by this role
    caller_perms = await load_user_permissions(caller, db)
    role_perms = await get_role_permissions(tenant_id, role.id, db)
    for perm in role_perms:
        if perm not in caller_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "privilege_escalation",
                    "message": f"Cannot assign role '{role.name}' because it grants permission '{perm}' which you do not possess.",
                },
            )

    # Check if assignment already exists
    existing = await db.execute(
        select(UserRole).where(
            UserRole.tenant_id == tenant_id,
            UserRole.user_id == target_user_id,
            UserRole.role_id == role_id,
        )
    )
    ur = existing.scalar_one_or_none()
    if not ur:
        ur = UserRole(tenant_id=tenant_id, user_id=target_user_id, role_id=role_id)
        db.add(ur)
        if role.name == "tenant_admin":
            target_user.is_admin = True
        await db.flush()

        await record_audit_event(
            tenant_id=tenant_id,
            actor_id=caller.id,
            action="role:assign",
            target_type="user",
            target_id=str(target_user_id),
            details={"role_id": str(role_id), "role_name": role.name},
            request=request,
            db=db,
        )

    return ur


async def remove_user_role(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    """
    Remove a role from a user.
    Guarantees the last tenant admin can never be removed.
    """
    role_res = await db.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.id == role_id)
    )
    role = role_res.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "role_not_found", "message": "Role not found."},
        )

    # Last tenant admin protection
    if role.name == "tenant_admin":
        active_admins = await count_active_tenant_admins(tenant_id, db)
        if active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "last_admin_protection", "message": "Cannot remove the last tenant admin."},
            )

    await db.execute(
        delete(UserRole).where(
            UserRole.tenant_id == tenant_id,
            UserRole.user_id == target_user_id,
            UserRole.role_id == role_id,
        )
    )
    if role.name == "tenant_admin":
        user_res = await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.id == target_user_id)
        )
        target_user = user_res.scalar_one_or_none()
        if target_user:
            target_user.is_admin = False
    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        actor_id=caller.id,
        action="role:unassign",
        target_type="user",
        target_id=str(target_user_id),
        details={"role_id": str(role_id), "role_name": role.name},
        request=request,
        db=db,
    )


async def deactivate_user_safely(
    *,
    tenant_id: uuid.UUID,
    caller: User,
    target_user_id: uuid.UUID,
    db: AsyncSession,
    request: Request | None = None,
) -> User:
    """
    Deactivate a user with last-admin and self-deactivation protection.
    """
    if caller.id == target_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "cannot_deactivate_self", "message": "You cannot deactivate your own account."},
        )

    res = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.id == target_user_id)
    )
    target_user = res.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    # Check if target user is an admin
    admin_role_stmt = select(Role.id).where(Role.tenant_id == tenant_id, Role.name == "tenant_admin")
    admin_role_id = (await db.execute(admin_role_stmt)).scalar_one_or_none()

    has_admin_role = False
    if admin_role_id:
        ur_check = await db.execute(
            select(UserRole).where(
                UserRole.tenant_id == tenant_id,
                UserRole.user_id == target_user_id,
                UserRole.role_id == admin_role_id,
            )
        )
        has_admin_role = ur_check.scalar_one_or_none() is not None

    if target_user.is_admin or has_admin_role:
        active_admins = await count_active_tenant_admins(tenant_id, db)
        if active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "last_admin_protection", "message": "Cannot deactivate the last tenant admin."},
            )

    target_user.status = "deactivated"
    # Revoke all active refresh sessions
    await db.execute(
        delete(RefreshToken).where(
            RefreshToken.tenant_id == tenant_id, RefreshToken.user_id == target_user_id
        )
    )
    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        actor_id=caller.id,
        action="user:deactivate",
        target_type="user",
        target_id=str(target_user_id),
        details={"status": "deactivated"},
        request=request,
        db=db,
    )
    return target_user

