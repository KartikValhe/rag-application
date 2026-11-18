"""
tests/test_document_limits.py
─────────────────────────────
Per-tenant document limits (design A2, step 5b-1):

  1. Migration 0011: app_tenant may read tenants.doc_limits, but only its own row (tenant_self_select
     policy); db_dsn, name and registration_mode stay hidden; the downgrade restores the exact 0010
     grants (run for real, in the TEST database, inside a transaction that is rolled back).
  2. app/services/document_limits.py: env defaults merged with the tenant's overrides, validated
     against hard platform ceilings (unknown keys and values above a ceiling are refused).
  3. PATCH /api/v1/platform/tenants/{tenant_id}/limits: superadmin only, validated, audited in the
     tenant's audit log, generic 404 for an unknown tenant.
"""

import importlib.util
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.tenant import Tenant
from app.db.models.user import User
from tests.test_isolation import _tenants_column_privilege_inventory
from tests.test_tenants_rls import _as_tenant, _assert_tenant_denied

# No module-level asyncio mark: asyncio_mode=auto already runs the async tests, and the sync tests here must not carry it.

MIGRATIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_migration(filename: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / filename)
    assert spec is not None and spec.loader is not None, f"{filename} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _in_rolled_back_test_transaction(fn: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    """
    Run `fn` on a superuser connection to the TEST database inside one transaction that is always
    rolled back (PostgreSQL DDL and GRANT/REVOKE are transactional), so nothing persists.
    """
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
                assert database.endswith("_test"), f"refusing to run a migration experiment on {database!r}"
                return await fn(conn)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


def _run_migration_step(sync_conn: Connection, step: Callable[[], None]) -> None:
    """Run a migration's upgrade() or downgrade() with alembic's `op` bound to this connection."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        step()


async def _column_privileges(conn: AsyncConnection) -> set[tuple[str, str, str]]:
    rows = (
        await conn.execute(
            text("""
                SELECT grantee, privilege_type, column_name
                FROM information_schema.column_privileges
                WHERE table_schema = 'app' AND table_name = 'tenants'
                  AND grantee <> (SELECT pg_get_userbyid(relowner) FROM pg_class
                                  WHERE oid = 'app.tenants'::regclass)
            """)
        )
    ).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def _table_privileges(conn: AsyncConnection) -> set[tuple[str, str]]:
    rows = (
        await conn.execute(
            text("""
                SELECT pg_get_userbyid(a.grantee), a.privilege_type
                FROM pg_class c, LATERAL aclexplode(c.relacl) a
                WHERE c.oid = 'app.tenants'::regclass AND a.grantee <> c.relowner
            """)
        )
    ).all()
    return {(r[0], r[1]) for r in rows}


# ── 1. Migration 0011 ────────────────────────────────────────────────────────


async def test_app_tenant_reads_its_own_doc_limits(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": '{"max_documents": 7}', "id": tenant_a.id},
    )
    await raw_db.commit()
    rows = await _as_tenant(tenant_a.id, "SELECT id, doc_limits FROM app.tenants")
    assert rows == [(tenant_a.id, {"max_documents": 7})]


async def test_app_tenant_cannot_read_another_tenants_doc_limits(
    tenant_a: Tenant, tenant_b: Tenant, raw_db: AsyncSession
) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": '{"max_documents": 3}', "id": tenant_b.id},
    )
    await raw_db.commit()
    # Asking for B by id from A's context returns no row (not an error, not B's values).
    assert await _as_tenant(tenant_a.id, "SELECT doc_limits FROM app.tenants WHERE id = :b", {"b": tenant_b.id}) == []
    # A blanket read sees only A's own row.
    rows = await _as_tenant(tenant_a.id, "SELECT id, doc_limits FROM app.tenants")
    assert [r[0] for r in rows] == [tenant_a.id]
    # Without a tenant context there is no row at all.
    assert await _as_tenant(uuid.uuid4(), "SELECT doc_limits FROM app.tenants") == []


@pytest.mark.parametrize("column", ["db_dsn", "name", "registration_mode"])
async def test_app_tenant_still_cannot_read_db_dsn_name_or_registration_mode(tenant_a: Tenant, column: str) -> None:
    await _assert_tenant_denied(tenant_a.id, f"SELECT {column} FROM app.tenants")
    await _assert_tenant_denied(tenant_a.id, f"SELECT doc_limits, {column} FROM app.tenants")


