"""
app/services/auth.py
─────────────────────
Auth business logic: registration, login, token refresh, logout,
email verification, password reset.

All functions accept an `AsyncSession` already locked to a tenant (via
`get_tenant_session`) and work only on that tenant's data.

Login throttle uses Redis sliding-window counters keyed by
`throttle:login:{tenant_id}:{email}` so limits are per-tenant-per-email.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis_client import get_redis
from app.core.security import (
    generate_email_token,
    generate_refresh_token,
    hash_email_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.db.models.auth import EmailToken, RefreshToken
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.email import (
    send_password_reset_email,
    send_verification_email,
)

logger = structlog.get_logger(__name__)

# ── Throttle helpers ─────────────────────────────────────────────────────────

def _throttle_email_key(tenant_id: str, email: str) -> str:
    return f"throttle:login:{tenant_id}:{email.lower()}"


def _throttle_ip_key(ip: str) -> str:
    return f"throttle:login:ip:{ip}"


async def _check_throttle(
    tenant_id: str,
    email: str,
    ip: str | None = None,
) -> None:
    """
    Raise HTTP 429 if the user or IP has exceeded the allowed failed-login attempts
    within the sliding window. Does NOT increment counters.
    """
    redis = await get_redis()

    if ip:
        ip_val = await redis.get(_throttle_ip_key(ip))
        if ip_val and int(ip_val) >= settings.LOGIN_THROTTLE_IP_MAX_ATTEMPTS:
            ttl = await redis.ttl(_throttle_ip_key(ip))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "ip_throttled",
                    "message": "Too many failed login attempts from this IP. Try again later.",
                    "retry_after_seconds": max(ttl, 0),
                },
                headers={"Retry-After": str(max(ttl, 0))},
            )

    email_val = await redis.get(_throttle_email_key(tenant_id, email))
    if email_val and int(email_val) >= settings.LOGIN_THROTTLE_MAX_ATTEMPTS:
        ttl = await redis.ttl(_throttle_email_key(tenant_id, email))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "login_throttled",
                "message": "Too many failed login attempts. Try again later.",
                "retry_after_seconds": max(ttl, 0),
            },
            headers={"Retry-After": str(max(ttl, 0))},
        )


async def _record_login_failure(
    tenant_id: str,
    email: str,
    ip: str | None = None,
) -> None:
    """Increment failed login counters for the IP and email."""
    redis = await get_redis()
    if ip:
        ip_key = _throttle_ip_key(ip)
        ip_count = await redis.incr(ip_key)
        if ip_count == 1:
            await redis.expire(ip_key, settings.LOGIN_THROTTLE_WINDOW_SECONDS)

    email_key = _throttle_email_key(tenant_id, email)
    count = await redis.incr(email_key)
    if count == 1:
        await redis.expire(email_key, settings.LOGIN_THROTTLE_WINDOW_SECONDS)


async def _check_and_increment_throttle(
    tenant_id: str,
    email: str,
    ip: str | None = None,
) -> None:
    """Deprecated alias for backwards compatibility."""
    await _check_throttle(tenant_id, email, ip)


async def _clear_throttle(tenant_id: str, email: str) -> None:
    """Clear the email throttle counter after a successful login."""
    redis = await get_redis()
    await redis.delete(_throttle_email_key(tenant_id, email))


# ── Tenant lookup ────────────────────────────────────────────────────────────

async def get_tenant_by_slug(slug: str, db: AsyncSession) -> Tenant:
    """
    Fetch a tenant by slug using the platform session.
    Raises 404 if not found or not active.
    Note: this uses the platform (no-RLS) session so it can read the tenants table.
    """
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    if tenant is None or tenant.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "tenant_not_found", "message": "Tenant not found."},
        )
    return tenant


# ── Registration ─────────────────────────────────────────────────────────────

async def register_user(
    *,
    tenant: Tenant,
    email: str,
    full_name: str,
    password: str,
    db: AsyncSession,          # tenant-scoped session
    platform_db: AsyncSession, # platform session (to read tenant row)
) -> User:
    """
    Create a new user according to the tenant's registration_mode:
      - open        → user is created with status=active.
      - approval    → user is created with status=pending.
      - invite_only → registration is blocked (must use invitation flow).
    """
    if tenant.registration_mode == "invite_only":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "registration_closed",
                "message": "This organisation only accepts invited users.",
            },
        )

    # Check for duplicate email (the DB unique constraint catches races,
    # but we return a nicer error here).
    existing = await db.execute(
        select(User).where(User.email == email.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "email_taken", "message": "This email is already registered."},
        )

    initial_status = (
        "pending" if tenant.registration_mode == "approval" else "active"
    )

    user = User(
        tenant_id=tenant.id,
        email=email.lower(),
        full_name=full_name,
        password_hash=hash_password(password),
        status=initial_status,
        is_admin=False,
    )
    db.add(user)
    await db.flush()  # Populate user.id before sending email.

    if initial_status == "active":
        # Send email verification token.
        raw_token = generate_email_token()
        token_row = EmailToken(
            tenant_id=tenant.id,
            user_id=user.id,
            token_hash=hash_email_token(raw_token),
            purpose="verify_email",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        db.add(token_row)
        verify_url = (
            f"{settings.FRONTEND_URL}/verify-email?token={raw_token}"
            f"&tenant={tenant.slug}"
        )
        # Email is sent after the transaction commits (fire-and-forget for now).
        # Phase 8 moves this to an ARQ task queue.
        await send_verification_email(
            to_email=email,
            full_name=full_name,
            verify_url=verify_url,
            tenant_name=tenant.name,
        )

    logger.info("user_registered", user_id=str(user.id), status=initial_status)
    return user


# ── Login ────────────────────────────────────────────────────────────────────

async def login_user(
    *,
    tenant_id: str,
    email: str,
    password: str,
    user_agent: str | None,
    client_ip: str | None = None,
    db: AsyncSession,
) -> tuple[User, str, str]:
    """
    Authenticate a user.  Returns (user, access_token, raw_refresh_token).
    Raises 401 on bad credentials, 429 on throttle, 403 on inactive account.
    """
    # Throttle check runs BEFORE the DB query so attackers can't probe timing.
    await _check_throttle(tenant_id, email, ip=client_ip)

    result = await db.execute(select(User).where(User.email == email.lower()))
    user = result.scalar_one_or_none()

    if user is None or user.password_hash is None or not verify_password(
        password, user.password_hash
    ):
        await _record_login_failure(tenant_id, email, ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "Invalid email or password."},
        )

    if user.status == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "account_pending",
                "message": "Your account is awaiting approval.",
            },
        )
    if user.status == "deactivated":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "account_deactivated",
                "message": "This account has been deactivated.",
            },
        )

    # Rehash password if argon2 params were updated.
    if password_needs_rehash(user.password_hash):
        await db.execute(
            update(User)
            .where(User.tenant_id == user.tenant_id, User.id == user.id)
            .values(password_hash=hash_password(password))
        )

    # Clear throttle on success.
    await _clear_throttle(tenant_id, email)

    # Issue tokens.
    from app.core.security import create_access_token  # local import avoids circular
    from app.db.models.rbac import Role, UserRole
    from app.core.authz import load_user_permissions

    role_res = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == user.tenant_id, UserRole.user_id == user.id)
    )
    user_roles = list(role_res.scalars().all())
    if user.is_admin and "tenant_admin" not in user_roles:
        user_roles.append("tenant_admin")
    user_perms = list(await load_user_permissions(user, db))

    access_token = create_access_token(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
        is_admin=user.is_admin,
        email=user.email,
        roles=user_roles,
        permissions=user_perms,
    )
    raw_refresh = generate_refresh_token()
    refresh_row = RefreshToken(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(UTC)
        + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=user_agent,
    )
    db.add(refresh_row)

    logger.info("user_logged_in", user_id=str(user.id))
    return user, access_token, raw_refresh


# ── Token refresh ─────────────────────────────────────────────────────────────

async def refresh_access_token(
    *,
    tenant_id: str,
    raw_refresh_token: str,
    db: AsyncSession,
) -> tuple[str, str]:
    """
    Validate the refresh token, rotate it, and return a new (access, refresh) pair.
    Rotated refresh tokens are kept as revoked rows.
    If a revoked token is replayed outside the 10-second grace window,
    all sessions for that user are deleted (theft detection).
    Raises 401 on any invalid state.
    """
    hashed = hash_refresh_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hashed,
            RefreshToken.tenant_id == uuid.UUID(tenant_id),
        )
    )
    token_row = result.scalar_one_or_none()

    now = datetime.now(UTC)

    if token_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Session expired."},
        )

    expires_at = token_row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if expires_at is not None and expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Session expired."},
        )

    if token_row.revoked:
        revoked_at = token_row.revoked_at
        if revoked_at is not None and revoked_at.tzinfo is None:
            revoked_at = revoked_at.replace(tzinfo=UTC)

        # 10-second grace window for concurrent tab refreshes
        is_within_grace = (
            revoked_at is not None
            and (now - revoked_at) <= timedelta(seconds=10)
        )

        if not is_within_grace:
            # Token reuse detected! Revoke all tokens for this user
            await db.execute(
                delete(RefreshToken).where(
                    RefreshToken.tenant_id == token_row.tenant_id,
                    RefreshToken.user_id == token_row.user_id,
                )
            )
            await db.commit()  # Ensure session revocation persists before raising exception
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(token_row.user_id),
                tenant_id=tenant_id,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "token_reused",
                    "message": "Revoked refresh token reused; all sessions terminated.",
                },
            )
        # Within grace window: allow concurrent tab refresh to proceed

    # Revoke old token on first rotation
    if not token_row.revoked:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.id == token_row.id)
            .values(revoked=True, revoked_at=now)
        )

    # Fetch user to include current is_admin flag in new token.
    user_result = await db.execute(
        select(User).where(
            User.tenant_id == token_row.tenant_id,
            User.id == token_row.user_id,
        )
    )
    user = user_result.scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_inactive"},
        )

    from app.core.security import create_access_token
    from app.db.models.rbac import Role, UserRole
    from app.core.authz import load_user_permissions

    role_res = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == user.tenant_id, UserRole.user_id == user.id)
    )
    user_roles = list(role_res.scalars().all())
    if user.is_admin and "tenant_admin" not in user_roles:
        user_roles.append("tenant_admin")
    user_perms = list(await load_user_permissions(user, db))

    new_access = create_access_token(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
        is_admin=user.is_admin,
        email=user.email,
        roles=user_roles,
        permissions=user_perms,
    )
    new_raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=hash_refresh_token(new_raw_refresh),
            expires_at=datetime.now(UTC)
            + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    return new_access, new_raw_refresh


# ── Logout ────────────────────────────────────────────────────────────────────

async def logout_user(
    *,
    tenant_id: str,
    raw_refresh_token: str,
    db: AsyncSession,
) -> None:
    """Revoke the specific refresh token used for this session (preserve row)."""
    hashed = hash_refresh_token(raw_refresh_token)
    now = datetime.now(UTC)
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hashed,
            RefreshToken.tenant_id == uuid.UUID(tenant_id),
        )
        .values(
            revoked=True,
            revoked_at=now,
            expires_at=now - timedelta(seconds=1),
        )
    )


# ── Email verification ────────────────────────────────────────────────────────

async def verify_email(*, raw_token: str, db: AsyncSession) -> User:
    """Consume a verify_email token and mark the user's email as verified."""
    hashed = hash_email_token(raw_token)
    result = await db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == hashed,
            EmailToken.purpose == "verify_email",
        )
    )
    token_row = result.scalar_one_or_none()
    expires_at = token_row.expires_at if token_row else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if token_row is None or (expires_at is not None and expires_at < datetime.now(UTC)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Link is invalid or has expired."},
        )

    # Activate the user.
    user_result = await db.execute(
        select(User).where(
            User.tenant_id == token_row.tenant_id,
            User.id == token_row.user_id,
        )
    )
    user = user_result.scalar_one()
    await db.execute(
        update(User)
        .where(User.tenant_id == user.tenant_id, User.id == user.id)
        .values(status="active")
    )
    # Consume the token (delete after use — no replay window).
    await db.execute(delete(EmailToken).where(EmailToken.id == token_row.id))
    return user


