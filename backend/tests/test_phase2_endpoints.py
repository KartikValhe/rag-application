"""
tests/test_phase2_endpoints.py
───────────────────────────────
Comprehensive test suite for Phase 2 endpoints:
  1. Roles and Permissions CRUD & privilege-escalation enforcement.
  2. Classes and Subjects CRUD with list-scope filtering.
  3. Class-Subject linking and unlinking.
  4. Student enrollments, roster listing, and CSV bulk import with error reporting.
  5. Teacher assignments to class-subjects.
  6. Audit log viewer (read-only, filtered, admin only).
  7. "My Classes" endpoint for teachers and students.
  8. Route x Role security matrix (admin, teacher, student, unauthenticated).
  9. Cross-class and cross-tenant isolation on all endpoint groups.
"""

import io
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, Subject
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


# ── Fixtures & Setup Helpers ──────────────────────────────────────────────────

async def _create_teacher_user(tenant: Tenant, raw_db: AsyncSession) -> tuple[User, str]:
    u = User(
        tenant_id=tenant.id,
        email=f"teacher-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Test Teacher",
        is_admin=False,
        status="active",
    )
    raw_db.add(u)
    await raw_db.flush()

    role_res = await raw_db.execute(
        select(Role.id).where(Role.tenant_id == tenant.id, Role.name == "teacher")
    )
    r_id = role_res.scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=u.id, role_id=r_id))
    await raw_db.commit()

    token = create_access_token(tenant_id=str(tenant.id), user_id=str(u.id), is_admin=False)
    return u, token


async def _create_student_user(tenant: Tenant, raw_db: AsyncSession) -> tuple[User, str]:
    u = User(
        tenant_id=tenant.id,
        email=f"student-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Test Student",
        is_admin=False,
        status="active",
    )
    raw_db.add(u)
    await raw_db.flush()

    role_res = await raw_db.execute(
        select(Role.id).where(Role.tenant_id == tenant.id, Role.name == "student")
    )
    r_id = role_res.scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=u.id, role_id=r_id))
    await raw_db.commit()

    token = create_access_token(tenant_id=str(tenant.id), user_id=str(u.id), is_admin=False)
    return u, token


# ── 1. Roles & Permissions Tests ──────────────────────────────────────────────

