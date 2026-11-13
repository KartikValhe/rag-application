"""
tests/test_timetable_endpoints.py
──────────────────────────────────
Phase 4a Comprehensive Timetable Endpoint & Authorization Tests:
  1. Permissions: confirm timetable:manage and timetable:publish exist, are seeded for tenant_admin,
     scoped for teachers to assigned classes only, and students have timetable:view only.
  2. New tenant creation via platform API automatically seeds timetable permissions.
  3. Teacher publishing for assigned class (allowed) vs unassigned class (generic 404).
  4. Student denied on every draft and manage endpoint (403), allowed on /me (200).
  5. Cross-class and cross-tenant entry ID manipulation returns generic 404.
  6. Entries cap (200) and Pydantic length limits on room (64) and note (255).
  7. Academic validation: end after start, weekday 0-6, subject linked to class, teacher assigned to class+subject.
  8. Publish conflict protection: requires confirm_conflicts=true, else returns 409 with structured warnings.
  9. Audit trail verification for create_draft, update_draft, create_entry, update_entry, delete_entry, publish, unpublish.
  10. Tenant timezone settings endpoints (GET & PATCH), validated against real IANA names and audited.
"""

import uuid
from datetime import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, ClassSubject, Subject, TeacherAssignment
from app.db.models.audit import AuditLog
from app.db.models.rbac import Role, RolePermission, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from app.services.rbac import seed_tenant_system_roles

pytestmark = pytest.mark.asyncio


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


