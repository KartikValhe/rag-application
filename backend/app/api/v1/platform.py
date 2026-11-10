"""
app/api/v1/platform.py
──────────────────────
Superadmin (platform) routes.  All routes here are gated behind a platform JWT
and use the platform DB session (no tenant_id set, no RLS on tenants table).

Routes:
  POST   /platform/auth/login
  GET    /platform/tenants
  POST   /platform/tenants
  PATCH  /platform/tenants/{tenant_id}
  POST   /platform/tenants/{tenant_id}/admins
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update

from app.api.deps import PlatformAdminUser, PlatformDB
from app.core.authz import platform_endpoint, public_endpoint
from app.core.security import create_platform_access_token, verify_password
from app.db.engine import SessionLocal
from app.db.models.tenant import PlatformAdmin, Tenant
from app.services.email import send_temp_password_email
from app.services.invitation import create_admin_user

router = APIRouter(prefix="/platform", tags=["platform"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class PlatformLoginRequest(BaseModel):
    email: EmailStr
    password: str


class PlatformLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TenantCreateRequest(BaseModel):
    slug: str = Field(..., min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., pattern=r"^(school|college|institute|class|company|other)$")
    registration_mode: str = Field("invite_only", pattern=r"^(open|approval|invite_only)$")


class TenantResponse(BaseModel):
    id: str
    slug: str
    name: str
    kind: str
    registration_mode: str
    status: str
    created_at: str


class TenantUpdateRequest(BaseModel):
    status: str | None = Field(None, pattern=r"^(active|suspended|archived)$")
    registration_mode: str | None = Field(None, pattern=r"^(open|approval|invite_only)$")
    name: str | None = None


class TenantListResponse(BaseModel):
    items: list[TenantResponse]
    total: int


class CreateAdminRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=200)


class CreateAdminResponse(BaseModel):
    user_id: str
    email: str
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/auth/login", response_model=PlatformLoginResponse, dependencies=[Depends(public_endpoint())])
async def platform_login(
    body: PlatformLoginRequest,
    db: PlatformDB,
) -> PlatformLoginResponse:
    """Authenticate a platform (superadmin) account."""
    result = await db.execute(
        select(PlatformAdmin).where(
            PlatformAdmin.email == body.email.lower(),
            PlatformAdmin.is_active.is_(True),
        )
    )
    admin = result.scalar_one_or_none()
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials"},
        )
    token = create_platform_access_token(platform_admin_id=str(admin.id))
    return PlatformLoginResponse(access_token=token)


@router.get("/tenants", response_model=TenantListResponse, dependencies=[Depends(platform_endpoint())])
async def list_tenants(
    db: PlatformDB,
    _admin: PlatformAdminUser,
    skip: int = 0,
    limit: int = 50,
    status_filter: str | None = None,
) -> TenantListResponse:
    """List all tenants with optional status filter."""
    query = select(Tenant).order_by(Tenant.created_at.desc())
    if status_filter:
        query = query.where(Tenant.status == status_filter)
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one()
    result = await db.execute(query.offset(skip).limit(limit))
    tenants = result.scalars().all()
    return TenantListResponse(
        items=[_to_tenant_response(t) for t in tenants],
        total=total,
    )


@router.post("/tenants", response_model=TenantResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(platform_endpoint())])
async def create_tenant(
    body: TenantCreateRequest,
    db: PlatformDB,
    _admin: PlatformAdminUser,
) -> TenantResponse:
    """Create a new tenant."""
    # Check slug uniqueness.
    existing = await db.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "slug_taken", "message": "This slug is already in use."},
        )
    tenant = Tenant(
        slug=body.slug,
        name=body.name,
        kind=body.kind,
        registration_mode=body.registration_mode,
    )
    db.add(tenant)
    await db.flush()

    # Seed default branding for the new tenant within the same transaction.
    # Set app.tenant_id locally so the RLS WITH CHECK clause allows the row.
    from sqlalchemy import text

    from app.db.models.branding import TenantBranding
    from app.schemas.branding import DEFAULT_TOKENS

    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant.id)},
    )
    branding = TenantBranding(
        tenant_id=tenant.id,
        tokens=DEFAULT_TOKENS,
        display_name=tenant.name,
        version=1,
    )
    db.add(branding)
    await db.flush()

    return _to_tenant_response(tenant)


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse, dependencies=[Depends(platform_endpoint())])
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdateRequest,
    db: PlatformDB,
    _admin: PlatformAdminUser,
) -> TenantResponse:
    """Update a tenant's status or registration mode."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    updates: dict[str, object] = {}
    if body.status is not None:
        updates["status"] = body.status
    if body.registration_mode is not None:
        updates["registration_mode"] = body.registration_mode
    if body.name is not None:
        updates["name"] = body.name

    if updates:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(**updates))
        await db.refresh(tenant)

        # Invalidate branding cache on status change (e.g. suspended) or rename
        if body.status is not None or body.name is not None:
            try:
                from app.core.redis_client import get_redis
                redis = await get_redis()
                await redis.delete(f"cache:branding:slug:{tenant.slug}")
            except Exception:
                pass
    return _to_tenant_response(tenant)


@router.post(
    "/tenants/{tenant_id}/admins",
    response_model=CreateAdminResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(platform_endpoint())],
)
async def create_tenant_admin(
    tenant_id: uuid.UUID,
    body: CreateAdminRequest,
    platform_db: PlatformDB,
    _admin: PlatformAdminUser,
) -> CreateAdminResponse:
    """
    Create the first admin user for a tenant.
    A temporary password is emailed to the admin.
    """
    # Fetch the tenant via platform session.
    result = await platform_db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.status == "active")
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # We need a tenant-scoped session to create the user (RLS must be active).
    from app.core.context import set_request_context

    set_request_context(
        request_id="platform-create-admin",
        tenant_id=str(tenant.id),
    )

    async with SessionLocal() as tenant_db, tenant_db.begin():
        from sqlalchemy import text
        await tenant_db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant.id)},
        )
        user, temp_password = await create_admin_user(
            tenant=tenant,
            email=body.email,
            full_name=body.full_name,
            db=tenant_db,
        )
        user_id = str(user.id)

    login_url = f"{__import__('app.core.config', fromlist=['settings']).settings.FRONTEND_URL}/login?tenant={tenant.slug}"
    await send_temp_password_email(
        to_email=body.email,
        full_name=body.full_name,
        temp_password=temp_password,
        login_url=login_url,
        tenant_name=tenant.name,
    )
    return CreateAdminResponse(
        user_id=user_id,
        email=body.email,
        message="Admin created. Temporary password sent by email.",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_tenant_response(tenant: Tenant) -> TenantResponse:
    return TenantResponse(
        id=str(tenant.id),
        slug=tenant.slug,
        name=tenant.name,
        kind=tenant.kind,
        registration_mode=tenant.registration_mode,
        status=tenant.status,
        created_at=str(tenant.created_at),
    )