async def test_app_tenant_still_cannot_write_doc_limits(tenant_a: Tenant) -> None:
    await _assert_tenant_denied(
        tenant_a.id, "UPDATE app.tenants SET doc_limits = '{}'::jsonb WHERE id = :id", {"id": tenant_a.id}
    )


async def test_0011_downgrade_restores_the_exact_0010_grants_and_upgrade_reapplies(raw_db: AsyncSession) -> None:
    """Runs the real 0011 downgrade and upgrade in the TEST database, then rolls everything back."""
    module = _load_migration("0011_document_limits_grant.py", "migration_0011_real")
    assert module.down_revision == "0010"

    async def check(conn: AsyncConnection) -> None:
        assert await _column_privileges(conn) == _tenants_column_privilege_inventory()
        await conn.run_sync(_run_migration_step, module.downgrade)
        after_down = await _column_privileges(conn)
        expected_0010 = _tenants_column_privilege_inventory(after_0011=False)
        assert after_down == expected_0010, (
            f"unexpected: {sorted(after_down - expected_0010)}; missing: {sorted(expected_0010 - after_down)}"
        )
        assert await _table_privileges(conn) == {("app_platform", "SELECT")}
        await conn.run_sync(_run_migration_step, module.upgrade)
        assert await _column_privileges(conn) == _tenants_column_privilege_inventory()
        assert await _table_privileges(conn) == {("app_platform", "SELECT")}

    await _in_rolled_back_test_transaction(check)
    # The rollback left the head grants in place (checked through a separate connection).
    rows = (
        await raw_db.execute(
            text("SELECT has_column_privilege('app_tenant', 'app.tenants', 'doc_limits', 'SELECT')")
        )
    ).scalar_one()
    assert rows is True


