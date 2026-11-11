"""
app/api/v1/admin.py
────────────────────
Tenant-admin routes.  All routes require is_admin=True on the current user.

Routes:
  GET    /admin/users
  PATCH  /admin/users/{user_id}/deactivate
  POST   /admin/users/invite
  GET    /admin/invitations/accept   (public — no auth, token in query param)
  POST   /admin/users/import
  GET    /admin/settings/registration-mode
  PATCH  /admin/settings/registration-mode
  GET    /admin/pending-approvals
  POST   /admin/pending-approvals/{user_id}/approve
  POST   /admin/pending-approvals/{user_id}/reject
"""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, TenantDB
from app.core.authz import public_endpoint, require
from app.core.tenant_db import get_platform_session
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.audit import record_audit_event
from app.services.invitation import accept_invitation, bulk_import_users, create_invitation
from app.services.rbac import deactivate_user_safely

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    is_admin: bool
    status: str
    created_at: str


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int


class InviteRequest(BaseModel):
    email: EmailStr
    role_name: str = Field("student", pattern=r"^(student|teacher|admin)$")


class InviteResponse(BaseModel):
    invitation_id: str
    email: str
    message: str


class AcceptInvitationRequest(BaseModel):
    token: str
    full_name: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=8, max_length=128)


class RegistrationModeResponse(BaseModel):
    registration_mode: str


class RegistrationModeUpdateRequest(BaseModel):
    registration_mode: str = Field(..., pattern=r"^(open|approval|invite_only)$")


class ImportResultRow(BaseModel):
    row_number: int
    email: str
    full_name: str
    role_name: str
    success: bool
    error: str | None


class ImportResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    rows: list[ImportResultRow]


class TimezoneResponse(BaseModel):
    timezone: str


class TimezoneUpdateRequest(BaseModel):
    timezone: str = Field(..., max_length=64, description="Valid IANA timezone, e.g. 'Asia/Kolkata'")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=UserListResponse, dependencies=[Depends(require("user:manage"))])