async def _create_teacher_user(tenant: Tenant, raw_db: AsyncSession) -> tuple[User, str]:
    u = User(
        tenant_id=tenant.id,
        email=f"teacher-{uuid.uuid4().hex}@example.com",
        full_name="Assigned Teacher",
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
        email=f"student-{uuid.uuid4().hex}@example.com",
        full_name="Enrolled Student",
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


# ── 1. New Tenant Seeding & Role Permissions Check ────────────────────────────

async def test_new_tenant_seeds_timetable_permissions(raw_db: AsyncSession) -> None:
    """New tenant created via platform API / system role seeding receives timetable permissions."""
    t = Tenant(slug=f"new-tenant-{uuid.uuid4().hex}", name="New Org", kind="school")
    raw_db.add(t)
    await raw_db.commit()

    await seed_tenant_system_roles(t.id, raw_db)
    await raw_db.commit()

    # Query role permissions
    query = (
        select(Role.name, RolePermission.permission_id)
        .join(RolePermission, (RolePermission.tenant_id == Role.tenant_id) & (RolePermission.role_id == Role.id))
        .where(Role.tenant_id == t.id)
    )
    res = await raw_db.execute(query)
    perms_by_role: dict[str, set[str]] = {}
    for r_name, p_id in res.all():
        perms_by_role.setdefault(r_name, set()).add(p_id)

    # 1. Admin gets all
    assert "timetable:manage" in perms_by_role["tenant_admin"]
    assert "timetable:publish" in perms_by_role["tenant_admin"]
    assert "timetable:view" in perms_by_role["tenant_admin"]

    # 2. Teacher gets manage, publish, view
    assert "timetable:manage" in perms_by_role["teacher"]
    assert "timetable:publish" in perms_by_role["teacher"]
    assert "timetable:view" in perms_by_role["teacher"]

    # 3. Student gets ONLY view
    assert "timetable:view" in perms_by_role["student"]
    assert "timetable:manage" not in perms_by_role["student"]
    assert "timetable:publish" not in perms_by_role["student"]


async def test_platform_created_tenant_receives_timetable_permissions(
    client: AsyncClient,
    platform_token: str,
    raw_db: AsyncSession,
) -> None:
    """Tenants created through the platform API receive timetable permissions upon admin creation."""
    p_headers = {"Authorization": f"Bearer {platform_token}"}
    slug = f"plat-org-{uuid.uuid4().hex}"

    # 1. Create tenant via platform API
    create_resp = await client.post(
        "/api/v1/platform/tenants",
        headers=p_headers,
        json={"slug": slug, "name": "Platform Timetable Org", "kind": "school"},
    )
    assert create_resp.status_code == 201
    t_id = uuid.UUID(create_resp.json()["id"])

    # 2. Create tenant admin via platform API (which triggers seed_tenant_system_roles)
    admin_resp = await client.post(
        f"/api/v1/platform/tenants/{t_id}/admins",
        headers=p_headers,
        json={"email": f"admin-{uuid.uuid4().hex}@plat.local", "full_name": "Platform Admin"},
    )
    assert admin_resp.status_code == 201

    # 3. Query role permissions
    query = (
        select(Role.name, RolePermission.permission_id)
        .join(RolePermission, (RolePermission.tenant_id == Role.tenant_id) & (RolePermission.role_id == Role.id))
        .where(Role.tenant_id == t_id)
    )
    res = await raw_db.execute(query)
    perms_by_role: dict[str, set[str]] = {}
    for r_name, p_id in res.all():
        perms_by_role.setdefault(r_name, set()).add(p_id)

    assert "timetable:manage" in perms_by_role["tenant_admin"]
    assert "timetable:publish" in perms_by_role["tenant_admin"]
    assert "timetable:view" in perms_by_role["tenant_admin"]

    assert "timetable:manage" in perms_by_role["teacher"]
    assert "timetable:publish" in perms_by_role["teacher"]
    assert "timetable:view" in perms_by_role["teacher"]

    assert "timetable:view" in perms_by_role["student"]
    assert "timetable:manage" not in perms_by_role["student"]
    assert "timetable:publish" not in perms_by_role["student"]


# ── 2. Teacher Scoping: Assigned vs Unassigned Class ───────────────────────────

async def test_teacher_scoping_assigned_vs_unassigned(
    client: AsyncClient,
    tenant_a: Tenant,
    raw_db: AsyncSession,
) -> None:
    """
    Teacher publishing/managing for assigned class is ALLOWED.
    Teacher attempting to publish/manage for unassigned class is DENIED with generic 404.
    """
    teacher, teacher_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_hdr = _headers(teacher_token, tenant_a.slug)

    # Create Class A and Class B
    c_a = Class(tenant_id=tenant_a.id, name=f"Class-A-{uuid.uuid4().hex[:6]}")
    c_b = Class(tenant_id=tenant_a.id, name=f"Class-B-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant_a.id, name=f"Math-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c_a, c_b, sub])
    await raw_db.flush()

    # Link Subject to Class A and B
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c_a.id, subject_id=sub.id))
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c_b.id, subject_id=sub.id))

    # Assign teacher ONLY to Class A
    raw_db.add(
        TeacherAssignment(
            tenant_id=tenant_a.id,
            user_id=teacher.id,
            class_id=c_a.id,
            subject_id=sub.id,
        )
    )
    await raw_db.commit()

    # 1. Teacher acts on assigned Class A -> 200 OK
    resp_draft = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=teacher_hdr)
    assert resp_draft.status_code == 200

    # Scoped teacher picker endpoint returns assigned teachers without requiring assignment:manage
    resp_pick_subj = await client.get(
        f"/api/v1/classes/{c_a.id}/subjects/{sub.id}/assigned-teachers",
        headers=teacher_hdr,
    )
    assert resp_pick_subj.status_code == 200
    teachers_data = resp_pick_subj.json()
    assert len(teachers_data) == 1
    assert teachers_data[0]["id"] == str(teacher.id)

    resp_pick_class = await client.get(
        f"/api/v1/classes/{c_a.id}/assigned-teachers",
        headers=teacher_hdr,
    )
    assert resp_pick_class.status_code == 200
    assert len(resp_pick_class.json()) == 1

    resp_pub = await client.post(
        f"/api/v1/classes/{c_a.id}/timetables/publish",
        headers=teacher_hdr,
        json={"confirm_conflicts": True},
    )
    assert resp_pub.status_code == 200

    # 2. Teacher acts on unassigned Class B -> Generic 404 (defense in depth)
    resp_unassigned_get = await client.get(f"/api/v1/classes/{c_b.id}/timetables/draft", headers=teacher_hdr)
    assert resp_unassigned_get.status_code == 404
    assert resp_unassigned_get.json()["detail"]["code"] == "not_found"

    resp_unassigned_teachers = await client.get(
        f"/api/v1/classes/{c_b.id}/subjects/{sub.id}/assigned-teachers",
        headers=teacher_hdr,
    )
    assert resp_unassigned_teachers.status_code == 404
    assert resp_unassigned_teachers.json()["detail"]["code"] == "not_found"

    resp_unassigned_pub = await client.post(
        f"/api/v1/classes/{c_b.id}/timetables/publish",
        headers=teacher_hdr,
        json={"confirm_conflicts": True},
    )
    assert resp_unassigned_pub.status_code == 404
    assert resp_unassigned_pub.json()["detail"]["code"] == "not_found"