# ── Password reset ────────────────────────────────────────────────────────────

async def request_password_reset(
    *, email: str, tenant: Tenant, db: AsyncSession
) -> None:
    """
    Generate a password-reset token and send the email.
    Always returns 200 even if the email is not found (prevents enumeration).
    """
    result = await db.execute(select(User).where(User.email == email.lower()))
    user = result.scalar_one_or_none()
    if user is None:
        return  # Silently ignore unknown email.

    raw_token = generate_email_token()
    # Remove any existing reset tokens for this user.
    await db.execute(
        delete(EmailToken).where(
            EmailToken.user_id == user.id,
            EmailToken.purpose == "reset_password",
        )
    )
    db.add(
        EmailToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=hash_email_token(raw_token),
            purpose="reset_password",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    reset_url = (
        f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"
        f"&tenant={tenant.slug}"
    )
    await send_password_reset_email(
        to_email=email,
        full_name=user.full_name,
        reset_url=reset_url,
        tenant_name=tenant.name,
    )


async def reset_password(*, raw_token: str, new_password: str, db: AsyncSession) -> None:
    """Consume a reset_password token and update the user's password."""
    hashed = hash_email_token(raw_token)
    result = await db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == hashed,
            EmailToken.purpose == "reset_password",
        )
    )
    token_row = result.scalar_one_or_none()
    expires_at = token_row.expires_at if token_row else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if token_row is None or (expires_at is not None and expires_at < datetime.now(UTC)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Link is invalid or has expired."},
        )
    await db.execute(
        update(User)
        .where(User.tenant_id == token_row.tenant_id, User.id == token_row.user_id)
        .values(password_hash=hash_password(new_password))
    )
    # Revoke all refresh tokens (session reset after password change).
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.tenant_id == token_row.tenant_id,
            RefreshToken.user_id == token_row.user_id,
        )
        .values(revoked=True)
    )
    await db.execute(delete(EmailToken).where(EmailToken.id == token_row.id))

