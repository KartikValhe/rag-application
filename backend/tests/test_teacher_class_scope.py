"""
tests/test_teacher_class_scope.py
─────────────────────────────────
Class-scoped access with realistic assignment data:
  1. Regression: a teacher assigned to SEVERAL subjects in one class (as the dev
     seed does: Mathematics + Physics in Class 10-A) used to get HTTP 500 on every
     class-scoped route, because the scope check expected at most one assignment row.
  2. A student enrolled in another class sees no timetable or entries of this class.
  3. A teacher not assigned to the class gets the generic 404 on every class
     timetable endpoint.
"""

import uuid
from datetime import time

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


async def _make_user(tenant: Tenant, raw_db: AsyncSession, role_name: str) -> tuple[User, str]:
    """Create a user holding one seeded system role (same permissions as the dev seed)."""
    u = User(
        tenant_id=tenant.id,
        email=f"{role_name}-{uuid.uuid4().hex}@example.com",
        full_name=f"Test {role_name}",
        is_admin=False,
        status="active",
    )
    raw_db.add(u)
    await raw_db.flush()
    role_id = (
        await raw_db.execute(select(Role.id).where(Role.tenant_id == tenant.id, Role.name == role_name))
    ).scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=u.id, role_id=role_id))
    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant.id), user_id=str(u.id), is_admin=False)
    return u, token