# ── 3. Student Denied On Every Manage/Publish Endpoint ─────────────────────────

async def test_student_denied_on_draft_and_manage_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    raw_db: AsyncSession,
) -> None:
    """Student caller is rejected with 403 Forbidden on all timetable draft/manage endpoints."""
    student, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)

    c = Class(tenant_id=tenant_a.id, name=f"StudentClass-{uuid.uuid4().hex[:6]}")
    raw_db.add(c)
    await raw_db.commit()

    dummy_id = uuid.uuid4()

    # Every manage/draft/publish endpoint returns 403 Forbidden
    endpoints = [
        ("GET", f"/api/v1/classes/{c.id}/timetables", None),
        ("GET", f"/api/v1/classes/{c.id}/timetables/draft", None),
        ("POST", f"/api/v1/classes/{c.id}/timetables/draft", {"academic_year": "2026-2027"}),
        ("PUT", f"/api/v1/classes/{c.id}/timetables/draft", {"academic_year": "2026-2027"}),
        ("POST", f"/api/v1/classes/{c.id}/timetables/draft/entries", {"day_of_week": 0, "start_time": "09:00", "end_time": "10:00", "subject_id": str(dummy_id)}),
        ("PUT", f"/api/v1/classes/{c.id}/timetables/draft/entries/{dummy_id}", {"day_of_week": 0, "start_time": "09:00", "end_time": "10:00", "subject_id": str(dummy_id)}),
        ("DELETE", f"/api/v1/classes/{c.id}/timetables/draft/entries/{dummy_id}", None),
        ("POST", f"/api/v1/classes/{c.id}/timetables/check-conflicts", None),
        ("POST", f"/api/v1/classes/{c.id}/timetables/publish", {"confirm_conflicts": True}),
        ("POST", f"/api/v1/classes/{c.id}/timetables/unpublish", None),
        ("GET", f"/api/v1/classes/{c.id}/subjects/{dummy_id}/assigned-teachers", None),
        ("GET", f"/api/v1/classes/{c.id}/assigned-teachers", None),
    ]

    for method, path, payload in endpoints:
        if method == "GET":
            resp = await client.get(path, headers=student_hdr)
        elif method == "POST":
            resp = await client.post(path, headers=student_hdr, json=payload or {})
        elif method == "PUT":
            resp = await client.put(path, headers=student_hdr, json=payload or {})
        elif method == "DELETE":
            resp = await client.delete(path, headers=student_hdr)
        assert resp.status_code == 403, f"{method} {path} returned {resp.status_code}, expected 403"
        assert resp.json()["detail"]["code"] == "permission_denied"

    # Enroll student in class `c`
    from app.db.models.academic import Enrollment
    raw_db.add(Enrollment(tenant_id=tenant_a.id, user_id=student.id, class_id=c.id))

    # Add a draft-only timetable with an entry to class `c`
    draft_tt = Timetable(tenant_id=tenant_a.id, class_id=c.id, status="draft", version=1)
    raw_db.add(draft_tt)
    await raw_db.flush()

    sub_demo = Subject(tenant_id=tenant_a.id, name=f"DraftOnlySub-{uuid.uuid4().hex[:6]}")
    raw_db.add(sub_demo)
    await raw_db.flush()
    raw_db.add(
        TimetableEntry(
            tenant_id=tenant_a.id,
            timetable_id=draft_tt.id,
            class_id=c.id,
            subject_id=sub_demo.id,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
        )
    )
    await raw_db.commit()

    # Drafts are strictly INVISIBLE to students on /me endpoints
    res_me_tt = await client.get("/api/v1/me/timetable", headers=student_hdr)
    assert res_me_tt.status_code == 200
    me_tt_data = res_me_tt.json()
    assert len(me_tt_data["classes"]) == 0

    res_me_nl = await client.get("/api/v1/me/next-lecture", headers=student_hdr)
    assert res_me_nl.status_code == 200
    me_nl_data = res_me_nl.json()
    assert me_nl_data["status"] == "no_published_timetable"
    assert me_nl_data["next_lecture"] is None