async def list_users(
    admin: AdminUser,
    db: TenantDB,
    status_filter: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> UserListResponse:
    """List all users in this tenant."""
    query = select(User).where(User.tenant_id == admin.tenant_id)
    if status_filter:
        query = query.where(User.status == status_filter)
    result = await db.execute(query.offset(skip).limit(limit))
    users = result.scalars().all()
    # Total count without pagination.
    count_stmt = select(func.count()).select_from(User).where(User.tenant_id == admin.tenant_id)
    if status_filter:
        count_stmt = count_stmt.where(User.status == status_filter)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()
    return UserListResponse(
        items=[_to_user_response(u) for u in users],
        total=total,
    )


@router.patch("/users/{user_id}/deactivate", response_model=UserResponse, dependencies=[Depends(require("user:manage"))])
async def deactivate_user(
    user_id: uuid.UUID,
    admin: AdminUser,
    db: TenantDB,
) -> UserResponse:
    """Deactivate a user with last-admin and self-deactivation protection."""
    user = await deactivate_user_safely(
        tenant_id=admin.tenant_id,
        caller=admin,
        target_user_id=user_id,
        db=db,
    )
    return _to_user_response(user)


@router.post("/users/invite", response_model=InviteResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require("user:manage"))])
async def invite_user(
    body: InviteRequest,
    admin: AdminUser,
    db: TenantDB,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> InviteResponse:
    """Send an invitation email to a new user."""
    # Fetch tenant metadata (need the name for the email).
    tenant_result = await platform_db.execute(
        select(Tenant).where(Tenant.id == admin.tenant_id)
    )
    tenant = tenant_result.scalar_one()

    invitation = await create_invitation(
        tenant=tenant,
        email=body.email,
        role_name=body.role_name,
        invited_by_user=admin,
        db=db,
    )
    return InviteResponse(
        invitation_id=str(invitation.id),
        email=invitation.email,
        message="Invitation sent.",
    )


@router.post("/invitations/accept", response_model=dict, dependencies=[Depends(public_endpoint())])
async def accept_invitation_route(
    body: AcceptInvitationRequest,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> dict[str, str]:
    """
    Public endpoint: accept an invitation link.
    Works without an existing user session by finding the invitation's tenant.
    """
    from app.core.security import hash_email_token
    from app.core.tenant_db import tenant_session_context
    from app.db.models.auth import Invitation

    token_h = hash_email_token(body.token)
    result = await platform_db.execute(
        select(Invitation).where(Invitation.token_hash == token_h)
    )
    invitation = result.scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Invitation is invalid or expired."},
        )

    async with tenant_session_context(str(invitation.tenant_id)) as tenant_db:
        user = await accept_invitation(
            raw_token=body.token, full_name=body.full_name, password=body.password, db=tenant_db
        )
    return {"message": "Account created. You can now log in.", "user_id": str(user.id)}


@router.post("/users/import", response_model=ImportResponse, dependencies=[Depends(require("user:manage"))])
async def import_users(
    admin: AdminUser,
    db: TenantDB,
    platform_db: AsyncSession = Depends(get_platform_session),
    file: UploadFile = File(..., description="CSV with columns: email, full_name, role_name"),
) -> ImportResponse:
    """Bulk-import users from a CSV file."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_file_type", "message": "File must be a .csv."},
        )
    contents = await file.read()
    MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "file_too_large", "message": "CSV file exceeds maximum limit of 2MB."},
        )

    tenant_result = await platform_db.execute(
        select(Tenant).where(Tenant.id == admin.tenant_id)
    )
    tenant = tenant_result.scalar_one()

    rows = await bulk_import_users(
        csv_content=contents, tenant=tenant, imported_by=admin, db=db
    )
    succeeded = sum(1 for r in rows if r.success)
    return ImportResponse(
        total=len(rows),
        succeeded=succeeded,
        failed=len(rows) - succeeded,
        rows=[
            ImportResultRow(
                row_number=r.row_number,
                email=r.email,
                full_name=r.full_name,
                role_name=r.role_name,
                success=r.success,
                error=r.error,
            )
            for r in rows
        ],
    )


@router.get("/settings/registration-mode", response_model=RegistrationModeResponse, dependencies=[Depends(require("branding:manage"))])
async def get_registration_mode(
    admin: AdminUser,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> RegistrationModeResponse:
    result = await platform_db.execute(
        select(Tenant).where(Tenant.id == admin.tenant_id)
    )
    tenant = result.scalar_one()
    return RegistrationModeResponse(registration_mode=tenant.registration_mode)


@router.patch("/settings/registration-mode", response_model=RegistrationModeResponse, dependencies=[Depends(require("branding:manage"))])
async def set_registration_mode(
    body: RegistrationModeUpdateRequest,
    admin: AdminUser,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> RegistrationModeResponse:
    await platform_db.execute(
        update(Tenant)
        .where(Tenant.id == admin.tenant_id)
        .values(registration_mode=body.registration_mode)
    )
    return RegistrationModeResponse(registration_mode=body.registration_mode)


@router.get("/settings/timezone", response_model=TimezoneResponse, dependencies=[Depends(require("branding:manage"))])
async def get_tenant_timezone_setting(
    admin: AdminUser,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> TimezoneResponse:
    """Get the configured timezone for this tenant."""
    result = await platform_db.execute(
        select(Tenant.timezone).where(Tenant.id == admin.tenant_id)
    )
    tz = result.scalar_one_or_none() or "Asia/Kolkata"
    return TimezoneResponse(timezone=tz)


@router.patch("/settings/timezone", response_model=TimezoneResponse, dependencies=[Depends(require("branding:manage"))])
async def set_tenant_timezone_setting(
    body: TimezoneUpdateRequest,
    admin: AdminUser,
    request: Request,
    db: TenantDB,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> TimezoneResponse:
    """Update the tenant timezone, validated against IANA zoneinfo and audited."""
    tz_clean = body.timezone.strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_clean)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_timezone", "message": f"'{tz_clean}' is not a valid IANA timezone name."},
        ) from None

    t_res = await platform_db.execute(select(Tenant).where(Tenant.id == admin.tenant_id))
    tenant = t_res.scalar_one()
    old_tz = tenant.timezone
    tenant.timezone = tz_clean
    await platform_db.commit()

    await record_audit_event(
        tenant_id=admin.tenant_id,
        action="tenant:update_timezone",
        target_type="tenant",
        target_id=str(admin.tenant_id),
        details={"previous_timezone": old_tz, "new_timezone": tz_clean},
        actor_id=admin.id,
        request=request,
        db=db,
    )

    return TimezoneResponse(timezone=tz_clean)


@router.get("/pending-approvals", response_model=UserListResponse, dependencies=[Depends(require("user:manage"))])
async def list_pending_approvals(
    admin: AdminUser,
    db: TenantDB,
) -> UserListResponse:
    query = select(User).where(User.tenant_id == admin.tenant_id, User.status == "pending")
    result = await db.execute(query)
    users = result.scalars().all()
    count_stmt = select(func.count()).select_from(User).where(User.tenant_id == admin.tenant_id, User.status == "pending")
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()
    return UserListResponse(items=[_to_user_response(u) for u in users], total=total)


@router.post("/pending-approvals/{user_id}/approve", response_model=UserResponse, dependencies=[Depends(require("user:manage"))])
async def approve_user(
    user_id: uuid.UUID,
    admin: AdminUser,
    db: TenantDB,
) -> UserResponse:
    result = await db.execute(
        select(User).where(User.tenant_id == admin.tenant_id, User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None or user.status != "pending":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.execute(
        update(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id)
        .values(status="active")
    )
    await db.refresh(user)
    return _to_user_response(user)


@router.post("/pending-approvals/{user_id}/reject", response_model=UserResponse, dependencies=[Depends(require("user:manage"))])
async def reject_user(
    user_id: uuid.UUID,
    admin: AdminUser,
    db: TenantDB,
) -> UserResponse:
    result = await db.execute(
        select(User).where(User.tenant_id == admin.tenant_id, User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None or user.status != "pending":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.execute(
        update(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id)
        .values(status="deactivated")
    )
    await db.refresh(user)
    return _to_user_response(user)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
        status=user.status,
        created_at=str(user.created_at),
    )