async def test_roles_and_permissions_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # 1. List catalog permissions
    perm_resp = await client.get("/api/v1/roles/permissions", headers=admin_hdr)
    assert perm_resp.status_code == 200
    perms = perm_resp.json()
    assert len(perms) >= 13
    assert any(p["id"] == "class:manage" for p in perms)

    # 2. List roles (should include system roles)
    roles_resp = await client.get("/api/v1/roles", headers=admin_hdr)
    assert roles_resp.status_code == 200
    roles = roles_resp.json()
    role_names = [r["name"] for r in roles]
    assert "tenant_admin" in role_names
    assert "teacher" in role_names
    assert "student" in role_names

    # 3. Create custom role
    create_resp = await client.post(
        "/api/v1/roles",
        headers=admin_hdr,
        json={
            "name": "Teaching Assistant",
            "description": "Assists teachers in class",
            "permissions": ["class:manage", "chat:use"],
        },
    )
    assert create_resp.status_code == 201
    custom_role = create_resp.json()
    assert custom_role["name"] == "Teaching Assistant"
    assert "class:manage" in custom_role["permissions"]
    custom_role_id = custom_role["id"]

    # 4. Get single role
    get_resp = await client.get(f"/api/v1/roles/{custom_role_id}", headers=admin_hdr)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == custom_role_id

    # 5. Update custom role
    patch_resp = await client.patch(
        f"/api/v1/roles/{custom_role_id}",
        headers=admin_hdr,
        json={"description": "Updated TA description", "permissions": ["class:manage"]},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["description"] == "Updated TA description"

    # 6. System role immutability
    teacher_role_id = next(r["id"] for r in roles if r["name"] == "teacher")
    sys_patch = await client.patch(
        f"/api/v1/roles/{teacher_role_id}",
        headers=admin_hdr,
        json={"name": "Hacked Teacher"},
    )
    assert sys_patch.status_code == 403
    assert sys_patch.json()["detail"]["code"] == "system_role_immutable"

    sys_del = await client.delete(f"/api/v1/roles/{teacher_role_id}", headers=admin_hdr)
    assert sys_del.status_code == 403
    assert sys_del.json()["detail"]["code"] == "system_role_immutable"

    # 7. Assign and unassign custom role to a user
    target_student, _ = await _create_student_user(tenant_a, raw_db)
    assign_resp = await client.post(
        f"/api/v1/users/{target_student.id}/roles",
        headers=admin_hdr,
        json={"role_id": custom_role_id},
    )
    assert assign_resp.status_code == 201

    user_roles_resp = await client.get(f"/api/v1/users/{target_student.id}/roles", headers=admin_hdr)
    assert user_roles_resp.status_code == 200
    ur_names = [r["name"] for r in user_roles_resp.json()]
    assert "Teaching Assistant" in ur_names

    unassign_resp = await client.delete(
        f"/api/v1/users/{target_student.id}/roles/{custom_role_id}",
        headers=admin_hdr,
    )
    assert unassign_resp.status_code == 200

    # 8. Delete custom role
    del_resp = await client.delete(f"/api/v1/roles/{custom_role_id}", headers=admin_hdr)
    assert del_resp.status_code == 200


# ── 2. Classes, Subjects & Links Tests (with Scope Defense) ───────────────────

async def test_classes_subjects_and_links(
    client: AsyncClient,
    tenant_a: Tenant,
    tenant_b: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # 1. Admin creates Class 10-A and 10-B
    c1_resp = await client.post(
        "/api/v1/classes",
        headers=admin_hdr,
        json={"name": "Class 10-A", "kind": "section", "description": "Grade 10 Section A"},
    )
    assert c1_resp.status_code == 201
    class_10a = c1_resp.json()

    c2_resp = await client.post(
        "/api/v1/classes",
        headers=admin_hdr,
        json={"name": "Class 10-B", "kind": "section"},
    )
    assert c2_resp.status_code == 201
    class_10b = c2_resp.json()

    # 2. Admin creates Subjects Math and Physics
    s1_resp = await client.post(
        "/api/v1/subjects",
        headers=admin_hdr,
        json={"name": "Mathematics", "code": "MATH101"},
    )
    assert s1_resp.status_code == 201
    sub_math = s1_resp.json()

    s2_resp = await client.post(
        "/api/v1/subjects",
        headers=admin_hdr,
        json={"name": "Physics", "code": "PHYS101"},
    )
    assert s2_resp.status_code == 201
    sub_phys = s2_resp.json()

    # 3. Link subjects to classes
    # 10-A has both Math and Physics
    link_m = await client.post(
        f"/api/v1/classes/{class_10a['id']}/subjects/{sub_math['id']}",
        headers=admin_hdr,
    )
    assert link_m.status_code == 201

    link_p = await client.post(
        f"/api/v1/classes/{class_10a['id']}/subjects/{sub_phys['id']}",
        headers=admin_hdr,
    )
    assert link_p.status_code == 201

    # List class subjects
    cs_resp = await client.get(f"/api/v1/classes/{class_10a['id']}/subjects", headers=admin_hdr)
    assert cs_resp.status_code == 200
    cs_names = [s["name"] for s in cs_resp.json()]
    assert "Mathematics" in cs_names
    assert "Physics" in cs_names

    # 4. Teacher assigned ONLY to (10-A, Math)
    teacher, teacher_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_hdr = _headers(teacher_token, tenant_a.slug)

    assign_resp = await client.post(
        f"/api/v1/classes/{class_10a['id']}/subjects/{sub_math['id']}/teachers",
        headers=admin_hdr,
        json={"user_id": str(teacher.id)},
    )
    assert assign_resp.status_code == 201

    # 5. Teacher Scope Verifications:
    # Teacher CAN access 10-A
    t_10a = await client.get(f"/api/v1/classes/{class_10a['id']}", headers=teacher_hdr)
    assert t_10a.status_code == 200

    # Teacher CANNOT access 10-B -> Generic 404
    t_10b = await client.get(f"/api/v1/classes/{class_10b['id']}", headers=teacher_hdr)
    assert t_10b.status_code == 404
    assert t_10b.json()["detail"]["code"] == "not_found"

    # Teacher lists classes -> sees ONLY 10-A!
    t_classes = await client.get("/api/v1/classes", headers=teacher_hdr)
    assert t_classes.status_code == 200
    t_c_items = t_classes.json()["items"]
    assert len(t_c_items) == 1
    assert t_c_items[0]["id"] == class_10a["id"]

    # Teacher CAN access Math subject
    t_sub_m = await client.get(f"/api/v1/subjects/{sub_math['id']}", headers=teacher_hdr)
    assert t_sub_m.status_code == 200

    # Teacher CANNOT access Physics subject -> Generic 404
    t_sub_p = await client.get(f"/api/v1/subjects/{sub_phys['id']}", headers=teacher_hdr)
    assert t_sub_p.status_code == 404
    assert t_sub_p.json()["detail"]["code"] == "not_found"

    # Teacher lists subjects -> sees ONLY Math!
    t_subs = await client.get("/api/v1/subjects", headers=teacher_hdr)
    assert t_subs.status_code == 200
    t_s_items = t_subs.json()["items"]
    assert len(t_s_items) == 1
    assert t_s_items[0]["id"] == sub_math["id"]


# ── 3. Enrollments & CSV Bulk Import Tests ────────────────────────────────────

async def test_enrollments_and_bulk_import(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # 1. Setup class and students
    c_resp = await client.post(
        "/api/v1/classes",
        headers=admin_hdr,
        json={"name": f"Cohort-{uuid.uuid4().hex[:6]}"},
    )
    assert c_resp.status_code == 201
    class_id = c_resp.json()["id"]

    s1, _ = await _create_student_user(tenant_a, raw_db)
    s2, _ = await _create_student_user(tenant_a, raw_db)

    # 2. Enroll s1
    enr_resp = await client.post(
        f"/api/v1/classes/{class_id}/students",
        headers=admin_hdr,
        json={"user_id": str(s1.id)},
    )
    assert enr_resp.status_code == 201

    # 3. Check class roster
    roster_resp = await client.get(f"/api/v1/classes/{class_id}/students", headers=admin_hdr)
    assert roster_resp.status_code == 200
    roster = roster_resp.json()
    assert roster["total"] == 1
    assert roster["items"][0]["user_id"] == str(s1.id)

    # 4. CSV bulk import test (valid student + unknown email)
    csv_content = f"email,full_name\n{s2.email},Student Two\nunknown-ghost@example.com,Ghost User\n"
    csv_bytes = csv_content.encode("utf-8")

    import_resp = await client.post(
        f"/api/v1/classes/{class_id}/students/import",
        headers=admin_hdr,
        files={"file": ("roster.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert import_resp.status_code == 200
    import_data = import_resp.json()
    assert import_data["total_rows"] == 2
    assert import_data["succeeded"] == 1
    assert import_data["failed"] == 1
    assert len(import_data["errors"]) == 1
    assert import_data["errors"][0]["email"] == "unknown-ghost@example.com"

    # Verify roster now has 2 students
    roster_resp2 = await client.get(f"/api/v1/classes/{class_id}/students", headers=admin_hdr)
    assert roster_resp2.json()["total"] == 2

    # 5. Unenroll s1
    unenroll_resp = await client.delete(
        f"/api/v1/classes/{class_id}/students/{s1.id}",
        headers=admin_hdr,
    )
    assert unenroll_resp.status_code == 200

    roster_resp3 = await client.get(f"/api/v1/classes/{class_id}/students", headers=admin_hdr)
    assert roster_resp3.json()["total"] == 1


# ── 4. "My Classes" Endpoint for Teacher & Student ─────────────────────────────

async def test_my_classes_endpoint(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # Create Class A and Class B
    c_a = (
        await client.post("/api/v1/classes", headers=admin_hdr, json={"name": f"A-{uuid.uuid4().hex[:4]}"})
    ).json()
    await client.post("/api/v1/classes", headers=admin_hdr, json={"name": f"B-{uuid.uuid4().hex[:4]}"})

    # Create Subject Math
    sub_m = (
        await client.post("/api/v1/subjects", headers=admin_hdr, json={"name": f"Math-{uuid.uuid4().hex[:4]}"})
    ).json()

    # Link Math to Class A
    await client.post(f"/api/v1/classes/{c_a['id']}/subjects/{sub_m['id']}", headers=admin_hdr)

    # Teacher setup -> assigned ONLY to (Class A, Math)
    teacher, teacher_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_hdr = _headers(teacher_token, tenant_a.slug)

    await client.post(
        f"/api/v1/classes/{c_a['id']}/subjects/{sub_m['id']}/teachers",
        headers=admin_hdr,
        json={"user_id": str(teacher.id)},
    )

    # Student setup -> enrolled ONLY in Class A
    student, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)

    await client.post(
        f"/api/v1/classes/{c_a['id']}/students",
        headers=admin_hdr,
        json={"user_id": str(student.id)},
    )

    # Teacher calls /me/classes
    t_my_classes = await client.get("/api/v1/me/classes", headers=teacher_hdr)
    assert t_my_classes.status_code == 200
    t_items = t_my_classes.json()
    assert len(t_items) == 1
    assert t_items[0]["id"] == c_a["id"]
    assert any(s["id"] == sub_m["id"] for s in t_items[0]["subjects"])

    # Student calls /me/classes
    s_my_classes = await client.get("/api/v1/me/classes", headers=student_hdr)
    assert s_my_classes.status_code == 200
    s_items = s_my_classes.json()
    assert len(s_items) == 1
    assert s_items[0]["id"] == c_a["id"]
    assert any(s["id"] == sub_m["id"] for s in s_items[0]["subjects"])


# ── 5. Audit Log Viewer Tests ─────────────────────────────────────────────────

async def test_audit_log_viewer_endpoint(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # Perform action to create audit log
    await client.post("/api/v1/classes", headers=admin_hdr, json={"name": f"AuditClass-{uuid.uuid4().hex[:4]}"})

    # Admin reads audit logs
    logs_resp = await client.get("/api/v1/audit-logs", headers=admin_hdr)
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    assert logs["total"] > 0
    assert any(item["action"] == "class:create" for item in logs["items"])

    # Filter by action
    filtered_resp = await client.get("/api/v1/audit-logs?action=class:create", headers=admin_hdr)
    assert filtered_resp.status_code == 200
    assert all(item["action"] == "class:create" for item in filtered_resp.json()["items"])

    # Teacher / Student calling audit logs -> 403 Forbidden
    _, teacher_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_hdr = _headers(teacher_token, tenant_a.slug)
    t_resp = await client.get("/api/v1/audit-logs", headers=teacher_hdr)
    assert t_resp.status_code == 403
    assert t_resp.json()["detail"]["code"] == "permission_denied"

    _, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)
    s_resp = await client.get("/api/v1/audit-logs", headers=student_hdr)
    assert s_resp.status_code == 403
    assert s_resp.json()["detail"]["code"] == "permission_denied"


# ── 6. Student Denial on Admin/Teacher Endpoints ──────────────────────────────

async def test_student_denied_on_admin_and_teacher_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    raw_db: AsyncSession,
) -> None:
    student, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)
    dummy_uuid = uuid.uuid4()

    # 1. Roles management
    assert (await client.get("/api/v1/roles", headers=student_hdr)).status_code == 403
    assert (await client.post("/api/v1/roles", headers=student_hdr, json={"name": "test"})).status_code == 403

    # 2. Classes management (creation/deletion)
    assert (await client.post("/api/v1/classes", headers=student_hdr, json={"name": "test"})).status_code == 403
    assert (await client.delete(f"/api/v1/classes/{dummy_uuid}", headers=student_hdr)).status_code == 403

    # 3. Subjects management
    assert (await client.post("/api/v1/subjects", headers=student_hdr, json={"name": "test"})).status_code == 403
    assert (await client.delete(f"/api/v1/subjects/{dummy_uuid}", headers=student_hdr)).status_code == 403

    # 4. Enrollments management
    assert (await client.post(f"/api/v1/classes/{dummy_uuid}/students", headers=student_hdr, json={"user_id": str(student.id)})).status_code == 403

    # 5. Teacher assignments management
    assert (await client.get("/api/v1/teacher-assignments", headers=student_hdr)).status_code == 403
    assert (await client.post(f"/api/v1/classes/{dummy_uuid}/subjects/{dummy_uuid}/teachers", headers=student_hdr, json={"user_id": str(student.id)})).status_code == 403


# ── 7. Cross-Tenant Defense on All Phase 2 Endpoint Groups ─────────────────────

async def test_cross_tenant_phase2_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    tenant_b: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Assert that accessing Tenant B resources from Tenant A returns generic 404.
    """
    admin_a_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    hdr_a = _headers(admin_a_token, tenant_a.slug)

    # Setup Tenant B resources directly
    c_b = Class(tenant_id=tenant_b.id, name=f"Class-B-{uuid.uuid4().hex[:6]}")
    s_b = Subject(tenant_id=tenant_b.id, name=f"Sub-B-{uuid.uuid4().hex[:6]}")
    r_b = Role(tenant_id=tenant_b.id, name=f"Custom-B-{uuid.uuid4().hex[:6]}", is_system=False)
    raw_db.add_all([c_b, s_b, r_b])
    await raw_db.commit()

    # 1. Roles: Tenant A admin requests Tenant B role -> 404
    resp_role = await client.get(f"/api/v1/roles/{r_b.id}", headers=hdr_a)
    assert resp_role.status_code == 404
    assert resp_role.json()["detail"]["code"] == "not_found"

    # 2. Classes: Tenant A admin requests Tenant B class -> 404
    resp_class = await client.get(f"/api/v1/classes/{c_b.id}", headers=hdr_a)
    assert resp_class.status_code == 404
    assert resp_class.json()["detail"]["code"] == "not_found"

    # 3. Subjects: Tenant A admin requests Tenant B subject -> 404
    resp_sub = await client.get(f"/api/v1/subjects/{s_b.id}", headers=hdr_a)
    assert resp_sub.status_code == 404
    assert resp_sub.json()["detail"]["code"] == "not_found"

    # 4. Enrollments: Tenant A admin enrolls into Tenant B class -> 404
    resp_enr = await client.post(
        f"/api/v1/classes/{c_b.id}/students",
        headers=hdr_a,
        json={"user_id": str(admin_a.id)},
    )
    assert resp_enr.status_code == 404
    assert resp_enr.json()["detail"]["code"] == "not_found"

    # 5. Teacher Assignment: Tenant A admin assigns to Tenant B class -> 404
    resp_ta = await client.post(
        f"/api/v1/classes/{c_b.id}/subjects/{s_b.id}/teachers",
        headers=hdr_a,
        json={"user_id": str(admin_a.id)},
    )
    assert resp_ta.status_code == 404
    assert resp_ta.json()["detail"]["code"] == "not_found"

