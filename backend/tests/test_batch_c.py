"""
Batch C regression tests: email visibility (2), Content-Disposition safety (3),
settings:manage permission and transactional audit (4).
"""

import io
import uuid
from typing import Any

import openpyxl
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_timetable_import import _setup_import_test_data

from app.core.security import create_access_token
from app.db.models.academic import Class
from app.db.models.audit import AuditLog
from app.db.models.rbac import Permission, Role, RolePermission, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.rbac import seed_tenant_system_roles


def _h(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


def _admin_token(tenant: Tenant, admin: User) -> str:
    return create_access_token(tenant_id=str(tenant.id), user_id=str(admin.id), is_admin=True)


# ─── Item 2: emails only for admins ──────────────────────────────────────────

async def test_teacher_picker_hides_emails_for_non_admin(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    c, s1, _s2, teacher, teacher_token = await _setup_import_test_data(tenant_a, raw_db)
    for path in (f"/api/v1/classes/{c.id}/assigned-teachers", f"/api/v1/classes/{c.id}/subjects/{s1.id}/assigned-teachers"):
        res = await client.get(path, headers=_h(teacher_token, tenant_a.slug))
        assert res.status_code == 200, res.text
        item = res.json()[0]
        assert item["full_name"] == teacher.full_name
        assert item["email"] is None
        assert teacher.email not in res.text

        res_admin = await client.get(path, headers=_h(_admin_token(tenant_a, admin_a), tenant_a.slug))
        assert res_admin.json()[0]["email"] == teacher.email


async def test_templates_hide_emails_for_non_admin(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    c, _s1, _s2, teacher, teacher_token = await _setup_import_test_data(tenant_a, raw_db)
    base = f"/api/v1/classes/{c.id}/timetables"
    res = await client.get(f"{base}/template.csv", headers=_h(teacher_token, tenant_a.slug))
    assert res.status_code == 200
    assert teacher.email not in res.text
    assert teacher.full_name in res.text

    res_x = await client.get(f"{base}/template.xlsx", headers=_h(teacher_token, tenant_a.slug))
    wb = openpyxl.load_workbook(io.BytesIO(res_x.content))
    cells = [str(cell.value) for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value]
    assert not any(teacher.email in v for v in cells)
    assert any(teacher.full_name in v for v in cells)

    admin_hdr = _h(_admin_token(tenant_a, admin_a), tenant_a.slug)
    assert teacher.email in (await client.get(f"{base}/template.csv", headers=admin_hdr)).text


async def test_import_preview_matching_works_but_hides_emails_for_non_admin(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession
) -> None:
    c, s1, _s2, teacher, teacher_token = await _setup_import_test_data(tenant_a, raw_db)
    url = f"/api/v1/classes/{c.id}/timetables/import-preview"
    csv_data = (
        "weekday,start_time,end_time,subject_code,teacher_email,room,note\n"
        f"Monday,09:00,10:00,{s1.code},{teacher.email},R1,a\n"
        f"Tuesday,09:00,10:00,{s1.code},{teacher.full_name},R2,b\n"
    ).encode()
    files = {"file": ("t.csv", csv_data, "text/csv")}

    res = await client.post(url, headers=_h(teacher_token, tenant_a.slug), files=files)
    assert res.status_code == 200, res.text
    data = res.json()
    assert [r["teacher_id"] for r in data["rows"]] == [str(teacher.id)] * 2  # both matched
    assert all(r["teacher_email"] is None for r in data["rows"])
    assert all(r["teacher_name"] == teacher.full_name for r in data["rows"])
    assert all(t["email"] is None for t in data["available_teachers"])

    res_admin = await client.post(url, headers=_h(_admin_token(tenant_a, admin_a), tenant_a.slug), files=files)
    assert res_admin.json()["rows"][0]["teacher_email"] == teacher.email
    assert res_admin.json()["available_teachers"][0]["email"] == teacher.email


# ─── Item 3: Content-Disposition ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "name",
    ['Grade "10"', "Grade 10\r\nX-Evil: 1", "数学 Ünï クラス", 'a";filename="evil.exe', "../../etc/passwd"],
)
async def test_template_content_disposition_is_safe(
    client: AsyncClient, tenant_a: Tenant, admin_a: User, raw_db: AsyncSession, name: str
) -> None:
    c = Class(tenant_id=tenant_a.id, name=name)
    raw_db.add(c)
    await raw_db.commit()
    for ext in ("csv", "xlsx"):
        res = await client.get(
            f"/api/v1/classes/{c.id}/timetables/template.{ext}",
            headers=_h(_admin_token(tenant_a, admin_a), tenant_a.slug),
        )
        assert res.status_code == 200
        assert "x-evil" not in res.headers
        cd = res.headers["content-disposition"]
        assert "\r" not in cd and "\n" not in cd
        assert cd.encode("ascii")
        assert "; filename*=UTF-8''" in cd  # RFC 5987 form is intact
        assert cd.count('"') == 2  # exactly one quoted filename, no quote injection
        assert ".." not in cd and "/" not in cd.split("filename=")[1]


# ─── Item 4: settings:manage and transactional audit ─────────────────────────

async def _user_with_permission(tenant: Tenant, raw_db: AsyncSession, perm: str) -> str:
    role = Role(tenant_id=tenant.id, name=f"r-{uuid.uuid4().hex[:8]}", description="x", is_system=False)
    raw_db.add(role)
    await raw_db.flush()
    raw_db.add(RolePermission(tenant_id=tenant.id, role_id=role.id, permission_id=perm))
    user = User(
        tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:8]}@t.edu", full_name="Cfg", is_admin=False, status="active"
    )
    raw_db.add(user)
    await raw_db.flush()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
    await raw_db.commit()
    return create_access_token(tenant_id=str(tenant.id), user_id=str(user.id), is_admin=False)


