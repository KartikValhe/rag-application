"""
tests/test_auth.py
───────────────────
Integration tests for auth endpoints and services.

Tests run against a real Postgres + Redis (started by docker-compose).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient, tenant_slug: str, email: str, password: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Registration ──────────────────────────────────────────────────────────────

async def test_register_open_mode_creates_active_user(
    client: AsyncClient,
    raw_db: AsyncSession,
) -> None:
    """Users can self-register on open-mode tenants."""
    tenant = Tenant(slug=f"open-{uuid.uuid4().hex[:6]}", name="Open Tenant", kind="school",
                    registration_mode="open")
    raw_db.add(tenant)
    await raw_db.commit()

    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@example.com",
            "full_name": "New User",
            "password": "Password123!",
            "tenant_slug": tenant.slug,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"


async def test_register_approval_mode_creates_pending_user(
    client: AsyncClient,
    raw_db: AsyncSession,
) -> None:
    tenant = Tenant(slug=f"appr-{uuid.uuid4().hex[:6]}", name="Approval Tenant", kind="school",
                    registration_mode="approval")
    raw_db.add(tenant)
    await raw_db.commit()

    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "pending@example.com",
            "full_name": "Pending User",
            "password": "Password123!",
            "tenant_slug": tenant.slug,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


async def test_register_invite_only_mode_blocked(
    client: AsyncClient,
    tenant_a: Tenant,
) -> None:
    """Registration on invite_only tenants must be blocked."""
    # tenant_a fixture uses default registration_mode="invite_only"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "blocked@example.com",
            "full_name": "Blocked User",
            "password": "Password123!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "registration_closed"


async def test_register_duplicate_email_returns_409(
    client: AsyncClient,
    raw_db: AsyncSession,
) -> None:
    tenant = Tenant(slug=f"dup-{uuid.uuid4().hex[:6]}", name="Dup Tenant", kind="school",
                    registration_mode="open")
    raw_db.add(tenant)
    await raw_db.commit()

    payload = {
        "email": "dup@example.com",
        "full_name": "Dup User",
        "password": "Password123!",
        "tenant_slug": tenant.slug,
    }
    r1 = await client.post("/api/v1/auth/register", json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/auth/register", json=payload)
    assert r2.status_code == 409


# ── Login ─────────────────────────────────────────────────────────────────────

async def test_login_success_returns_tokens(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in resp.cookies


async def test_login_wrong_password_returns_401(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "WrongPass999!", "tenant_slug": tenant_a.slug},
    )
    assert resp.status_code == 401


async def test_login_nonexistent_email_returns_401(
    client: AsyncClient,
    tenant_a: Tenant,
) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "Any123!", "tenant_slug": tenant_a.slug},
    )
    assert resp.status_code == 401


async def test_login_deactivated_user_returns_403(
    client: AsyncClient,
    raw_db: AsyncSession,
    tenant_a: Tenant,
) -> None:
    from app.core.security import hash_password
    user = User(
        tenant_id=tenant_a.id,
        email=f"deact-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Deactivated",
        password_hash=hash_password("Password123!"),
        status="deactivated",
    )
    raw_db.add(user)
    await raw_db.commit()

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "account_deactivated"


# ── JWT cross-tenant rejection ────────────────────────────────────────────────

async def test_jwt_tenant_mismatch_rejected(
    client: AsyncClient,
    tenant_a: Tenant,
    tenant_b: Tenant,
    user_a: User,
    admin_a: User,
) -> None:
    """
    A JWT issued for tenant A must be rejected on a request scoped to tenant B.
    This is the cross-tenant JWT rejection test from Phase 1 exit criteria.
    """
    # Login as tenant A's user to get a token.
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    token_a = login_resp.json()["access_token"]

    # Attempt to use tenant A's token on a tenant B host.
    # We simulate this by passing X-Tenant-Slug: tenant_b.slug.
    resp = await client.get(
        "/api/v1/admin/users",
        headers={
            "Authorization": f"Bearer {token_a}",
            "X-Tenant-Slug": tenant_b.slug,
        },
    )
    assert resp.status_code == 403, (
        f"Expected 403 for cross-tenant JWT use, got {resp.status_code}: {resp.text}"
    )


# ── Token refresh ─────────────────────────────────────────────────────────────

async def test_refresh_rotates_token(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    # Login to get initial refresh cookie.
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login_resp.status_code == 200

    refresh_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
        cookies=login_resp.cookies,
    )
    assert refresh_resp.status_code == 200
    assert "access_token" in refresh_resp.json()
    # A new refresh cookie should be set.
    assert "refresh_token" in refresh_resp.cookies


# ── Logout ────────────────────────────────────────────────────────────────────

async def test_logout_clears_session(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login_resp.status_code == 200

    logout_resp = await client.post(
        "/api/v1/auth/logout",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
        cookies=login_resp.cookies,
    )
    assert logout_resp.status_code == 204

    # Using the old refresh cookie should now fail.
    refresh_resp = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"},
        cookies=login_resp.cookies,
    )
    assert refresh_resp.status_code == 401