async def test_0011_grants_only_the_doc_limits_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static check of the SQL it issues: one column grant, never a table-wide one."""
    from tests.test_phase5_hardening import _FakeConnection, _patched

    module = _load_migration("0011_document_limits_grant.py", "migration_0011_static")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=True))
    module.upgrade()
    sql = "\n".join(" ".join(s.split()) for s in executed)
    assert "GRANT SELECT (doc_limits) ON app.tenants TO app_tenant" in sql
    assert "GRANT SELECT ON" not in sql and "UPDATE" not in sql
    executed.clear()
    module.downgrade()
    sql = "\n".join(" ".join(s.split()) for s in executed)
    assert "REVOKE SELECT (doc_limits) ON app.tenants FROM app_tenant" in sql


# ── 2. The limits service ────────────────────────────────────────────────────

from app.core.config import settings  # noqa: E402
from app.core.tenant_db import tenant_session_context  # noqa: E402
from app.services import document_limits as dl  # noqa: E402


def test_limit_keys_and_ceilings_cover_every_field() -> None:
    assert set(dl.LIMIT_KEYS) == set(dl.PLATFORM_CEILINGS) == {
        "max_file_bytes", "max_pdf_pages", "max_pptx_slides", "max_documents",
        "max_storage_bytes", "max_pages", "uploads_per_user_per_hour", "uploads_per_tenant_per_hour",
    }
    # The file ceiling is the platform upload ceiling the middleware and the stream use.
    from app.core.upload_security import DOC_UPLOAD_CEILING_BYTES

    assert dl.PLATFORM_CEILINGS["max_file_bytes"] == DOC_UPLOAD_CEILING_BYTES


def test_defaults_come_from_the_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (
        ("DOC_MAX_FILE_BYTES", 5_000_000), ("DOC_MAX_PDF_PAGES", 150), ("DOC_MAX_PPTX_SLIDES", 60),
        ("QUOTA_MAX_DOCUMENTS", 40), ("QUOTA_MAX_STORAGE_BYTES", 90_000_000), ("QUOTA_MAX_PAGES", 900),
        ("DOC_UPLOAD_RATE_USER", 9), ("DOC_UPLOAD_RATE_TENANT", 33),
    ):
        monkeypatch.setattr(settings, name, value)
    assert dl.default_limits().as_dict() == {
        "max_file_bytes": 5_000_000, "max_pdf_pages": 150, "max_pptx_slides": 60, "max_documents": 40,
        "max_storage_bytes": 90_000_000, "max_pages": 900, "uploads_per_user_per_hour": 9,
        "uploads_per_tenant_per_hour": 33,
    }


def test_pilot_defaults_match_the_design() -> None:
    assert (settings.DOC_MAX_FILE_BYTES, settings.DOC_MAX_PDF_PAGES, settings.DOC_MAX_PPTX_SLIDES) == (
        10 * 1024 * 1024, 200, 100,
    )
    assert (settings.QUOTA_MAX_DOCUMENTS, settings.QUOTA_MAX_STORAGE_BYTES, settings.QUOTA_MAX_PAGES) == (
        200, 500 * 1024 * 1024, 5000,
    )
    assert (settings.DOC_UPLOAD_RATE_USER, settings.DOC_UPLOAD_RATE_TENANT) == (20, 100)


def test_an_env_default_above_its_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOC_MAX_FILE_BYTES", dl.PLATFORM_CEILINGS["max_file_bytes"] + 1)
    with pytest.raises(dl.DocumentLimitsError) as exc_info:
        dl.default_limits()
    assert exc_info.value.code == "limit_above_ceiling"


def test_overrides_are_merged_over_the_defaults() -> None:
    base = dl.default_limits()
    merged = dl.merge_limits({"max_documents": 3, "max_pdf_pages": 500})
    assert merged.max_documents == 3 and merged.max_pdf_pages == 500
    assert merged.max_file_bytes == base.max_file_bytes
    assert dl.merge_limits({}) == base and dl.merge_limits(None) == base


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"max_docs": 3}, "unknown_limit"),
        ({"MAX_DOCUMENTS": 3}, "unknown_limit"),
        ({"max_file_bytes": dl.PLATFORM_CEILINGS["max_file_bytes"] + 1}, "limit_above_ceiling"),
        ({"max_pages": dl.PLATFORM_CEILINGS["max_pages"] + 1}, "limit_above_ceiling"),
        ({"max_documents": 0}, "invalid_limit_value"),
        ({"max_documents": -5}, "invalid_limit_value"),
        ({"max_documents": True}, "invalid_limit_value"),
        ({"max_documents": 2.5}, "invalid_limit_value"),
        ({"max_documents": "10"}, "invalid_limit_value"),
        ({"max_documents": None}, "invalid_limit_value"),
    ],
)
def test_invalid_overrides_are_refused(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(dl.DocumentLimitsError) as exc_info:
        dl.merge_limits(overrides)
    assert exc_info.value.code == code


def test_a_value_exactly_at_the_ceiling_is_accepted() -> None:
    ceiling = dl.PLATFORM_CEILINGS["max_storage_bytes"]
    assert dl.merge_limits({"max_storage_bytes": ceiling}).max_storage_bytes == ceiling


def test_apply_patch_sets_and_clears_overrides() -> None:
    current = {"max_documents": 5, "max_pages": 100}
    assert dl.apply_patch(current, {"max_documents": 9, "max_pages": None, "max_pdf_pages": 300}) == {
        "max_documents": 9, "max_pdf_pages": 300,
    }
    assert current == {"max_documents": 5, "max_pages": 100}  # not mutated
    with pytest.raises(dl.DocumentLimitsError):
        dl.apply_patch(current, {"nope": 1})


async def test_load_tenant_limits_reads_the_tenants_own_overrides_through_rls(
    tenant_a: Tenant, tenant_b: Tenant, raw_db: AsyncSession
) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": '{"max_documents": 4}', "id": tenant_a.id},
    )
    await raw_db.commit()
    async with tenant_session_context(str(tenant_a.id)) as db:
        assert (await dl.load_tenant_limits(db, tenant_a.id)).max_documents == 4
    async with tenant_session_context(str(tenant_b.id)) as db:
        assert (await dl.load_tenant_limits(db, tenant_b.id)) == dl.default_limits()
        # Another tenant's row is invisible, so asking for it fails closed instead of using defaults.
        with pytest.raises(dl.TenantLimitsNotVisible):
            await dl.load_tenant_limits(db, tenant_a.id)


async def test_corrupt_stored_overrides_fail_closed(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": '{"max_documents": 999999999}', "id": tenant_a.id},
    )
    await raw_db.commit()
    async with tenant_session_context(str(tenant_a.id)) as db:
        with pytest.raises(dl.DocumentLimitsError):
            await dl.load_tenant_limits(db, tenant_a.id)


# ── 3. PATCH /api/v1/platform/tenants/{tenant_id}/limits ─────────────────────


def _limits_url(tenant_id: uuid.UUID) -> str:
    return f"/api/v1/platform/tenants/{tenant_id}/limits"


async def _stored_limits(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    return (await raw_db.execute(select(Tenant.doc_limits).where(Tenant.id == tenant_id))).scalar_one()


async def _limit_audits(raw_db: AsyncSession, tenant_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await raw_db.execute(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "tenant:update_doc_limits")
            )
        ).scalars()
    )


async def test_superadmin_sets_limits_and_the_change_is_audited_in_that_tenant(
    client: AsyncClient, platform_token: str, platform_admin: Any, tenant_a: Tenant, tenant_b: Tenant,
    raw_db: AsyncSession,
) -> None:
    headers = {"Authorization": f"Bearer {platform_token}"}
    resp = await client.patch(_limits_url(tenant_a.id), headers=headers, json={"max_documents": 7, "max_pdf_pages": 400})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"tenant_id", "overrides", "effective", "ceilings"}
    assert body["tenant_id"] == str(tenant_a.id)
    assert body["overrides"] == {"max_documents": 7, "max_pdf_pages": 400}
    assert body["effective"] == dl.merge_limits({"max_documents": 7, "max_pdf_pages": 400}).as_dict()
    assert body["ceilings"] == dl.PLATFORM_CEILINGS
    assert await _stored_limits(raw_db, tenant_a.id) == {"max_documents": 7, "max_pdf_pages": 400}

    # A second PATCH merges; null removes an override.
    resp = await client.patch(_limits_url(tenant_a.id), headers=headers, json={"max_pdf_pages": None, "max_pages": 50})
    assert resp.status_code == 200, resp.text
    assert await _stored_limits(raw_db, tenant_a.id) == {"max_documents": 7, "max_pages": 50}

    audits = await _limit_audits(raw_db, tenant_a.id)
    assert len(audits) == 2
    first = min(audits, key=lambda a: a.created_at)
    assert first.target_type == "tenant" and first.target_id == str(tenant_a.id)
    assert first.actor_id is None  # a platform admin is not a user of the tenant
    assert first.details == {
        "previous": {}, "new": {"max_documents": 7, "max_pdf_pages": 400},
        "platform_admin_id": str(platform_admin.id),
    }
    assert await _limit_audits(raw_db, tenant_b.id) == []
    assert await _stored_limits(raw_db, tenant_b.id) == {}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"max_docs": 1}, "unknown_limit"),
        ({"max_file_bytes": 51 * 1024 * 1024}, "limit_above_ceiling"),
        ({"max_documents": 0}, "invalid_limit_value"),
        ({"max_documents": "5"}, "invalid_limit_value"),
        ({"max_documents": True}, "invalid_limit_value"),
    ],
)
async def test_invalid_limits_are_refused_with_a_stable_code_and_nothing_is_written(
    client: AsyncClient, platform_token: str, tenant_a: Tenant, raw_db: AsyncSession, payload: dict[str, Any], code: str
) -> None:
    resp = await client.patch(_limits_url(tenant_a.id), headers={"Authorization": f"Bearer {platform_token}"}, json=payload)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == code
    assert await _stored_limits(raw_db, tenant_a.id) == {}
    assert await _limit_audits(raw_db, tenant_a.id) == []


async def test_non_object_body_is_refused(client: AsyncClient, platform_token: str, tenant_a: Tenant) -> None:
    resp = await client.patch(_limits_url(tenant_a.id), headers={"Authorization": f"Bearer {platform_token}"}, json=[1])
    assert resp.status_code == 422


async def test_unknown_tenant_gets_the_generic_404(client: AsyncClient, platform_token: str) -> None:
    resp = await client.patch(
        _limits_url(uuid.uuid4()), headers={"Authorization": f"Bearer {platform_token}"}, json={"max_documents": 5}
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": {"code": "not_found", "message": "Resource not found."}}


async def test_only_a_platform_superadmin_may_change_limits(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    tenant_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    for headers in ({}, {"Authorization": f"Bearer {tenant_token}", "X-Tenant-Slug": tenant_a.slug}):
        resp = await client.patch(_limits_url(tenant_a.id), headers=headers, json={"max_documents": 1000})
        assert resp.status_code == 401, resp.text
    assert await _stored_limits(raw_db, tenant_a.id) == {}


def test_the_limits_route_is_a_platform_endpoint() -> None:
    from app.core.authz import get_route_declared_permission
    from app.main import app
    from tests.test_document_store import all_app_routes

    matches = [
        r for r in all_app_routes(app)
        if getattr(r, "path", "") == "/api/v1/platform/tenants/{tenant_id}/limits"
    ]
    assert len(matches) == 1
    assert get_route_declared_permission(matches[0]) == "platform:superadmin"
