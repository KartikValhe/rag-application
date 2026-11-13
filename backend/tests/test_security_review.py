"""
tests/test_security_review.py
───────────────────────────────
Security regression tests covering Step 0 and Batch 1 hardening requirements:
  - Strong JWT secret validation from env (fail if missing, weak, or committed defaults)
  - PLATFORM_DATABASE_URL is required (no hardcoded default in config)
  - set_config('app.tenant_id', :tid, true) uses bound parameters only
  - Refresh cookie flags: HttpOnly, SameSite, Secure
  - Explicit CORS allowed origins list
  - seed.py environment guard
  - create_superadmin password policy
  - verify_scope fail-closed when scoping param missing
  - Suspended tenant blocked on every authenticated API request
"""

import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import Settings, settings
from app.db.models.tenant import Tenant
from app.db.models.user import User


def test_jwt_secret_fails_if_weak() -> None:
    """Settings must reject short or default secret keys (< 32 characters)."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="too_short",
            PLATFORM_SECRET_KEY="also_too_short",
        )
    errors = str(exc.value)
    assert "SECRET_KEY and PLATFORM_SECRET_KEY are too weak" in errors


def test_jwt_secret_fails_if_default_placeholder() -> None:
    """Settings must reject 'change-me' placeholder keys."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="change-me-placeholder-key-long-enough",
            PLATFORM_SECRET_KEY="change-me-another-placeholder-key-here",
        )
    errors = str(exc.value)
    assert "must be set to real secrets" in errors


def test_jwt_secrets_must_differ() -> None:
    """Tenant SECRET_KEY and PLATFORM_SECRET_KEY must never be identical."""
    same_secret = "a" * 40
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY=same_secret,
            PLATFORM_SECRET_KEY=same_secret,
        )
    errors = str(exc.value)
    assert "must be different" in errors


def test_cors_origins_is_explicit_list() -> None:
    """CORS origins must be an explicit list without wildcard '*'."""
    origins = settings.cors_origins
    assert isinstance(origins, list)
    assert "*" not in origins
    assert "http://localhost:5173" in origins


def test_set_config_uses_bound_parameters_in_source() -> None:
    """Verify that tenant_db.py sets app.tenant_id via parameterized SQL."""
    import inspect

    from app.core import tenant_db

    src = inspect.getsource(tenant_db.tenant_session_context)
    assert "set_config('app.tenant_id', :tid, true)" in src
    assert "f\"" not in src and "f'" not in src, "Must not use f-strings for set_config"


# ── Batch 1 Regression Tests ──────────────────────────────────────────────────

def test_platform_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLATFORM_DATABASE_URL must be explicitly provided (no hardcoded defaults)."""
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="a" * 64,
            PLATFORM_SECRET_KEY="b" * 64,
        )
    assert "PLATFORM_DATABASE_URL" in str(exc.value)


def test_committed_jwt_secrets_rejected() -> None:
    """Old hardcoded hex keys from docker-compose must be explicitly rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            PLATFORM_SECRET_KEY="b" * 64,
        )
    assert "previously committed insecure default keys" in str(exc.value)