# ── 4. Cross-Class and Cross-Tenant Entry ID Manipulation ──────────────────────

async def test_foreign_entry_id_returns_generic_404(
    client: AsyncClient,
    tenant_a: Tenant,
    tenant_b: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Accessing an entry_id from another class or tenant returns a generic 404."""
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c_a = Class(tenant_id=tenant_a.id, name=f"ClassA-{uuid.uuid4().hex[:6]}")
    c_b = Class(tenant_id=tenant_b.id, name=f"ClassB-{uuid.uuid4().hex[:6]}")
    sub_b = Subject(tenant_id=tenant_b.id, name=f"SubB-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c_a, c_b, sub_b])
    await raw_db.flush()

    tt_b = Timetable(tenant_id=tenant_b.id, class_id=c_b.id, status="draft", version=1)
    raw_db.add(tt_b)
    await raw_db.flush()

    entry_b = TimetableEntry(
        tenant_id=tenant_b.id,
        timetable_id=tt_b.id,
        class_id=c_b.id,
        subject_id=sub_b.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
    )
    raw_db.add(entry_b)
    await raw_db.commit()

    # Attempt to update entry_b under Class A of Tenant A
    payload = {
        "day_of_week": 1,
        "start_time": "11:00",
        "end_time": "12:00",
        "subject_id": str(sub_b.id),
    }
    put_resp = await client.put(
        f"/api/v1/classes/{c_a.id}/timetables/draft/entries/{entry_b.id}",
        headers=admin_hdr,
        json=payload,
    )
    # Generic 404
    assert put_resp.status_code == 404
    assert put_resp.json()["detail"]["code"] == "not_found"

    del_resp = await client.delete(
        f"/api/v1/classes/{c_a.id}/timetables/draft/entries/{entry_b.id}",
        headers=admin_hdr,
    )
    assert del_resp.status_code == 404
    assert del_resp.json()["detail"]["code"] == "not_found"


# ── 5. Validation Rules: End after Start, Weekday, Room & Note Limits ──────────

async def test_validation_rules_and_pydantic_limits(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Enforces start < end, weekday 0-6, room <= 64, note <= 255."""
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c = Class(tenant_id=tenant_a.id, name=f"ClassVal-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant_a.id, name=f"SubVal-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c, sub])
    await raw_db.flush()
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c.id, subject_id=sub.id))
    await raw_db.commit()

    # 1. End time <= start time -> 422
    r_bad_time = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 0, "start_time": "10:00", "end_time": "09:00", "subject_id": str(sub.id)},
    )
    assert r_bad_time.status_code == 422

    # 2. Weekday outside 0-6 -> 422
    r_bad_dow = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 7, "start_time": "09:00", "end_time": "10:00", "subject_id": str(sub.id)},
    )
    assert r_bad_dow.status_code == 422

    # 3. Room > 64 chars -> 422
    r_bad_room = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={
            "day_of_week": 0,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(sub.id),
            "room": "R" * 65,
        },
    )
    assert r_bad_room.status_code == 422

    # 4. Note > 255 chars -> 422
    r_bad_note = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={
            "day_of_week": 0,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(sub.id),
            "note": "N" * 256,
        },
    )
    assert r_bad_note.status_code == 422


