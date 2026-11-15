"""
tests/test_document_permissions.py
──────────────────────────────────
document:view and document:publish (design A4): catalog, system roles, new-tenant seeding,
the migration backfill and the custom "view only" role.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import ALL_SYSTEM_PERMISSIONS, load_user_permissions
from app.db.models.rbac import Permission, Role, RolePermission
from app.db.models.tenant import Tenant
from app.services.rbac import SYSTEM_ROLE_DEFINITIONS, seed_tenant_system_roles
from tests.fixtures.academic_world import AcademicWorld

pytestmark = pytest.mark.asyncio

NEW_PERMS = {"document:view", "document:publish"}


async def _perms_by_role(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, set[str]]:
    rows = (
        await db.execute(
            select(Role.name, RolePermission.permission_id)
            .join(
                RolePermission,
                (RolePermission.tenant_id == Role.tenant_id) & (RolePermission.role_id == Role.id),
            )
            .where(Role.tenant_id == tenant_id)
        )
    ).all()
    out: dict[str, set[str]] = {}
    for name, perm in rows:
        out.setdefault(name, set()).add(perm)
    return out


def _assert_system_role_grants(perms_by_role: dict[str, set[str]]) -> None:
    assert perms_by_role["tenant_admin"] >= NEW_PERMS
    assert perms_by_role["teacher"] >= NEW_PERMS
    # Students keep chat:use but get none of the document permissions.
    assert "chat:use" in perms_by_role["student"]
    assert not {p for p in perms_by_role["student"] if p.startswith("document:")}


async def test_new_permissions_are_in_the_catalog_and_system_role_definitions() -> None:
    assert NEW_PERMS <= ALL_SYSTEM_PERMISSIONS
    assert set(SYSTEM_ROLE_DEFINITIONS["tenant_admin"]["permissions"]) >= NEW_PERMS
    assert set(SYSTEM_ROLE_DEFINITIONS["teacher"]["permissions"]) >= NEW_PERMS
    assert not {
        p for p in SYSTEM_ROLE_DEFINITIONS["student"]["permissions"] if p.startswith("document:")
    }
    # tenant_admin holds every system permission.
    assert set(SYSTEM_ROLE_DEFINITIONS["tenant_admin"]["permissions"]) == ALL_SYSTEM_PERMISSIONS


async def test_permission_catalog_table_has_the_new_permissions(raw_db: AsyncSession) -> None:
    ids = set((await raw_db.execute(select(Permission.id))).scalars().all())
    assert ids >= NEW_PERMS
    # Every code-level system permission exists in the database catalog.
    assert ids >= ALL_SYSTEM_PERMISSIONS


async def test_new_tenant_seeds_document_permissions(raw_db: AsyncSession) -> None:
    t = Tenant(slug=f"doc-perms-{uuid.uuid4().hex}", name="Doc Perms Org", kind="school")
    raw_db.add(t)
    await raw_db.commit()
    await seed_tenant_system_roles(t.id, raw_db)
    await raw_db.commit()
    _assert_system_role_grants(await _perms_by_role(raw_db, t.id))


async def test_platform_created_tenant_receives_document_permissions(
    client: AsyncClient, platform_token: str, raw_db: AsyncSession
) -> None:
    headers = {"Authorization": f"Bearer {platform_token}"}
    created = await client.post(
        "/api/v1/platform/tenants",
        headers=headers,
        json={"slug": f"plat-doc-{uuid.uuid4().hex}", "name": "Platform Doc Org", "kind": "school"},
    )
    assert created.status_code == 201
    tenant_id = uuid.UUID(created.json()["id"])
    admin = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/admins",
        headers=headers,
        json={"email": f"admin-{uuid.uuid4().hex}@plat.local", "full_name": "Platform Admin"},
    )
    assert admin.status_code == 201
    _assert_system_role_grants(await _perms_by_role(raw_db, tenant_id))


async def test_migration_backfill_restores_document_permissions_for_existing_tenants(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    """Run the migration's own backfill statement on a tenant whose roles lack the permissions."""
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0008_phase5_documents.py"
    spec = importlib.util.spec_from_file_location("migration_0008_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    w = academic_world.a
    other = academic_world.b
    # Simulate a tenant that existed before the migration: its system roles lack the new
    # permissions (the custom viewer role keeps its own grant).
    system_role_ids = select(Role.id).where(Role.tenant_id == w.id, Role.is_system.is_(True))
    await raw_db.execute(
        delete(RolePermission).where(
            RolePermission.tenant_id == w.id,
            RolePermission.permission_id.in_(NEW_PERMS),
            RolePermission.role_id.in_(system_role_ids),
        )
    )
    before = await _perms_by_role(raw_db, w.id)
    assert not (NEW_PERMS & before["teacher"])
    assert not (NEW_PERMS & before["tenant_admin"])

    await raw_db.execute(text(module.BACKFILL_ROLE_PERMISSIONS_SQL))
    after = await _perms_by_role(raw_db, w.id)
    _assert_system_role_grants(after)
    # Custom roles are never touched by the backfill: the viewer role still has only document:view.
    assert after["doc-viewer"] == {"document:view"}
    # Running it again changes nothing (idempotent), and other tenants stay correct.
    await raw_db.execute(text(module.BACKFILL_ROLE_PERMISSIONS_SQL))
    assert await _perms_by_role(raw_db, w.id) == after
    _assert_system_role_grants(await _perms_by_role(raw_db, other.id))


async def test_academic_world_roles_get_the_expected_document_permissions(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    for w in (academic_world.a, academic_world.b):
        admin = await load_user_permissions(w.admin, raw_db)
        t1 = await load_user_permissions(w.t1, raw_db)
        s1 = await load_user_permissions(w.s1, raw_db)
        viewer = await load_user_permissions(w.viewer, raw_db)
        assert admin >= NEW_PERMS
        assert t1 >= NEW_PERMS and {"document:upload", "document:delete"} <= t1
        assert not {p for p in s1 if p.startswith("document:")}
        assert viewer == {"document:view"}