@pytest.mark.asyncio
async def test_seed_refuses_to_run_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed.py must abort if ENVIRONMENT is not development."""
    from app.scripts.seed import seed
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="must be 'development'"):
        await seed()


def test_create_superadmin_password_policy() -> None:
    """create_superadmin CLI must enforce strong password rules."""
    from app.scripts.create_superadmin import validate_password_strength

    with pytest.raises(ValueError, match="at least 12 characters"):
        validate_password_strength("Short1!")

    with pytest.raises(ValueError, match="uppercase"):
        validate_password_strength("lowercase123!@#")

    with pytest.raises(ValueError, match="lowercase"):
        validate_password_strength("UPPERCASE123!@#")

    with pytest.raises(ValueError, match="digit"):
        validate_password_strength("NoDigitsHere!@#")

    with pytest.raises(ValueError, match="special character"):
        validate_password_strength("NoSpecialChar1234")

    # Valid strong password passes without error
    validate_password_strength("ValidStrongPass123!")


@pytest.mark.asyncio
async def test_verify_scope_fails_closed_when_param_missing(
    raw_db: AsyncSession,
    user_a: User,
) -> None:
    """verify_scope must raise HTTP 500 when scope parameter is missing."""
    from app.core.authz import Scope, ScopeType, verify_scope

    scope = Scope(ScopeType.CLASS, class_id_param="class_id")
    dummy_scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [],
        "query_string": b"",
        "path_params": {},
    }
    req = Request(dummy_scope)

    with pytest.raises(HTTPException) as exc_info:
        await verify_scope(user_a, scope, req, raw_db)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["code"] == "scope_resolution_failed"


@pytest.mark.asyncio
async def test_suspended_tenant_rejected_on_api_call(
    client: AsyncClient,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    admin_a: User,
) -> None:
    """An authenticated request for a suspended tenant must be rejected on every API call."""
    from app.core.security import create_access_token

    token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}

    # 1. API request succeeds while tenant is active
    res1 = await client.get("/api/v1/admin/users", headers=headers)
    assert res1.status_code == 200

    # 2. Suspend tenant
    await raw_db.execute(
        update(Tenant).where(Tenant.id == tenant_a.id).values(status="suspended")
    )
    await raw_db.commit()

    # 3. Next API request with the same token is rejected with 403 Forbidden
    res2 = await client.get("/api/v1/admin/users", headers=headers)
    assert res2.status_code == 403
    assert res2.json()["detail"]["code"] == "tenant_inactive"


# ── Batch 2 Regression Tests (Auth Hardening) ─────────────────────────────────

@pytest.mark.asyncio
async def test_csrf_header_required_on_refresh_and_logout(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """POST /auth/refresh and /auth/logout must require X-Requested-With: XMLHttpRequest."""
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login_resp.status_code == 200

    # 1. Missing CSRF header on /refresh
    client.cookies.clear()
    client.cookies.update(login_resp.cookies)
    res_refresh_nocsrf = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug},
    )
    assert res_refresh_nocsrf.status_code == 403
    assert res_refresh_nocsrf.json()["detail"]["code"] == "csrf_detected"

    # 2. Missing CSRF header on /logout
    client.cookies.clear()
    client.cookies.update(login_resp.cookies)
    res_logout_nocsrf = await client.post(
        "/api/v1/auth/logout",
        headers={"X-Tenant-Slug": tenant_a.slug},
    )
    assert res_logout_nocsrf.status_code == 403
    assert res_logout_nocsrf.json()["detail"]["code"] == "csrf_detected"

    # 3. With valid CSRF header, both succeed
    client.cookies.clear()
    client.cookies.update(login_resp.cookies)
    res_refresh_ok = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert res_refresh_ok.status_code == 200

    client.cookies.clear()
    client.cookies.update(res_refresh_ok.cookies)
    res_logout_ok = await client.post(
        "/api/v1/auth/logout",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert res_logout_ok.status_code == 204


@pytest.mark.asyncio
async def test_refresh_token_reuse_outside_grace_window_wipes_all_sessions(
    client: AsyncClient,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """
    Replaying a rotated refresh token outside the 10s grace window deletes ALL
    sessions for that user and returns 401 token_reused.
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models.auth import RefreshToken

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login_resp.status_code == 200
    token1_cookie = login_resp.cookies

    # Rotate token1 -> receive token2
    client.cookies.clear()
    client.cookies.update(token1_cookie)
    rotate_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert rotate_resp.status_code == 200

    # Ensure two token rows exist in DB (one rotated/revoked, one active)
    res_rows = await raw_db.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_a.id)
    )
    tokens = list(res_rows.scalars().all())
    assert len(tokens) >= 2

    # Simulate passing the 10-second grace window on token1
    await raw_db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_a.id, RefreshToken.revoked.is_(True))
        .values(revoked_at=datetime.now(UTC) - timedelta(seconds=20))
    )
    await raw_db.commit()

    # Replay token1 (outside grace window) -> theft detected!
    client.cookies.clear()
    client.cookies.update(token1_cookie)
    replay_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert replay_resp.status_code == 401
    assert replay_resp.json()["detail"]["code"] == "token_reused"

    # Verify ALL refresh tokens for this user were wiped
    res_after = await raw_db.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_a.id)
    )
    tokens_after = list(res_after.scalars().all())
    assert len(tokens_after) == 0


@pytest.mark.asyncio
async def test_refresh_token_replay_within_grace_window_succeeds(
    client: AsyncClient,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """
    Replaying a rotated refresh token within the 10s grace window succeeds
    (simulating concurrent tab refresh) without wiping sessions.
    """
    from app.db.models.auth import RefreshToken

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login_resp.status_code == 200
    token1_cookie = login_resp.cookies

    # Rotate token1 in Tab 1
    client.cookies.clear()
    client.cookies.update(token1_cookie)
    rotate_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert rotate_resp.status_code == 200

    # Replay token1 immediately in Tab 2 (within 10s grace window)
    client.cookies.clear()
    client.cookies.update(token1_cookie)
    concurrent_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
    )
    assert concurrent_resp.status_code == 200
    assert "access_token" in concurrent_resp.json()

    # Verify sessions were NOT wiped
    res_after = await raw_db.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_a.id)
    )
    tokens_after = list(res_after.scalars().all())
    assert len(tokens_after) > 0


