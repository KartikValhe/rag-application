"""
app/api/v1/roles.py
───────────────────
Roles and Permissions management endpoints.
All endpoints require the 'role:manage' permission.
Prevents privilege escalation and protects system roles.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import TenantDB
from app.core.authz import require
from app.db.models.rbac import Role, RolePermission, UserRole
from app.db.models.user import User
from app.services.rbac import (
    assign_user_role,
    create_custom_role,
    delete_custom_role,
    get_role_permissions,
    list_permissions,
    list_roles,
    remove_user_role,
    update_custom_role,
)

router = APIRouter(tags=["roles"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class PermissionResponse(BaseModel):
    id: str
    category: str
    description: str


class RoleResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_system: bool
    created_at: str
    permissions: list[str] = Field(default_factory=list)


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(None, max_length=500)
    permissions: list[str] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = Field(None, max_length=500)
    permissions: list[str] | None = None


class AssignRoleRequest(BaseModel):
    role_id: uuid.UUID


class MessageResponse(BaseModel):
    message: str


# ── Helper ────────────────────────────────────────────────────────────────────

async def _build_role_response(role: Role, tenant_id: uuid.UUID, db: TenantDB) -> RoleResponse:
    perms = await get_role_permissions(tenant_id, role.id, db)
    return RoleResponse(
        id=str(role.id),
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        created_at=role.created_at.isoformat() if hasattr(role.created_at, "isoformat") else str(role.created_at),
        permissions=sorted(perms),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/roles/permissions", response_model=list[PermissionResponse])
async def get_permissions(
    db: TenantDB,
    _current_user: User = Depends(require("role:manage")),
) -> list[PermissionResponse]:
    """List the static catalog of system permissions."""
    perms = await list_permissions(db)
    return [
        PermissionResponse(id=p.id, category=p.category, description=p.description)
        for p in perms
    ]


@router.get("/roles", response_model=list[RoleResponse])
async def get_roles(
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> list[RoleResponse]:
    """List all system and custom roles defined for this tenant."""
    roles = await list_roles(current_user.tenant_id, db)
    return [await _build_role_response(r, current_user.tenant_id, db) for r in roles]


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: CreateRoleRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> RoleResponse:
    """Create a new custom role with specified permissions."""
    role = await create_custom_role(
        tenant_id=current_user.tenant_id,
        caller=current_user,
        name=body.name,
        description=body.description,
        permission_ids=body.permissions,
        db=db,
        request=request,
    )
    return await _build_role_response(role, current_user.tenant_id, db)


@router.get("/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> RoleResponse:
    """Get single role details by ID."""
    res = await db.execute(
        select(Role).where(Role.tenant_id == current_user.tenant_id, Role.id == role_id)
    )
    role = res.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Resource not found."},
        )
    return await _build_role_response(role, current_user.tenant_id, db)


@router.patch("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: uuid.UUID,
    body: UpdateRoleRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> RoleResponse:
    """Update a custom role's name, description, or permissions."""
    role = await update_custom_role(
        tenant_id=current_user.tenant_id,
        caller=current_user,
        role_id=role_id,
        name=body.name,
        description=body.description,
        permission_ids=body.permissions,
        db=db,
        request=request,
    )
    return await _build_role_response(role, current_user.tenant_id, db)


@router.delete("/roles/{role_id}", response_model=MessageResponse)
async def delete_role(
    role_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> MessageResponse:
    """Delete a custom role. System roles cannot be deleted."""
    await delete_custom_role(
        tenant_id=current_user.tenant_id,
        caller=current_user,
        role_id=role_id,
        db=db,
        request=request,
    )
    return MessageResponse(message="Role deleted successfully.")


@router.get("/users/{user_id}/roles", response_model=list[RoleResponse])
async def get_user_roles(
    user_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> list[RoleResponse]:
    """List all roles assigned to a user."""
    # Verify user exists in tenant
    user_res = await db.execute(
        select(User).where(User.tenant_id == current_user.tenant_id, User.id == user_id)
    )
    if user_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Resource not found."},
        )

    stmt = (
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == current_user.tenant_id, UserRole.user_id == user_id)
        .order_by(Role.is_system.desc(), Role.name)
    )
    res = await db.execute(stmt)
    roles = res.scalars().all()
    return [await _build_role_response(r, current_user.tenant_id, db) for r in roles]


@router.post("/users/{user_id}/roles", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def assign_role(
    user_id: uuid.UUID,
    body: AssignRoleRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> MessageResponse:
    """Assign a role to a user."""
    await assign_user_role(
        tenant_id=current_user.tenant_id,
        caller=current_user,
        target_user_id=user_id,
        role_id=body.role_id,
        db=db,
        request=request,
    )
    return MessageResponse(message="Role assigned successfully.")


@router.delete("/users/{user_id}/roles/{role_id}", response_model=MessageResponse)
async def unassign_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("role:manage")),
) -> MessageResponse:
    """Remove a role assignment from a user."""
    await remove_user_role(
        tenant_id=current_user.tenant_id,
        caller=current_user,
        target_user_id=user_id,
        role_id=role_id,
        db=db,
        request=request,
    )
    return MessageResponse(message="Role removed successfully.")

