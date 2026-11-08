"""
app/api/v1/auth.py
──────────────────
Public auth routes: register, login, refresh, logout, verify-email,
forgot-password, reset-password.

The refresh token is transported as an HttpOnly cookie so it is never
accessible to JavaScript.  The access token is returned in the response body.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_email_token, hash_refresh_token
from app.core.tenant_db import get_platform_session, tenant_session_context
from app.db.models.auth import EmailToken, RefreshToken
from app.db.models.tenant import Tenant
from app.services.auth import (
    get_tenant_by_slug,
    login_user,
    logout_user,
    refresh_access_token,
    register_user,
    request_password_reset,
    reset_password,
    verify_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Cookie name used for the refresh token.
REFRESH_COOKIE = "refresh_token"
COOKIE_MAX_AGE = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    """Write the refresh token as an HttpOnly, Secure, SameSite=Lax cookie."""
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=raw_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=not settings.is_development,  # Secure=False only in HTTP dev mode.
        samesite="lax",
        path="/api/v1/auth",  # Narrow the cookie scope to auth endpoints.
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path="/api/v1/auth")


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=8, max_length=128)
    tenant_slug: str  # Which tenant to register with.


class RegisterResponse(BaseModel):
    user_id: str
    email: str
    status: str
    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_slug: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    is_admin: bool


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


class VerifyEmailRequest(BaseModel):
    token: str
    tenant_slug: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    tenant_slug: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=128)
    tenant_slug: str | None = None


# ── Route helpers ─────────────────────────────────────────────────────────────

async def _get_tenant(slug: str, db: AsyncSession) -> Tenant:
    """Shared helper to fetch and validate a tenant by slug."""
    return await get_tenant_by_slug(slug, db)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> RegisterResponse:
    """Register a new user with a tenant (respects registration_mode)."""
    tenant = await _get_tenant(body.tenant_slug, platform_db)
    async with tenant_session_context(str(tenant.id)) as tenant_db:
        user = await register_user(
            tenant=tenant,
            email=body.email,
            full_name=body.full_name,
            password=body.password,
            db=tenant_db,
            platform_db=platform_db,
        )
    msg = (
        "Account created. Please verify your email."
        if user.status == "active"
        else "Account created. Awaiting admin approval."
    )
    return RegisterResponse(
        user_id=str(user.id),
        email=user.email,
        status=user.status,
        message=msg,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> LoginResponse:
    """Login and receive access token (body) + refresh token (HttpOnly cookie)."""
    tenant = await _get_tenant(body.tenant_slug, platform_db)
    async with tenant_session_context(str(tenant.id)) as tenant_db:
        user, access_token, raw_refresh = await login_user(
            tenant_id=str(tenant.id),
            email=body.email,
            password=body.password,
            user_agent=request.headers.get("user-agent"),
            db=tenant_db,
        )
    # The refresh token cookie stores "{tenant_id}:{token}" so rotation can run under tenant RLS
    _set_refresh_cookie(response, f"{tenant.id}:{raw_refresh}")
    return LoginResponse(
        access_token=access_token,
        user_id=str(user.id),
        is_admin=user.is_admin,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> RefreshResponse:
    """Rotate the refresh token and return a new access token."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_refresh_token"},
        )

    # Format is "{tenant_id}:{raw_token}"
    if ":" in refresh_token:
        tenant_id_str, raw_token = refresh_token.split(":", 1)
    else:
        tenant_id_str, raw_token = "", refresh_token

    if not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Session expired."},
        )

    try:
        tenant_uuid = uuid.UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Session expired."},
        )

    # Ensure tenant exists and is active
    result = await platform_db.execute(
        select(Tenant).where(Tenant.id == tenant_uuid)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None or tenant.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "tenant_not_found"},
        )

    # Rotate token inside the tenant's isolated session (RLS active)
    async with tenant_session_context(tenant_id_str) as tenant_db:
        new_access, new_refresh = await refresh_access_token(
            tenant_id=tenant_id_str,
            raw_refresh_token=raw_token,
            db=tenant_db,
        )
    _set_refresh_cookie(response, f"{tenant.id}:{new_refresh}")
    return RefreshResponse(access_token=new_access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    """Revoke the refresh token and clear the cookie."""
    if refresh_token:
        tenant_id_str, raw_token = (
            refresh_token.split(":", 1) if ":" in refresh_token else ("", refresh_token)
        )
        if tenant_id_str:
            try:
                async with tenant_session_context(tenant_id_str) as tenant_db:
                    await logout_user(
                        tenant_id=tenant_id_str,
                        raw_refresh_token=raw_token,
                        db=tenant_db,
                    )
            except Exception:
                pass
    _clear_refresh_cookie(response)


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email_route(
    body: VerifyEmailRequest,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> MessageResponse:
    # Find token row to identify the tenant.
    token_h = hash_email_token(body.token)
    result = await platform_db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == token_h,
            EmailToken.purpose == "verify_email",
        )
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Link is invalid or has expired."},
        )

    async with tenant_session_context(str(token_row.tenant_id)) as tenant_db:
        await verify_email(raw_token=body.token, db=tenant_db)
    return MessageResponse(message="Email verified successfully.")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> MessageResponse:
    tenant = await _get_tenant(body.tenant_slug, platform_db)
    async with tenant_session_context(str(tenant.id)) as tenant_db:
        await request_password_reset(email=body.email, tenant=tenant, db=tenant_db)
    return MessageResponse(message="If that email exists, a reset link has been sent.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password_route(
    body: ResetPasswordRequest,
    platform_db: AsyncSession = Depends(get_platform_session),
) -> MessageResponse:
    token_h = hash_email_token(body.token)
    result = await platform_db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == token_h,
            EmailToken.purpose == "reset_password",
        )
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Link is invalid or has expired."},
        )

    async with tenant_session_context(str(token_row.tenant_id)) as tenant_db:
        await reset_password(
            raw_token=body.token, new_password=body.new_password, db=tenant_db
        )
    return MessageResponse(message="Password reset successfully.")
