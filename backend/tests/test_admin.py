"""
tests/test_admin.py
───────────────────
Integration tests for tenant admin endpoints.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def _auth_headers(token: str, tenant_slug: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant_slug:
        headers["X-Tenant-Slug"] = tenant_slug
    return headers


async def test_admin_list_users(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, user_a: User
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )
    resp = await client.get(
        "/api/v1/admin/users",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    emails = [u["email"] for u in data["items"]]
    assert admin_a.email in emails
    assert user_a.email in emails


async def test_non_admin_cannot_access_admin_routes(
    client: AsyncClient, tenant_a: Tenant, user_a: User
) -> None:
    # user_a has is_admin=False
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(user_a.id), is_admin=False
    )
    resp = await client.get(
        "/api/v1/admin/users",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] in ("admin_required", "permission_denied")


async def test_admin_deactivate_user(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, user_a: User
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )
    resp = await client.patch(
        f"/api/v1/admin/users/{user_a.id}/deactivate",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deactivated"


async def test_admin_cannot_deactivate_self(
    client: AsyncClient, tenant_a: Tenant, admin_a: User
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )
    resp = await client.patch(
        f"/api/v1/admin/users/{admin_a.id}/deactivate",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert resp.status_code == 400


async def test_invite_user_and_accept(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )
    invited_email = f"invited-{uuid.uuid4().hex[:6]}@example.com"

    # Send invite
    invite_resp = await client.post(
        "/api/v1/admin/users/invite",
        headers=_auth_headers(token, tenant_a.slug),
        json={"email": invited_email, "role_name": "teacher"},
    )
    assert invite_resp.status_code == 201

    # Extract invitation token from database
    from app.db.models.auth import Invitation

    res = await raw_db.execute(
        select(Invitation).where(
            Invitation.tenant_id == tenant_a.id,
            Invitation.email == invited_email,
            Invitation.status == "pending",
        )
    )
    invitation = res.scalar_one()

    # In our service, token_hash is stored; we test acceptance by calling accept
    # with the raw token if known, or directly checking accept_invitation logic.
    # Notice `create_invitation` creates a raw_token and stores `token_hash = hash_email_token(raw_token)`.
    # Let's test accept_invitation via a known raw token.
    from app.core.security import generate_email_token, hash_email_token

    test_raw_token = generate_email_token()
    invitation.token_hash = hash_email_token(test_raw_token)
    await raw_db.commit()

    accept_resp = await client.post(
        f"/api/v1/admin/invitations/accept?token={test_raw_token}&full_name=Accepted+User&password=Password123!",
    )
    assert accept_resp.status_code == 200

    # User can now login
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": invited_email,
            "password": "Password123!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert login_resp.status_code == 200


async def test_bulk_csv_import(
    client: AsyncClient, tenant_a: Tenant, admin_a: User
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )

    csv_content = (
        "email,full_name,role_name\n"
        f"csv1-{uuid.uuid4().hex[:6]}@example.com,Student One,student\n"
        f"csv2-{uuid.uuid4().hex[:6]}@example.com,Student Two,student\n"
    ).encode()

    files = {"file": ("students.csv", csv_content, "text/csv")}
    resp = await client.post(
        "/api/v1/admin/users/import",
        headers=_auth_headers(token, tenant_a.slug),
        files=files,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["succeeded"] == 2
    assert data["failed"] == 0


async def test_registration_mode_toggle_and_approval(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )

    # 1. Update registration mode to 'approval'
    patch_resp = await client.patch(
        "/api/v1/admin/settings/registration-mode",
        headers=_auth_headers(token, tenant_a.slug),
        json={"registration_mode": "approval"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["registration_mode"] == "approval"

    # 2. Register a new user -> should have status 'pending'
    pending_email = f"pending-{uuid.uuid4().hex[:6]}@example.com"
    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": pending_email,
            "full_name": "Pending Approval",
            "password": "Password123!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert reg_resp.status_code == 201
    pending_user_id = reg_resp.json()["user_id"]
    assert reg_resp.json()["status"] == "pending"

    # 3. Admin lists pending approvals
    list_appr = await client.get(
        "/api/v1/admin/pending-approvals",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert list_appr.status_code == 200
    user_ids = [u["id"] for u in list_appr.json()["items"]]
    assert pending_user_id in user_ids

    # 4. Admin approves the user
    approve_resp = await client.post(
        f"/api/v1/admin/pending-approvals/{pending_user_id}/approve",
        headers=_auth_headers(token, tenant_a.slug),
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "active"