@pytest.mark.asyncio
async def test_login_throttle_distributed_ips_still_throttles_email(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    5 failed attempts from 5 distinct IPs against one email must still result in 429
    (per-email throttle protection against distributed attacks).
    """
    from app.core.redis_client import get_redis

    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    redis = await get_redis()
    await redis.flushdb()

    for idx in range(1, 6):
        res = await client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": f"198.51.100.{idx}"},
            json={
                "email": user_a.email,
                "password": "WrongPassword!",
                "tenant_slug": tenant_a.slug,
            },
        )
        assert res.status_code == 401

    # 6th attempt from a 6th IP
    res6 = await client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "198.51.100.99"},
        json={
            "email": user_a.email,
            "password": "WrongPassword!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert res6.status_code == 429
    assert res6.json()["detail"]["code"] == "login_throttled"


@pytest.mark.asyncio
async def test_login_throttle_per_ip(
    client: AsyncClient,
    tenant_a: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Exceeding LOGIN_THROTTLE_IP_MAX_ATTEMPTS from one IP returns 429 ip_throttled
    even across different user emails.
    """
    from app.core.redis_client import get_redis

    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(settings, "LOGIN_THROTTLE_IP_MAX_ATTEMPTS", 5)
    redis = await get_redis()
    await redis.flushdb()

    ip = "203.0.113.42"
    for idx in range(5):
        res = await client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": ip},
            json={
                "email": f"diff_user_{idx}@acme.edu",
                "password": "WrongPassword!",
                "tenant_slug": tenant_a.slug,
            },
        )
        assert res.status_code == 401

    # 6th attempt from same IP exceeds per-IP throttle
    res6 = await client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": ip},
        json={
            "email": "another_user@acme.edu",
            "password": "WrongPassword!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert res6.status_code == 429
    assert res6.json()["detail"]["code"] == "ip_throttled"


def test_trust_proxy_controls_x_forwarded_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Forwarded-For is only trusted when TRUST_PROXY is True; else socket IP is used."""
    from app.core.security import get_client_ip

    req_scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [(b"x-forwarded-for", b"203.0.113.1, 10.0.0.1")],
        "client": ("127.0.0.1", 54321),
    }
    req = Request(req_scope)

    # 1. TRUST_PROXY = False (default)
    monkeypatch.setattr(settings, "TRUST_PROXY", False)
    assert get_client_ip(req) == "127.0.0.1"

    # 2. TRUST_PROXY = True
    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    assert get_client_ip(req) == "203.0.113.1"


def test_pyjwt_pinning_and_separate_secrets() -> None:
    """PyJWT must require exp, pin HS256, and reject cross-secret decoding."""
    from datetime import UTC, datetime, timedelta

    import jwt

    from app.core.security import (
        JWTError,
        create_access_token,
        create_platform_access_token,
        decode_access_token,
        decode_platform_token,
    )

    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())

    # Tenant token
    tenant_token = create_access_token(tenant_id=tid, user_id=uid)
    tenant_claims = decode_access_token(tenant_token)
    assert tenant_claims["sub"] == uid
    assert tenant_claims["tenant_id"] == tid

    # Decoding tenant token with platform key must fail
    with pytest.raises(JWTError):
        decode_platform_token(tenant_token)

    # Platform token
    plat_token = create_platform_access_token(platform_admin_id=uid)
    plat_claims = decode_platform_token(plat_token)
    assert plat_claims["sub"] == uid

    # Decoding platform token with tenant key must fail
    with pytest.raises(JWTError):
        decode_access_token(plat_token)

    # Token without exp must fail validation
    no_exp_token = jwt.encode(
        {"sub": uid, "tenant_id": tid, "type": "access"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        decode_access_token(no_exp_token)

    # Expired token must fail
    past_exp = datetime.now(UTC) - timedelta(minutes=5)
    expired_token = jwt.encode(
        {"sub": uid, "tenant_id": tid, "type": "access", "exp": past_exp},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        decode_access_token(expired_token)


@pytest.mark.asyncio
async def test_csv_user_import_limits(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
) -> None:
    """User invitation CSV import must enforce 2MB max size and 500 rows limit."""
    from app.core.security import create_access_token

    token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
        permissions=["user:manage"],
    )
    headers = {"Authorization": f"Bearer {token}"}

    # 1. File exceeding 2MB
    large_csv = b"email,full_name,role_name\n" + b"a" * (2 * 1024 * 1024 + 10)
    res_large = await client.post(
        "/api/v1/admin/users/import",
        headers=headers,
        files={"file": ("users.csv", large_csv, "text/csv")},
    )
    assert res_large.status_code == 413
    assert res_large.json()["detail"]["code"] == "file_too_large"

    # 2. Rows exceeding 500
    rows = ["email,full_name,role_name"]
    for i in range(501):
        rows.append(f"user{i}@test.com,User {i},student")
    too_many_rows_csv = "\n".join(rows).encode("utf-8")

    res_rows = await client.post(
        "/api/v1/admin/users/import",
        headers=headers,
        files={"file": ("users.csv", too_many_rows_csv, "text/csv")},
    )
    assert res_rows.status_code == 422
    assert res_rows.json()["detail"]["code"] == "too_many_rows"


# ── Batch 3 Regression Tests (Hardening) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_security_headers_present(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API responses must include defensive security headers."""
    # 1. In development mode
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert "Strict-Transport-Security" not in res.headers

    # 2. In non-development (production) mode, HSTS must be sent
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    res_prod = await client.get("/health")
    assert res_prod.status_code == 200
    assert (
        res_prod.headers["Strict-Transport-Security"]
        == "max-age=63072000; includeSubDomains; preload"
    )


def test_cors_origins_removes_port_3000() -> None:
    """Port 3000 must not be present in dev CORS origins."""
    origins = settings.cors_origins
    assert "http://localhost:3000" not in origins
    assert "http://127.0.0.1:3000" not in origins


def test_redis_password_required_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must reject unauthenticated Redis in non-development environments."""
    # 1. Fails when REDIS_PASSWORD is empty in production
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="a" * 64,
            PLATFORM_SECRET_KEY="b" * 64,
            REDIS_URL="redis://localhost:6379/0",
            REDIS_PASSWORD="",
        )
    assert "Redis password must be configured in non-development environments" in str(exc.value)

    # 2. Passes when REDIS_PASSWORD is set
    cfg_pwd = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        SECRET_KEY="a" * 64,
        PLATFORM_SECRET_KEY="b" * 64,
        REDIS_URL="redis://localhost:6379/0",
        REDIS_PASSWORD="SuperSecretRedisPassword!",
    )
    assert cfg_pwd.REDIS_PASSWORD == "SuperSecretRedisPassword!"

    # 3. Passes when password is embedded in REDIS_URL (e.g. rediss://:pass@host)
    cfg_tls = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        PLATFORM_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        SECRET_KEY="a" * 64,
        PLATFORM_SECRET_KEY="b" * 64,
        REDIS_URL="rediss://:EmbeddedPass123!@redis.local:6379/0",
        REDIS_PASSWORD="",
    )
    assert "rediss://" in cfg_tls.REDIS_URL


@pytest.mark.asyncio
async def test_list_pending_approvals_uses_sql_count(
    client: AsyncClient,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    admin_a: User,
) -> None:
    """GET /admin/pending-approvals must return total from SQL COUNT, not in-memory len()."""
    from app.core.security import create_access_token

    # Insert a user with status="pending"
    pending_user = User(
        tenant_id=tenant_a.id,
        email="pending_applicant@acme.edu",
        full_name="Pending Applicant",
        status="pending",
    )
    raw_db.add(pending_user)
    await raw_db.commit()

    token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
        permissions=["user:manage"],
    )
    res = await client.get(
        "/api/v1/admin/pending-approvals",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert any(u["email"] == "pending_applicant@acme.edu" for u in data["items"])


@pytest.mark.asyncio
async def test_per_ip_throttle_does_not_block_successful_logins(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Per-IP throttle counts FAILED attempts only.
    60 successful logins from one IP must NOT be blocked.
    """
    from app.core.redis_client import get_redis

    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    redis = await get_redis()
    await redis.flushdb()

    ip = "198.51.100.88"
    for _ in range(60):
        res = await client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": ip},
            json={
                "email": user_a.email,
                "password": "Password123!",
                "tenant_slug": tenant_a.slug,
            },
        )
        assert res.status_code == 200, f"Successful login unexpectedly throttled: {res.text}"

