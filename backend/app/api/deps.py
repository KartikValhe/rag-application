"""
app/api/deps.py
───────────────
Reusable FastAPI dependencies for authentication and DB session access.

Dependency hierarchy:
  get_tenant_session          → DB session locked to request's tenant via SET LOCAL.
  get_platform_session        → DB session for superadmin (no tenant context).
  get_current_user            → User from JWT + DB lookup.
  require_admin               → Like get_current_user but asserts is_admin=True.
  get_current_platform_admin  → PlatformAdmin from platform JWT.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import set_request_context
from app.core.security import JWTError, decode_access_token, decode_platform_token
from app.core.tenant_db import get_platform_session, tenant_session_context
from app.db.engine import SessionLocal
from app.db.models.tenant import PlatformAdmin, Tenant
from app.db.models.user import User

logger = structlog.get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)

# ── JWT extraction ────────────────────────────────────────────────────────────


def _extract_bearer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Extract the raw JWT from the Authorization: Bearer header."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "Authentication required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


# ── Tenant resolution from JWT ─────────────────────────────────────────────────


async def _resolve_tenant_from_jwt(
    request: Request,
    token: str = Depends(_extract_bearer),
) -> str:
    """
    Decode the JWT, validate that the token's tenant matches the request's
    host-resolved tenant slug, and return the tenant_id string.

    This is the cross-tenant JWT check described in exit criteria:
    "A JWT for tenant A is rejected on tenant B's host."
    """
    try:
        payload = decode_access_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    tenant_id = str(payload.get("tenant_id", ""))
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Missing tenant_id claim."},
        )

    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Invalid tenant_id."},
        )

    # Validate tenant exists and is active on every authenticated request
    host_slug: str | None = getattr(request.state, "tenant_slug", None)
    async with SessionLocal() as db, db.begin():
        result = await db.execute(
            select(Tenant).where(Tenant.id == tenant_uuid)
        )
        tenant = result.scalar_one_or_none()
        if tenant is None or tenant.status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "tenant_inactive", "message": "Tenant is suspended or inactive."},
            )
        if host_slug and tenant.slug != host_slug:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "tenant_mismatch",
                    "message": "Token was issued for a different tenant.",
                },
            )

    # Store tenant_id in context so logging and downstream code can read it.
    set_request_context(
        request_id=getattr(request.state, "request_id", ""),
        tenant_id=tenant_id,
        tenant_slug=host_slug,
    )
    return tenant_id


# ── Tenant session dependency ─────────────────────────────────────────────────


async def get_tenant_session(
    tenant_id: str = Depends(_resolve_tenant_from_jwt),
) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency: yields a DB session locked to the caller's tenant
    via SET LOCAL app.tenant_id.
    """
    async with tenant_session_context(tenant_id) as session:
        yield session


# ── Current user ─────────────────────────────────────────────────────────────


async def get_current_user(
    tenant_id: str = Depends(_resolve_tenant_from_jwt),
    token: str = Depends(_extract_bearer),
    db: AsyncSession = Depends(get_tenant_session),
) -> User:
    """Return the authenticated User, fetched from the tenant-scoped DB session."""
    try:
        payload = decode_access_token(token)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc

    user_id = uuid.UUID(str(payload.get("sub")))
    tid = uuid.UUID(tenant_id)

    result = await db.execute(
        select(User).where(User.tenant_id == tid, User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_not_found"},
        )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Like get_current_user but requires is_admin=True."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_required", "message": "Admin access required."},
        )
    return user


# ── Platform admin (superadmin) ───────────────────────────────────────────────


async def get_current_platform_admin(
    token: str = Depends(_extract_bearer),
    db: AsyncSession = Depends(get_platform_session),
) -> PlatformAdmin:
    """Return the authenticated PlatformAdmin from a platform JWT."""
    try:
        payload = decode_platform_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token"},
        ) from exc

    admin_id = uuid.UUID(str(payload.get("sub")))
    result = await db.execute(
        select(PlatformAdmin).where(
            PlatformAdmin.id == admin_id, PlatformAdmin.is_active.is_(True)
        )
    )
    admin = result.scalar_one_or_none()
    if admin is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return admin


# ── Convenience type aliases (use in route signatures for readability) ────────

CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
PlatformAdminUser = Annotated[PlatformAdmin, Depends(get_current_platform_admin)]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_session)]
PlatformDB = Annotated[AsyncSession, Depends(get_platform_session)]
