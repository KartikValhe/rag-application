"""
tests/test_tenants_rls.py
─────────────────────────
Row level security and column grants on app.tenants (migration 0010, design A3).

What the three roles may do with the tenants table:
  app_tenant   sees only its own row (SELECT policy), reads only id, slug, status, timezone, and
               updates only its own timezone. No INSERT, no DELETE.
  app_platform sees and writes every row, but never changes id or slug and never sets an explicit id.
  app_worker   sees every row and reads only the id column (the sweeper, covered in test_isolation).

The second half checks that the pre-auth flows (login, refresh, register, forgot password, public
branding) and the per-request tenant check still work with these rules.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.core.tenant_db import tenant_session_context
from app.db.engine import PlatformSessionLocal, SessionLocal
from app.db.models.academic import Subject
from app.db.models.audit import AuditLog
from app.db.models.document import Document
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.schedule import (
    TenantRowNotVisibleError,
    get_tenant_timezone,
    get_tenant_zoneinfo,
    resolve_reference_time,
)

pytestmark = pytest.mark.asyncio

NON_DEFAULT_TZ = "Pacific/Auckland"


def _hdr(token: str, slug: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if slug:
        headers["X-Tenant-Slug"] = slug
    return headers


async def _set_tz(raw_db: AsyncSession, tenant_id: uuid.UUID, tz: str) -> None:
    await raw_db.execute(text("UPDATE app.tenants SET timezone = :tz WHERE id = :id"), {"tz": tz, "id": tenant_id})
    await raw_db.commit()


async def _timezone_of(raw_db: AsyncSession, tenant_id: uuid.UUID) -> str:
    return (await raw_db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))).scalar_one()


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)


async def _as_tenant(tenant_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Run one statement as app_tenant in the given tenant context (own transaction)."""
    async with tenant_session_context(str(tenant_id)) as session:
        res = await session.execute(text(sql), params or {})
        return list(res.fetchall()) if res.returns_rows else [res.rowcount]


async def _assert_tenant_denied(tenant_id: uuid.UUID, sql: str, params: dict[str, Any] | None = None) -> None:
    """The statement must fail with 'permission denied' (SQLSTATE 42501), not just return nothing."""
    with pytest.raises(DBAPIError) as exc_info:
        await _as_tenant(tenant_id, sql, params)
    assert _sqlstate(exc_info.value) == "42501" or "permission denied" in str(exc_info.value).lower(), (
        f"expected 42501 for: {sql}; got: {exc_info.value}"
    )


async def _as_platform(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    async with PlatformSessionLocal() as session, session.begin():
        res = await session.execute(text(sql), params or {})
        return list(res.fetchall()) if res.returns_rows else [res.rowcount]


async def _assert_platform_denied(sql: str, params: dict[str, Any] | None = None) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        await _as_platform(sql, params)
    assert _sqlstate(exc_info.value) == "42501" or "permission denied" in str(exc_info.value).lower(), (
        f"expected 42501 for: {sql}; got: {exc_info.value}"
    )


# ── app_tenant: which rows ───────────────────────────────────────────────────


async def test_app_tenant_sees_only_its_own_tenant_row(tenant_a: Tenant, tenant_b: Tenant) -> None:
    rows = await _as_tenant(tenant_a.id, "SELECT id, slug, status, timezone FROM app.tenants")
    assert [r[0] for r in rows] == [tenant_a.id]
    # Asking for the other tenant by id returns nothing, not an error and not the row.
    other = await _as_tenant(tenant_a.id, "SELECT id FROM app.tenants WHERE id = :b", {"b": tenant_b.id})
    assert other == []
    assert (await _as_tenant(tenant_a.id, "SELECT count(*) FROM app.tenants"))[0][0] == 1


async def test_app_tenant_sees_no_tenant_row_without_context(tenant_a: Tenant) -> None:
    # No set_config at all.
    async with SessionLocal() as session, session.begin():
        assert (await session.execute(text("SELECT id FROM app.tenants"))).fetchall() == []
    # An empty context value is treated like no context.
    async with SessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        assert (await session.execute(text("SELECT id FROM app.tenants"))).fetchall() == []
    # A context for a tenant that does not exist sees nothing either.
    assert await _as_tenant(uuid.uuid4(), "SELECT id FROM app.tenants") == []


async def test_app_tenant_cannot_update_another_tenants_timezone(
    tenant_a: Tenant, tenant_b: Tenant, raw_db: AsyncSession
) -> None:
    before_b = await _timezone_of(raw_db, tenant_b.id)
    target = NON_DEFAULT_TZ if before_b != NON_DEFAULT_TZ else "UTC"
    res = await _as_tenant(
        tenant_a.id, "UPDATE app.tenants SET timezone = :tz WHERE id = :b", {"tz": target, "b": tenant_b.id}
    )
    assert res == [0]
    assert await _timezone_of(raw_db, tenant_b.id) == before_b
    # A blanket UPDATE with no WHERE only reaches the caller's own row.
    res_all = await _as_tenant(tenant_a.id, "UPDATE app.tenants SET timezone = :tz", {"tz": target})
    assert res_all == [1]
    assert await _timezone_of(raw_db, tenant_b.id) == before_b


# ── app_tenant: which columns ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "select_list",
    ["db_dsn", "doc_limits", "name", "registration_mode", "kind", "created_at", "*", "tenants.*"],
)
async def test_app_tenant_cannot_select_columns_outside_its_grant(tenant_a: Tenant, select_list: str) -> None:
    await _assert_tenant_denied(tenant_a.id, f"SELECT {select_list} FROM app.tenants")


