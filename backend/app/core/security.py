"""
app/core/security.py
────────────────────
All cryptographic helpers live here.  No other module does hashing or
JWT creation/verification — it all goes through these functions.

Design choices (see docs/adr/003-auth.md):
  - Passwords: argon2id via argon2-cffi (memory-hard, side-channel resistant).
  - JWTs: HS256 signed with separate secrets for tenant and platform tokens.
  - Refresh tokens: 32 random bytes, stored hashed (sha-256) in the DB so a
    DB dump does not expose live session tokens.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
import jwt
from jwt.exceptions import PyJWTError as JWTError

from app.core.config import settings

# ── Password hashing ─────────────────────────────────────────────────────────

# argon2-cffi's high-level PasswordHasher with OWASP-recommended parameters.
_ph = PasswordHasher(
    time_cost=2,       # number of iterations
    memory_cost=65536, # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(plaintext: str) -> str:
    """Return an argon2id hash of *plaintext*."""
    return _ph.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """
    Return True if *plaintext* matches the stored *hashed* value.
    Returns False (never raises) if the hash does not match.
    """
    try:
        return _ph.verify(hashed, plaintext)
    except VerifyMismatchError:
        return False


def password_needs_rehash(hashed: str) -> bool:
    """True when the stored hash uses outdated parameters and should be updated."""
    return _ph.check_needs_rehash(hashed)


# ── JWT helpers ──────────────────────────────────────────────────────────────

ALGORITHM = "HS256"


def _make_token(
    payload: dict[str, object],
    secret: str,
    expire_minutes: int,
) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=expire_minutes)
    return str(
        jwt.encode(
            {**payload, "exp": expire, "iat": datetime.now(UTC)},
            secret,
            algorithm=ALGORITHM,
        )
    )


def create_access_token(
    *,
    tenant_id: str,
    user_id: str,
    is_admin: bool = False,
    email: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    """
    Create a short-lived access token for a tenant user.
    Claims: sub=user_id, tenant_id, is_admin, type="access", optional email, roles, permissions.
    """
    payload: dict[str, object] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "is_admin": is_admin,
        "type": "access",
    }
    if email is not None:
        payload["email"] = email
    if roles is not None:
        payload["roles"] = roles
    if permissions is not None:
        payload["permissions"] = permissions

    return _make_token(
        payload=payload,
        secret=settings.SECRET_KEY,
        expire_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
    )


def create_platform_access_token(*, platform_admin_id: str) -> str:
    """Create a short-lived access token for the superadmin."""
    return _make_token(
        payload={
            "sub": platform_admin_id,
            "type": "platform_access",
        },
        secret=settings.PLATFORM_SECRET_KEY,
        expire_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
    )


def decode_access_token(token: str) -> dict[str, object]:
    """
    Decode and validate a tenant access token.
    Raises JWTError on any failure (expired, bad signature, wrong type).
    """
    payload = cast(
        dict[str, object],
        jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        ),
    )
    if payload.get("type") != "access":
        raise JWTError("Wrong token type")
    return payload


def decode_platform_token(token: str) -> dict[str, object]:
    """Decode and validate a platform (superadmin) access token."""
    payload = cast(
        dict[str, object],
        jwt.decode(
            token,
            settings.PLATFORM_SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        ),
    )
    if payload.get("type") != "platform_access":
        raise JWTError("Wrong token type")
    return payload


# ── Network / Proxy helpers ──────────────────────────────────────────────────

def get_client_ip(request: Request | None) -> str | None:
    """
    Extract client IP address from a request.
    Only trusts X-Forwarded-For if settings.TRUST_PROXY is True;
    otherwise falls back to request.client.host.
    """
    if request is None:
        return None
    if settings.TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return None


# ── Refresh token helpers ────────────────────────────────────────────────────

def generate_refresh_token() -> str:
    """Return a URL-safe 48-byte random string (the raw token sent to the client)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    """Return the SHA-256 hex digest of *raw* — stored in the DB."""
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Email token helpers ──────────────────────────────────────────────────────

def generate_email_token() -> str:
    """Return a URL-safe 32-byte token for email verification / password reset."""
    return secrets.token_urlsafe(32)


def hash_email_token(raw: str) -> str:
    """SHA-256 hex digest stored in the DB."""
    return hashlib.sha256(raw.encode()).hexdigest()