async def test_timetable_entries_cap_200_enforced(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """A timetable draft cannot exceed 200 entries (HTTP 422 entries_cap_exceeded)."""
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c = Class(tenant_id=tenant_a.id, name=f"ClassCap-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant_a.id, name=f"SubCap-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c, sub])
    await raw_db.flush()
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c.id, subject_id=sub.id))

    draft = Timetable(tenant_id=tenant_a.id, class_id=c.id, status="draft", version=1)
    raw_db.add(draft)
    await raw_db.flush()

    # Pre-populate 200 entries
    entries = [
        TimetableEntry(
            tenant_id=tenant_a.id,
            timetable_id=draft.id,
            class_id=c.id,
            subject_id=sub.id,
            day_of_week=i % 7,
            start_time=time(8 + (i % 10), 0),
            end_time=time(9 + (i % 10), 0),
            room=f"R{i}",
        )
        for i in range(200)
    ]
    raw_db.add_all(entries)
    await raw_db.commit()

    # Attempt to add 201st entry -> 422 entries_cap_exceeded
    r_overflow = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 0, "start_time": "07:00", "end_time": "08:00", "subject_id": str(sub.id)},
    )
    assert r_overflow.status_code == 422
    assert r_overflow.json()["detail"]["code"] == "entries_cap_exceeded"


# ── 6. Academic Validation: Subject linked to Class & Teacher Assigned ─────────

async def test_academic_relations_validation(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Adding an entry with subject NOT linked to class -> 422 subject_not_linked.
    Adding an entry with teacher NOT assigned to that class+subject -> 422 teacher_not_assigned.
    """
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c = Class(tenant_id=tenant_a.id, name=f"ClassRel-{uuid.uuid4().hex[:6]}")
    sub_linked = Subject(tenant_id=tenant_a.id, name=f"Linked-{uuid.uuid4().hex[:6]}")
    sub_unlinked = Subject(tenant_id=tenant_a.id, name=f"Unlinked-{uuid.uuid4().hex[:6]}")
    teacher, _ = await _create_teacher_user(tenant_a, raw_db)

    raw_db.add_all([c, sub_linked, sub_unlinked])
    await raw_db.flush()

    # Link ONLY sub_linked
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c.id, subject_id=sub_linked.id))
    await raw_db.commit()

    # 1. Unlinked subject -> 422
    r_unlinked = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 0, "start_time": "09:00", "end_time": "10:00", "subject_id": str(sub_unlinked.id)},
    )
    assert r_unlinked.status_code == 422
    assert r_unlinked.json()["detail"]["code"] == "subject_not_linked"

    # 2. Linked subject, but teacher NOT assigned -> 422
    r_unassigned_teacher = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={
            "day_of_week": 0,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(sub_linked.id),
            "teacher_id": str(teacher.id),
        },
    )
    assert r_unassigned_teacher.status_code == 422
    assert r_unassigned_teacher.json()["detail"]["code"] == "teacher_not_assigned"

    # 3. Assign teacher -> 200 OK
    raw_db.add(
        TeacherAssignment(
            tenant_id=tenant_a.id,
            user_id=teacher.id,
            class_id=c.id,
            subject_id=sub_linked.id,
        )
    )
    await raw_db.commit()

    r_ok = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={
            "day_of_week": 0,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(sub_linked.id),
            "teacher_id": str(teacher.id),
        },
    )
    assert r_ok.status_code == 200


# ── 7. Conflict Detection on Publish (explicit confirm flag) ───────────────────

async def test_conflict_detection_requires_explicit_confirm(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Publishing a timetable with conflicts returns 409 unless confirm_conflicts=True."""
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c1 = Class(tenant_id=tenant_a.id, name=f"Class1-{uuid.uuid4().hex[:6]}")
    c2 = Class(tenant_id=tenant_a.id, name=f"Class2-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant_a.id, name=f"SubConf-{uuid.uuid4().hex[:6]}")
    teacher, _ = await _create_teacher_user(tenant_a, raw_db)

    raw_db.add_all([c1, c2, sub])
    await raw_db.flush()

    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c1.id, subject_id=sub.id))
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c2.id, subject_id=sub.id))
    raw_db.add(TeacherAssignment(tenant_id=tenant_a.id, user_id=teacher.id, class_id=c1.id, subject_id=sub.id))
    raw_db.add(TeacherAssignment(tenant_id=tenant_a.id, user_id=teacher.id, class_id=c2.id, subject_id=sub.id))
    await raw_db.commit()

    # 1. Publish Class 1 timetable with Room 101 on Monday 09:00-10:00
    tt1 = Timetable(tenant_id=tenant_a.id, class_id=c1.id, status="published", version=1)
    raw_db.add(tt1)
    await raw_db.flush()
    raw_db.add(
        TimetableEntry(
            tenant_id=tenant_a.id,
            timetable_id=tt1.id,
            class_id=c1.id,
            subject_id=sub.id,
            teacher_id=teacher.id,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
            room="Room 101",
        )
    )
    await raw_db.commit()

    # 2. Add conflicting entry to Class 2 draft (same teacher and same room!)
    r_add = await client.post(
        f"/api/v1/classes/{c2.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={
            "day_of_week": 0,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(sub.id),
            "teacher_id": str(teacher.id),
            "room": "Room 101",
        },
    )
    assert r_add.status_code == 200

    # 3. Publish without confirm_conflicts -> 409 Conflict
    r_pub_fail = await client.post(
        f"/api/v1/classes/{c2.id}/timetables/publish",
        headers=admin_hdr,
        json={"confirm_conflicts": False},
    )
    assert r_pub_fail.status_code == 409
    body = r_pub_fail.json()["detail"]
    assert body["code"] == "timetable_conflicts_detected"
    assert len(body["conflicts"]) >= 1

    # 4. Publish with confirm_conflicts=True -> 200 OK
    r_pub_ok = await client.post(
        f"/api/v1/classes/{c2.id}/timetables/publish",
        headers=admin_hdr,
        json={"confirm_conflicts": True},
    )
    assert r_pub_ok.status_code == 200
    assert r_pub_ok.json()["status"] == "published"


