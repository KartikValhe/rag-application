"""
tests/test_platform.py
──────────────────────
Integration tests for superadmin (platform) endpoints.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenant import PlatformAdmin, Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def _platform_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_platform_login_success(client: AsyncClient, platform_admin: PlatformAdmin) -> None:
    resp = await client.post(
        "/api/v1/platform/auth/login",
        json={"email": platform_admin.email, "password": "SuperSecret123!"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


async def test_platform_login_invalid_password(client: AsyncClient, platform_admin: PlatformAdmin) -> None:
    resp = await client.post(
        "/api/v1/platform/auth/login",
        json={"email": platform_admin.email, "password": "WrongPassword!"},
    )
    assert resp.status_code == 401


async def test_create_and_list_tenants(client: AsyncClient, platform_token: str) -> None:
    headers = _platform_headers(platform_token)
    slug = f"acme-{uuid.uuid4().hex}"

    # Create tenant
    create_resp = await client.post(
        "/api/v1/platform/tenants",
        headers=headers,
        json={
            "slug": slug,
            "name": "Acme University",
            "kind": "college",
            "registration_mode": "invite_only",
        },
    )
    assert create_resp.status_code == 201
    tenant_data = create_resp.json()
    assert tenant_data["slug"] == slug
    assert tenant_data["name"] == "Acme University"
    assert tenant_data["status"] == "active"

    # List tenants
    list_resp = await client.get("/api/v1/platform/tenants?limit=200", headers=headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] >= 1
    slugs = [t["slug"] for t in list_data["items"]]
    assert slug in slugs


async def test_update_tenant_status(
    client: AsyncClient, platform_token: str, tenant_a: Tenant
) -> None:
    headers = _platform_headers(platform_token)
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_a.id}",
        headers=headers,
        json={"status": "suspended"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"


async def test_create_tenant_first_admin(
    client: AsyncClient, platform_token: str, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    headers = _platform_headers(platform_token)
    admin_email = f"dean-{uuid.uuid4().hex}@example.com"

    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_a.id}/admins",
        headers=headers,
        json={"email": admin_email, "full_name": "Dean Winchester"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == admin_email

    # Check user was created in the database with is_admin=True
    res = await raw_db.execute(
        select(User).where(User.tenant_id == tenant_a.id, User.email == admin_email)
    )
    user = res.scalar_one_or_none()
    assert user is not None
    assert user.is_admin is True
    assert user.status == "active"