async def _class_with_two_subjects(
    tenant: Tenant, raw_db: AsyncSession, teacher: User | None
) -> tuple[Class, Subject, Subject]:
    """A class offering two subjects; optionally assign the teacher to BOTH of them."""
    c = Class(tenant_id=tenant.id, name=f"10-A-{uuid.uuid4().hex[:6]}")
    math = Subject(tenant_id=tenant.id, name=f"Math-{uuid.uuid4().hex[:6]}")
    phys = Subject(tenant_id=tenant.id, name=f"Phys-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c, math, phys])
    await raw_db.flush()
    for s in (math, phys):
        raw_db.add(ClassSubject(tenant_id=tenant.id, class_id=c.id, subject_id=s.id))
        if teacher is not None:
            raw_db.add(
                TeacherAssignment(tenant_id=tenant.id, user_id=teacher.id, class_id=c.id, subject_id=s.id)
            )
    await raw_db.commit()
    return c, math, phys


async def _publish_timetable(tenant: Tenant, raw_db: AsyncSession, c: Class, sub: Subject) -> TimetableEntry:
    tt = Timetable(tenant_id=tenant.id, class_id=c.id, status="published", version=1)
    raw_db.add(tt)
    await raw_db.flush()
    entry = TimetableEntry(
        tenant_id=tenant.id,
        timetable_id=tt.id,
        class_id=c.id,
        subject_id=sub.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
    )
    raw_db.add(entry)
    await raw_db.commit()
    return entry


async def _call(client: AsyncClient, method: str, path: str, hdr: dict[str, str], body: object) -> Response:
    if method == "GET":
        return await client.get(path, headers=hdr)
    if method == "DELETE":
        return await client.delete(path, headers=hdr)
    if method == "UPLOAD":
        files = {"file": ("tt.csv", b"day,start,end,subject\n", "text/csv")}
        return await client.post(path, headers=hdr, files=files)
    return await client.request(method, path, headers=hdr, json=body if body is not None else {})


def _timetable_endpoints(class_id: uuid.UUID, subject_id: uuid.UUID) -> list[tuple[str, str, object]]:
    """Every class-scoped timetable route (keep in sync with app/api/v1/timetable.py)."""
    base = f"/api/v1/classes/{class_id}"
    entry = uuid.uuid4()
    entry_body = {"day_of_week": 0, "start_time": "09:00", "end_time": "10:00", "subject_id": str(subject_id)}
    return [
        ("GET", f"{base}/subjects/{subject_id}/assigned-teachers", None),
        ("GET", f"{base}/assigned-teachers", None),
        ("GET", f"{base}/timetables", None),
        ("GET", f"{base}/timetables/draft", None),
        ("POST", f"{base}/timetables/draft", {"academic_year": "2026-2027"}),
        ("PUT", f"{base}/timetables/draft", {"academic_year": "2026-2027"}),
        ("POST", f"{base}/timetables/draft/entries", entry_body),
        ("PUT", f"{base}/timetables/draft/entries/{entry}", entry_body),
        ("DELETE", f"{base}/timetables/draft/entries/{entry}", None),
        ("POST", f"{base}/timetables/check-conflicts", None),
        ("POST", f"{base}/timetables/publish", {"confirm_conflicts": True}),
        ("POST", f"{base}/timetables/unpublish", None),
        ("GET", f"{base}/timetables/template.csv", None),
        ("GET", f"{base}/timetables/template.xlsx", None),
        ("UPLOAD", f"{base}/timetables/import-preview", None),
        ("POST", f"{base}/timetables/save-imported-draft", {"entries": []}),
    ]


# ── 1. Regression: teacher with several subjects in one class ─────────────────

async def test_teacher_with_multiple_subjects_in_class_gets_200(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    """Two assignment rows for (teacher, class) must not crash the class scope check."""
    teacher, token = await _make_user(tenant_a, raw_db, "teacher")
    hdr = _headers(token, tenant_a.slug)
    c, math, phys = await _class_with_two_subjects(tenant_a, raw_db, teacher)

    resp = await client.get(f"/api/v1/classes/{c.id}/subjects", headers=hdr)
    assert resp.status_code == 200, resp.text
    assert {s["id"] for s in resp.json()} == {str(math.id), str(phys.id)}

    for path in (
        f"/api/v1/classes/{c.id}/timetables",
        f"/api/v1/classes/{c.id}/timetables/draft",
        f"/api/v1/classes/{c.id}/assigned-teachers",
    ):
        resp = await client.get(path, headers=hdr)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"

    # Subject-scoped routes still work for each assigned subject
    for s in (math, phys):
        resp = await client.get(f"/api/v1/classes/{c.id}/subjects/{s.id}/assigned-teachers", headers=hdr)
        assert resp.status_code == 200


# ── 2. Student in another class sees nothing of this class ────────────────────

async def test_student_in_other_class_sees_no_timetable_of_this_class(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    student, token = await _make_user(tenant_a, raw_db, "student")
    hdr = _headers(token, tenant_a.slug)

    target, math, _ = await _class_with_two_subjects(tenant_a, raw_db, None)
    own, own_sub, _ = await _class_with_two_subjects(tenant_a, raw_db, None)
    raw_db.add(Enrollment(tenant_id=tenant_a.id, user_id=student.id, class_id=own.id))
    await raw_db.commit()
    foreign_entry = await _publish_timetable(tenant_a, raw_db, target, math)
    await _publish_timetable(tenant_a, raw_db, own, own_sub)

    # Unfiltered /me/timetable: only the student's own class, no foreign entries
    resp = await client.get("/api/v1/me/timetable", headers=hdr)
    assert resp.status_code == 200
    data = resp.json()
    assert [t["class_id"] for t in data["classes"]] == [str(own.id)]
    all_entry_ids = {e["id"] for t in data["classes"] for e in t["entries"]}
    all_entry_ids |= {e["id"] for e in data["teaching_entries"]}
    assert str(foreign_entry.id) not in all_entry_ids
    assert str(target.id) not in resp.text

    # Explicitly asking for the foreign class: generic 404, never its data
    for path in (
        f"/api/v1/me/timetable?class_id={target.id}",
        f"/api/v1/me/next-lecture?class_id={target.id}",
    ):
        resp = await client.get(path, headers=hdr)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
        assert resp.json()["detail"]["code"] == "not_found"

    # Management routes stay closed (no timetable:manage permission)
    resp = await client.get(f"/api/v1/classes/{target.id}/timetables", headers=hdr)
    assert resp.status_code == 403


# ── 3. Unassigned teacher: generic 404 on every class timetable endpoint ──────

async def test_unassigned_teacher_gets_generic_404_on_every_timetable_endpoint(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    teacher, token = await _make_user(tenant_a, raw_db, "teacher")
    hdr = _headers(token, tenant_a.slug)

    # The teacher teaches two subjects elsewhere (multi-row path), not in `target`.
    await _class_with_two_subjects(tenant_a, raw_db, teacher)
    target, math, _ = await _class_with_two_subjects(tenant_a, raw_db, None)
    await _publish_timetable(tenant_a, raw_db, target, math)

    for method, path, body in _timetable_endpoints(target.id, math.id):
        resp = await _call(client, method, path, hdr, body)
        assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}: {resp.text}"
        assert resp.json()["detail"] == {"code": "not_found", "message": "Resource not found."}

    # Class subjects listing is class-scoped too
    resp = await client.get(f"/api/v1/classes/{target.id}/subjects", headers=hdr)
    assert resp.status_code == 404