@pytest.mark.parametrize("column", ["db_dsn", "doc_limits", "name", "registration_mode"])
async def test_app_tenant_cannot_filter_or_sort_on_hidden_columns(tenant_a: Tenant, column: str) -> None:
    """A hidden column must not leak through WHERE or ORDER BY either."""
    await _assert_tenant_denied(tenant_a.id, f"SELECT id FROM app.tenants WHERE {column}::text IS NOT NULL")
    await _assert_tenant_denied(tenant_a.id, f"SELECT id FROM app.tenants ORDER BY {column}")


@pytest.mark.parametrize(
    "assignment",
    [
        "name = 'x'", "slug = 'hacked-slug'", "status = 'suspended'", "registration_mode = 'open'",
        "doc_limits = '{}'::jsonb", "db_dsn = 'postgres://x'", "id = gen_random_uuid()", "kind = 'other'",
    ],
)
async def test_app_tenant_can_update_only_the_timezone_column(tenant_a: Tenant, assignment: str) -> None:
    await _assert_tenant_denied(tenant_a.id, f"UPDATE app.tenants SET {assignment} WHERE id = :id", {"id": tenant_a.id})


async def test_app_tenant_cannot_insert_or_delete_tenants(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    await _assert_tenant_denied(
        tenant_a.id,
        "INSERT INTO app.tenants (slug, name, kind) VALUES (:slug, 'Rogue', 'school')",
        {"slug": f"rogue-{uuid.uuid4().hex[:8]}"},
    )
    await _assert_tenant_denied(tenant_a.id, "DELETE FROM app.tenants WHERE id = :id", {"id": tenant_a.id})
    still_there = (await raw_db.execute(select(Tenant.id).where(Tenant.id == tenant_a.id))).scalar_one_or_none()
    assert still_there == tenant_a.id


# ── app_tenant: the legitimate paths keep working ────────────────────────────


async def test_app_tenant_own_timezone_update_get_audit_and_schedule(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    tenant_id = tenant_a.id
    token = create_access_token(tenant_id=str(tenant_id), user_id=str(admin_a.id), is_admin=True)

    patch = await client.patch(
        "/api/v1/admin/settings/timezone", headers=_hdr(token, tenant_a.slug), json={"timezone": NON_DEFAULT_TZ}
    )
    assert patch.status_code == 200, patch.text
    get = await client.get("/api/v1/admin/settings/timezone", headers=_hdr(token, tenant_a.slug))
    assert get.status_code == 200
    assert get.json()["timezone"] == NON_DEFAULT_TZ
    assert await _timezone_of(raw_db, tenant_id) == NON_DEFAULT_TZ

    audit = (
        await raw_db.execute(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "tenant:update_timezone")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].details["new_timezone"] == NON_DEFAULT_TZ

    # The schedule code reads the timezone through the tenant session and uses it (not the default).
    async with tenant_session_context(str(tenant_id)) as session:
        tz_name = await get_tenant_timezone(session, tenant_id)
    assert tz_name == NON_DEFAULT_TZ
    from datetime import UTC, datetime

    local = resolve_reference_time(datetime(2026, 1, 15, 12, 0, tzinfo=UTC), get_tenant_zoneinfo(tz_name))
    assert local.utcoffset() is not None and local.utcoffset().total_seconds() == 13 * 3600  # NZDT in January


async def test_schedule_raises_when_the_tenant_row_is_hidden(tenant_a: Tenant, tenant_b: Tenant) -> None:
    """No silent Asia/Kolkata fallback: another tenant's row, or no row at all, is an explicit error."""
    async with tenant_session_context(str(tenant_a.id)) as session:
        with pytest.raises(TenantRowNotVisibleError):
            await get_tenant_timezone(session, tenant_b.id)
        with pytest.raises(TenantRowNotVisibleError):
            await get_tenant_timezone(session, uuid.uuid4())
    # No tenant context at all: the own row is hidden too.
    async with SessionLocal() as session, session.begin():
        with pytest.raises(TenantRowNotVisibleError):
            await get_tenant_timezone(session, tenant_a.id)


async def test_app_tenant_can_still_insert_child_rows_that_reference_tenants(
    tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    """
    Foreign keys to app.tenants are checked by the system, not as app_tenant: the narrower column
    grants and the RLS policies on tenants must not break inserts of users, subjects and documents.
    """
    tenant_id = tenant_a.id
    user_id = uuid.uuid4()
    async with tenant_session_context(str(tenant_id)) as session:
        session.add(
            User(
                id=user_id, tenant_id=tenant_id, email=f"fk-{uuid.uuid4().hex}@example.com",
                full_name="FK Check", password_hash="x", status="active", is_admin=False,
            )
        )
        await session.flush()
        subject = Subject(tenant_id=tenant_id, name=f"FK subject {uuid.uuid4().hex[:6]}")
        session.add(subject)
        await session.flush()
        session.add(Document(tenant_id=tenant_id, subject_id=subject.id, title="FK doc", created_by=user_id))
        await session.flush()
    count = (
        await raw_db.execute(select(Document.id).where(Document.tenant_id == tenant_id, Document.title == "FK doc"))
    ).scalars().all()
    assert len(count) == 1
    # And the FK still rejects a tenant id that does not exist (the check is not skipped).
    ghost = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with tenant_session_context(str(ghost)) as session:
            await session.execute(
                text("INSERT INTO app.subjects (tenant_id, id, name) VALUES (:t, gen_random_uuid(), 'x')"),
                {"t": ghost},
            )


# ── app_platform ─────────────────────────────────────────────────────────────


async def test_app_platform_lists_all_tenants(tenant_a: Tenant, tenant_b: Tenant) -> None:
    ids = {r[0] for r in await _as_platform("SELECT id FROM app.tenants")}
    assert {tenant_a.id, tenant_b.id} <= ids


async def test_app_platform_can_update_its_columns(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    tenant_id = tenant_a.id
    res = await _as_platform(
        "UPDATE app.tenants SET name = 'Renamed', status = 'suspended', registration_mode = 'open', "
        "doc_limits = '{\"max_documents\": 7}'::jsonb, db_dsn = 'postgresql://example/other' WHERE id = :id",
        {"id": tenant_id},
    )
    assert res == [1]
    row = (
        await raw_db.execute(
            text("SELECT name, status, registration_mode, doc_limits, db_dsn FROM app.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
    ).one()
    assert tuple(row) == ("Renamed", "suspended", "open", {"max_documents": 7}, "postgresql://example/other")


@pytest.mark.parametrize("assignment", ["id = gen_random_uuid()", "slug = 'new-slug-value'"])
async def test_app_platform_cannot_update_id_or_slug(tenant_a: Tenant, assignment: str) -> None:
    await _assert_platform_denied(f"UPDATE app.tenants SET {assignment} WHERE id = :id", {"id": tenant_a.id})


async def test_app_platform_cannot_insert_an_explicit_id_but_can_insert_without_one(raw_db: AsyncSession) -> None:
    await _assert_platform_denied(
        "INSERT INTO app.tenants (id, slug, name, kind) VALUES (gen_random_uuid(), :slug, 'X', 'school')",
        {"slug": f"explicit-{uuid.uuid4().hex[:8]}"},
    )
    slug = f"plat-{uuid.uuid4().hex[:8]}"
    rows = await _as_platform(
        "INSERT INTO app.tenants (slug, name, kind) VALUES (:slug, 'Platform made', 'school') RETURNING id",
        {"slug": slug},
    )
    new_id = rows[0][0]
    try:
        assert (await raw_db.execute(select(Tenant.slug).where(Tenant.id == new_id))).scalar_one() == slug
    finally:
        await raw_db.execute(delete(Tenant).where(Tenant.id == new_id))
        await raw_db.commit()


async def test_app_platform_cannot_delete_tenants(tenant_a: Tenant) -> None:
    await _assert_platform_denied("DELETE FROM app.tenants WHERE id = :id", {"id": tenant_a.id})


# ── Pre-auth flows and the per-request tenant check ──────────────────────────


async def test_login_and_refresh_work_for_active_tenants_and_are_refused_when_suspended(
    client: AsyncClient, tenant_a: Tenant, user_a: User, raw_db: AsyncSession
) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert login.status_code == 200
    client.cookies.clear()
    client.cookies.update(login.cookies)
    headers = {"X-Tenant-Slug": tenant_a.slug, "X-Requested-With": "XMLHttpRequest"}
    ok = await client.post("/api/v1/auth/refresh", headers=headers)
    assert ok.status_code == 200
    cookies_after_rotation = ok.cookies

    await raw_db.execute(text("UPDATE app.tenants SET status = 'suspended' WHERE id = :id"), {"id": tenant_a.id})
    await raw_db.commit()

    client.cookies.clear()
    client.cookies.update(cookies_after_rotation)
    refused = await client.post("/api/v1/auth/refresh", headers=headers)
    assert refused.status_code == 401
    refused_login = await client.post(
        "/api/v1/auth/login",
        json={"email": user_a.email, "password": "Password123!", "tenant_slug": tenant_a.slug},
    )
    assert refused_login.status_code == 404
    assert refused_login.json()["detail"]["code"] == "tenant_not_found"


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_user_status"),
    [("open", 201, "active"), ("approval", 201, "pending"), ("invite_only", 403, None)],
)
async def test_register_respects_the_registration_mode(
    client: AsyncClient, raw_db: AsyncSession, mode: str, expected_status: int, expected_user_status: str | None
) -> None:
    tenant = Tenant(slug=f"reg-{uuid.uuid4().hex[:10]}", name="Reg Org", kind="school", registration_mode=mode)
    raw_db.add(tenant)
    await raw_db.commit()
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"new-{uuid.uuid4().hex[:6]}@example.com", "full_name": "New Person",
            "password": "Password123!", "tenant_slug": tenant.slug,
        },
    )
    assert resp.status_code == expected_status, resp.text
    if expected_user_status:
        assert resp.json()["status"] == expected_user_status
    else:
        assert resp.json()["detail"]["code"] == "registration_closed"


async def test_forgot_password_works_for_active_and_is_refused_for_suspended_tenants(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
    raw_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_send(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("app.services.auth.send_password_reset_email", fake_send)
    body = {"email": user_a.email, "tenant_slug": tenant_a.slug}
    ok = await client.post("/api/v1/auth/forgot-password", json=body)
    assert ok.status_code == 200
    assert len(sent) == 1 and sent[0]["to_email"] == user_a.email

    await raw_db.execute(text("UPDATE app.tenants SET status = 'suspended' WHERE id = :id"), {"id": tenant_a.id})
    await raw_db.commit()
    refused = await client.post("/api/v1/auth/forgot-password", json=body)
    assert refused.status_code == 404
    assert len(sent) == 1  # no second mail


async def test_public_branding_active_tenant_is_branded_and_suspended_gets_the_default(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    active = await client.get("/api/v1/public/branding", headers={"Host": f"{tenant_a.slug}.localhost"})
    assert active.status_code == 200
    assert active.json()["display_name"] == "Tenant A"

    suspended = Tenant(slug=f"susp-{uuid.uuid4().hex[:10]}", name="Hidden Name", kind="school", status="suspended")
    raw_db.add(suspended)
    await raw_db.commit()
    resp = await client.get("/api/v1/public/branding", headers={"Host": f"{suspended.slug}.localhost"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "RAG Platform"


async def test_authenticated_request_for_a_suspended_tenant_is_403(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    assert (await client.get("/api/v1/admin/users", headers=_hdr(token, tenant_a.slug))).status_code == 200
    await raw_db.execute(text("UPDATE app.tenants SET status = 'suspended' WHERE id = :id"), {"id": tenant_a.id})
    await raw_db.commit()
    resp = await client.get("/api/v1/admin/users", headers=_hdr(token, tenant_a.slug))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "tenant_inactive"


async def test_host_mismatch_is_403(
    client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant, admin_a: User
) -> None:
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    # The slug comes from the Host header (X-Tenant-Slug is only read for localhost-style hosts).
    headers = {"Authorization": f"Bearer {token}", "Host": f"{tenant_b.slug}.localhost"}
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "tenant_mismatch"
    ok = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}", "Host": f"{tenant_a.slug}.localhost"},
    )
    assert ok.status_code == 200


async def test_valid_jwt_for_a_tenant_that_does_not_exist_is_refused_not_500(client: AsyncClient) -> None:
    ghost = str(uuid.uuid4())
    token = create_access_token(tenant_id=ghost, user_id=str(uuid.uuid4()), is_admin=True)
    for path in ("/api/v1/admin/users", "/api/v1/admin/settings/timezone"):
        resp = await client.get(path, headers=_hdr(token))
        assert resp.status_code in (401, 403), f"{path}: {resp.status_code} {resp.text}"


async def test_registration_mode_change_is_audited_and_hidden_from_other_tenants(
    client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    resp = await client.patch(
        "/api/v1/admin/settings/registration-mode",
        headers=_hdr(token, tenant_a.slug),
        json={"registration_mode": "approval"},
    )
    assert resp.status_code == 200, resp.text
    rows = (
        await raw_db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a.id, AuditLog.action == "tenant:update_registration_mode"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_id == admin_a.id
    assert rows[0].target_id == str(tenant_a.id)
    assert rows[0].details == {"previous_mode": "invite_only", "new_mode": "approval"}
    other = (
        await raw_db.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_b.id, AuditLog.action == "tenant:update_registration_mode"
            )
        )
    ).scalars().all()
    assert other == []


# ── Migration 0010 itself ────────────────────────────────────────────────────


async def test_0010_refuses_to_run_when_the_role_cannot_bypass_rls(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_phase5_hardening import _FakeConnection, _load_migration, _patched

    module = _load_migration("0010_tenants_rls.py", "migration_0010_rls_check")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=False))
    for step in (module.upgrade, module.downgrade):
        with pytest.raises(RuntimeError) as exc_info:
            step()
        assert "BYPASSRLS" in str(exc_info.value) and "superuser" in str(exc_info.value)
    assert executed == []  # it checks first: nothing was issued before the refusal


async def test_0010_statements_never_grant_what_the_design_forbids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static check of the SQL the migration issues (the database result is checked above)."""
    from tests.test_phase5_hardening import _FakeConnection, _load_migration, _patched

    module = _load_migration("0010_tenants_rls.py", "migration_0010_statements")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=True))
    module.upgrade()
    sql = "\n".join(" ".join(s.split()) for s in executed)
    assert sql.count("CREATE POLICY") == 4
    assert "FOR INSERT" not in sql and "FOR DELETE" not in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql and "FORCE ROW LEVEL SECURITY" in sql
    assert "REVOKE SELECT ON app.tenants FROM app_tenant" in sql
    assert "GRANT SELECT ON app.tenants" not in sql
    assert "GRANT SELECT (id, slug, status, timezone) ON app.tenants TO app_tenant" in sql  # no doc_limits (5b)
    assert "REVOKE ALL ON app.tenants FROM anon" in sql and "REVOKE ALL ON app.tenants FROM authenticated" in sql