async def test_settings_manage_in_catalog_and_seeded(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    assert (await raw_db.execute(select(Permission).where(Permission.id == "settings:manage"))).scalar_one_or_none()

    new_tenant = Tenant(slug=f"new-{uuid.uuid4().hex}", name="New", kind="school")
    raw_db.add(new_tenant)
    await raw_db.commit()
    await seed_tenant_system_roles(new_tenant.id, raw_db)
    await raw_db.commit()
    for t in (tenant_a, new_tenant):
        rows = await raw_db.execute(
            select(Role.name)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .where(Role.tenant_id == t.id, RolePermission.permission_id == "settings:manage")
        )
        assert set(rows.scalars().all()) == {"tenant_admin"}


async def test_timezone_endpoint_uses_settings_manage_not_branding(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    branding_only = await _user_with_permission(tenant_a, raw_db, "branding:manage")
    r = await client.patch(
        "/api/v1/admin/settings/timezone", headers=_h(branding_only, tenant_a.slug), json={"timezone": "UTC"}
    )
    assert r.status_code == 403
    assert (await client.get("/api/v1/admin/settings/timezone", headers=_h(branding_only, tenant_a.slug))).status_code == 403

    settings_user = await _user_with_permission(tenant_a, raw_db, "settings:manage")
    r2 = await client.patch(
        "/api/v1/admin/settings/timezone", headers=_h(settings_user, tenant_a.slug), json={"timezone": "UTC"}
    )
    assert r2.status_code == 200, r2.text
    assert (await client.get("/api/v1/admin/settings/timezone", headers=_h(settings_user, tenant_a.slug))).json() == {
        "timezone": "UTC"
    }


async def test_failed_audit_write_rolls_back_timezone_change(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = tenant_a.id  # read before expire_all() below
    before = (await raw_db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))).scalar_one()
    target = "Pacific/Auckland" if before != "Pacific/Auckland" else "UTC"

    async def boom(**_k: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr("app.api.v1.admin.record_audit_event", boom)
    with pytest.raises(RuntimeError):
        await client.patch(
            "/api/v1/admin/settings/timezone",
            headers=_h(_admin_token(tenant_a, admin_a), tenant_a.slug),
            json={"timezone": target},
        )
    raw_db.expire_all()
    after = (await raw_db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))).scalar_one()
    assert after == before
    n = (
        await raw_db.execute(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "tenant:update_timezone")
        )
    ).scalars().all()
    assert n == []