# ── 8. Audit Trail Verification ────────────────────────────────────────────────

async def test_audit_trail_for_timetable_actions(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Verifies that create_draft, update_entry, delete_entry, publish, and unpublish write audit records."""
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    c = Class(tenant_id=tenant_a.id, name=f"AuditClass-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant_a.id, name=f"AuditSub-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c, sub])
    await raw_db.flush()
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c.id, subject_id=sub.id))
    await raw_db.commit()

    # 1. Create draft
    r_draft = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft",
        headers=admin_hdr,
        json={"academic_year": "2026-2027", "term": "Fall", "entries": []},
    )
    assert r_draft.status_code == 200

    # 2. Add entry
    r_entry = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 0, "start_time": "09:00", "end_time": "10:00", "subject_id": str(sub.id)},
    )
    assert r_entry.status_code == 200
    entry_id = r_entry.json()["id"]

    # 3. Update entry
    r_up = await client.put(
        f"/api/v1/classes/{c.id}/timetables/draft/entries/{entry_id}",
        headers=admin_hdr,
        json={"day_of_week": 1, "start_time": "10:00", "end_time": "11:00", "subject_id": str(sub.id)},
    )
    assert r_up.status_code == 200

    # 4. Add and delete entry to test delete_entry audit
    r_entry2 = await client.post(
        f"/api/v1/classes/{c.id}/timetables/draft/entries",
        headers=admin_hdr,
        json={"day_of_week": 2, "start_time": "14:00", "end_time": "15:00", "subject_id": str(sub.id)},
    )
    assert r_entry2.status_code == 200
    entry2_id = r_entry2.json()["id"]

    r_del = await client.delete(
        f"/api/v1/classes/{c.id}/timetables/draft/entries/{entry2_id}",
        headers=admin_hdr,
    )
    assert r_del.status_code == 204

    # 5. Publish
    r_pub = await client.post(
        f"/api/v1/classes/{c.id}/timetables/publish",
        headers=admin_hdr,
        json={"confirm_conflicts": True},
    )
    assert r_pub.status_code == 200

    # 6. Unpublish
    r_unpub = await client.post(
        f"/api/v1/classes/{c.id}/timetables/unpublish",
        headers=admin_hdr,
    )
    assert r_unpub.status_code == 200

    # Query audit logs
    audit_res = await raw_db.execute(
        select(AuditLog.action).where(AuditLog.tenant_id == tenant_a.id)
    )
    actions = set(audit_res.scalars().all())

    assert "timetable:create_draft" in actions
    assert "timetable:create_entry" in actions
    assert "timetable:update_entry" in actions
    assert "timetable:delete_entry" in actions
    assert "timetable:publish" in actions
    assert "timetable:unpublish" in actions


# ── 9. Tenant Timezone Admin Endpoints (GET & PATCH) ───────────────────────────

async def test_tenant_timezone_admin_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Tenant admin can read and update timezone, validated against IANA zoneinfo.
    Updates are written to database and logged in audit trail.
    Invalid timezone is rejected with 422.
    """
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    # 1. Read default timezone
    r_get = await client.get("/api/v1/admin/settings/timezone", headers=admin_hdr)
    assert r_get.status_code == 200
    assert r_get.json()["timezone"] in ("Asia/Kolkata", "UTC")

    # 2. Update to valid IANA timezone
    r_patch = await client.patch(
        "/api/v1/admin/settings/timezone",
        headers=admin_hdr,
        json={"timezone": "America/New_York"},
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["timezone"] == "America/New_York"

    # Verify DB updated
    t_res = await raw_db.execute(select(Tenant.timezone).where(Tenant.id == tenant_a.id))
    assert t_res.scalar_one() == "America/New_York"

    # 3. Reject invalid timezone
    r_invalid = await client.patch(
        "/api/v1/admin/settings/timezone",
        headers=admin_hdr,
        json={"timezone": "Moon/Base_Alpha"},
    )
    assert r_invalid.status_code == 422
    assert r_invalid.json()["detail"]["code"] == "invalid_timezone"

    # Verify audit log
    audit_res = await raw_db.execute(
        select(AuditLog).where(AuditLog.tenant_id == tenant_a.id, AuditLog.action == "tenant:update_timezone")
    )
    audit_entry = audit_res.scalar_one_or_none()
    assert audit_entry is not None
    assert audit_entry.details["new_timezone"] == "America/New_York"

    # 4. Non-admin users (teacher, student) are rejected with 403 Forbidden
    teacher, teacher_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_hdr = _headers(teacher_token, tenant_a.slug)
    r_teacher_patch = await client.patch(
        "/api/v1/admin/settings/timezone",
        headers=teacher_hdr,
        json={"timezone": "UTC"},
    )
    assert r_teacher_patch.status_code == 403
    assert r_teacher_patch.json()["detail"]["code"] == "permission_denied"

    student, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)
    r_student_patch = await client.patch(
        "/api/v1/admin/settings/timezone",
        headers=student_hdr,
        json={"timezone": "UTC"},
    )
    assert r_student_patch.status_code == 403
    assert r_student_patch.json()["detail"]["code"] == "permission_denied"


# ── 11. Comprehensive Route x Role Matrix ─────────────────────────────────────

async def test_timetable_route_role_matrix(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Route x Role Authorization Matrix:
      Routes:
        1. GET /classes/{class_id}/timetable (timetable:view)
        2. POST /classes/{class_id}/timetables/entries (timetable:manage)
        3. POST /classes/{class_id}/timetables/publish (timetable:publish)
        4. GET /classes/{class_id}/assigned-teachers (timetable:manage)
        5. GET /me/timetable (authenticated)
        6. GET /me/next-lecture (authenticated)
      Roles:
        - Admin: Full access
        - Teacher Assigned: Allowed on assigned class, generic 404 on unassigned class
        - Teacher Unassigned: Generic 404 on manage/publish/assigned-teachers
        - Student: timetable:view allowed, manage/publish forbidden (403), /me allowed (200)
        - Unauthenticated: 401 Unauthorized across all endpoints
    """
    admin_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    admin_hdr = _headers(admin_token, tenant_a.slug)

    teacher_1, teacher_1_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_1_hdr = _headers(teacher_1_token, tenant_a.slug)

    teacher_2, teacher_2_token = await _create_teacher_user(tenant_a, raw_db)
    teacher_2_hdr = _headers(teacher_2_token, tenant_a.slug)

    student, student_token = await _create_student_user(tenant_a, raw_db)
    student_hdr = _headers(student_token, tenant_a.slug)

    c_a = Class(tenant_id=tenant_a.id, name=f"MatrixClassA-{uuid.uuid4().hex}")
    c_b = Class(tenant_id=tenant_a.id, name=f"MatrixClassB-{uuid.uuid4().hex}")
    sub = Subject(tenant_id=tenant_a.id, name=f"MatrixSub-{uuid.uuid4().hex}")
    raw_db.add_all([c_a, c_b, sub])
    await raw_db.flush()

    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c_a.id, subject_id=sub.id))
    raw_db.add(ClassSubject(tenant_id=tenant_a.id, class_id=c_b.id, subject_id=sub.id))
    raw_db.add(TeacherAssignment(tenant_id=tenant_a.id, user_id=teacher_1.id, class_id=c_a.id, subject_id=sub.id))
    await raw_db.commit()

    entry_payload = {
        "subject_id": str(sub.id),
        "teacher_id": str(teacher_1.id),
        "day_of_week": 1,
        "start_time": "09:00",
        "end_time": "10:00",
        "room": "Room 101",
    }
    unauth_hdr = {"X-Tenant-Slug": tenant_a.slug}

    # 1. GET /classes/{id}/timetables/draft
    res = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=admin_hdr)
    assert res.status_code == 200
    res = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=teacher_1_hdr)
    assert res.status_code == 200
    res = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=teacher_2_hdr)
    assert res.status_code == 404
    res = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=student_hdr)
    assert res.status_code == 403
    res = await client.get(f"/api/v1/classes/{c_a.id}/timetables/draft", headers=unauth_hdr)
    assert res.status_code == 401

    # 2. POST /classes/{id}/timetables/draft/entries
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/draft/entries", headers=admin_hdr, json=entry_payload)
    assert res.status_code == 200
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/draft/entries", headers=teacher_1_hdr, json=entry_payload)
    assert res.status_code == 200
    res = await client.post(f"/api/v1/classes/{c_b.id}/timetables/draft/entries", headers=teacher_1_hdr, json=entry_payload)
    assert res.status_code == 404
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/draft/entries", headers=teacher_2_hdr, json=entry_payload)
    assert res.status_code == 404
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/draft/entries", headers=student_hdr, json=entry_payload)
    assert res.status_code == 403
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/draft/entries", headers=unauth_hdr, json=entry_payload)
    assert res.status_code == 401

    # 3. POST /classes/{id}/timetables/publish
    res = await client.post(f"/api/v1/classes/{c_b.id}/timetables/publish", headers=teacher_1_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 404
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/publish", headers=teacher_2_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 404
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/publish", headers=student_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 403
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/publish", headers=unauth_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 401
    # Teacher 1 publishes -> 200
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/publish", headers=teacher_1_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 200
    # Admin unpublishes -> 200 (reverting to draft)
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/unpublish", headers=admin_hdr)
    assert res.status_code == 200
    # Admin publishes -> 200
    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/publish", headers=admin_hdr, json={"confirm_conflicts": True})
    assert res.status_code == 200

    # 4. GET /classes/{id}/assigned-teachers
    res = await client.get(f"/api/v1/classes/{c_a.id}/assigned-teachers", headers=admin_hdr)
    assert res.status_code == 200
    res = await client.get(f"/api/v1/classes/{c_a.id}/assigned-teachers", headers=teacher_1_hdr)
    assert res.status_code == 200
    res = await client.get(f"/api/v1/classes/{c_b.id}/assigned-teachers", headers=teacher_1_hdr)
    assert res.status_code == 404
    res = await client.get(f"/api/v1/classes/{c_a.id}/assigned-teachers", headers=teacher_2_hdr)
    assert res.status_code == 404
    res = await client.get(f"/api/v1/classes/{c_a.id}/assigned-teachers", headers=student_hdr)
    assert res.status_code == 403
    res = await client.get(f"/api/v1/classes/{c_a.id}/assigned-teachers", headers=unauth_hdr)
    assert res.status_code == 401

    # 5. GET /me/timetable
    for hdr in (admin_hdr, teacher_1_hdr, teacher_2_hdr, student_hdr):
        res = await client.get("/api/v1/me/timetable", headers=hdr)
        assert res.status_code == 200
        assert "classes" in res.json()
    res = await client.get("/api/v1/me/timetable", headers=unauth_hdr)
    assert res.status_code == 401

    # 6. GET /me/next-lecture
    for hdr in (admin_hdr, teacher_1_hdr, teacher_2_hdr, student_hdr):
        res = await client.get("/api/v1/me/next-lecture", headers=hdr)
        assert res.status_code == 200
        assert "next_lecture" in res.json()
    res = await client.get("/api/v1/me/next-lecture", headers=unauth_hdr)
    assert res.status_code == 401

