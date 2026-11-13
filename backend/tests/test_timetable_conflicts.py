"""
tests/test_timetable_conflicts.py
─────────────────────────────────
Regression tests for the check-conflicts / publish conflict contract.

Scenario mirrors the dev data that crashed the Timetable Editor:
  - Class 10-A and Class 10-B, one teacher assigned in both.
  - 10-A is published with Math Mon 9-10, Physics Tue 10-11, Math Wed 11-12.
  - 10-B is published with the same teacher Wed 11:30-12:30.
  - 10-A has a draft with the same three lectures.
Expected: exactly one real conflict (Wednesday, teacher double-booked with 10-B).
The draft must never conflict with 10-A's own published timetable.
"""

import uuid
from datetime import time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, ClassSubject, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

# Every conflict item carries exactly these keys, always present.
CONFLICT_KEYS = {
    "conflict_type",
    "day_of_week",
    "day_name",
    "start_time",
    "end_time",
    "subject_name",
    "teacher_name",
    "room",
    "other_class_name",
    "other_subject_name",
    "other_start_time",
    "other_end_time",
    "description",
}

DRAFT_LECTURES = [
    # (subject key, day, start, end, room)
    ("math", 0, time(9, 0), time(10, 0), "Room 101"),
    ("phys", 1, time(10, 0), time(11, 0), "Lab 202"),
    ("math", 2, time(11, 0), time(12, 0), "Room 101"),
]


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


async def _build_dev_scenario(
    tenant: Tenant, raw_db: AsyncSession, *, publish_10b: bool = True
) -> dict[str, Any]:
    teacher = User(
        tenant_id=tenant.id,
        email=f"teacher-{uuid.uuid4().hex}@example.com",
        full_name="Ravi Teacher",
        is_admin=False,
        status="active",
    )
    raw_db.add(teacher)
    await raw_db.flush()
    role_id = (
        await raw_db.execute(select(Role.id).where(Role.tenant_id == tenant.id, Role.name == "teacher"))
    ).scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=teacher.id, role_id=role_id))

    suffix = uuid.uuid4().hex[:6]
    c_a = Class(tenant_id=tenant.id, name=f"Class 10-A {suffix}")
    c_b = Class(tenant_id=tenant.id, name=f"Class 10-B {suffix}")
    math = Subject(tenant_id=tenant.id, name=f"Mathematics {suffix}")
    phys = Subject(tenant_id=tenant.id, name=f"Physics {suffix}")
    raw_db.add_all([c_a, c_b, math, phys])
    await raw_db.flush()

    for cls, sub in ((c_a, math), (c_a, phys), (c_b, math)):
        raw_db.add(ClassSubject(tenant_id=tenant.id, class_id=cls.id, subject_id=sub.id))
        raw_db.add(
            TeacherAssignment(tenant_id=tenant.id, user_id=teacher.id, class_id=cls.id, subject_id=sub.id)
        )
    await raw_db.flush()

    subjects = {"math": math, "phys": phys}

    def add_entries(tt: Timetable, cls: Class, lectures: list[tuple[str, int, time, time, str]]) -> None:
        for key, dow, start, end, room in lectures:
            raw_db.add(
                TimetableEntry(
                    tenant_id=tenant.id,
                    timetable_id=tt.id,
                    class_id=cls.id,
                    subject_id=subjects[key].id,
                    teacher_id=teacher.id,
                    day_of_week=dow,
                    start_time=start,
                    end_time=end,
                    room=room,
                )
            )

    # 10-A published with the same three lectures as the draft.
    pub_a = Timetable(tenant_id=tenant.id, class_id=c_a.id, status="published", version=1)
    draft_a = Timetable(tenant_id=tenant.id, class_id=c_a.id, status="draft", version=2)
    raw_db.add_all([pub_a, draft_a])
    await raw_db.flush()
    add_entries(pub_a, c_a, DRAFT_LECTURES)
    add_entries(draft_a, c_a, DRAFT_LECTURES)

    if publish_10b:
        pub_b = Timetable(tenant_id=tenant.id, class_id=c_b.id, status="published", version=1)
        raw_db.add(pub_b)
        await raw_db.flush()
        # Same teacher Wed 11:30-12:30 overlaps the draft's Wed 11-12.
        add_entries(pub_b, c_b, [("math", 2, time(11, 30), time(12, 30), "Room 201")])

    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant.id), user_id=str(teacher.id), is_admin=False)
    return {
        "teacher": teacher,
        "class_a": c_a,
        "class_b": c_b,
        "math": math,
        "headers": _headers(token, tenant.slug),
    }


async def test_check_conflicts_dev_scenario_returns_typed_teacher_conflict(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _build_dev_scenario(tenant_a, raw_db)
    res = await client.post(
        f"/api/v1/classes/{s['class_a'].id}/timetables/check-conflicts", headers=s["headers"]
    )
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"has_conflicts", "conflicts"}
    assert body["has_conflicts"] is True
    # Only Wednesday clashes; Mon/Tue and 10-A's own published copy do not.
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert set(c) == CONFLICT_KEYS
    assert c == {
        "conflict_type": "teacher_overlap",
        "day_of_week": 2,
        "day_name": "Wednesday",
        "start_time": "11:00",
        "end_time": "12:00",
        "subject_name": s["math"].name,
        "teacher_name": "Ravi Teacher",
        "room": "Room 101",
        "other_class_name": s["class_b"].name,
        "other_subject_name": s["math"].name,
        "other_start_time": "11:30",
        "other_end_time": "12:30",
        "description": c["description"],
    }
    assert s["class_b"].name in c["description"]


async def test_draft_does_not_conflict_with_own_published_timetable(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    """The draft replaces the class's own published timetable, so identical lectures are not clashes."""
    s = await _build_dev_scenario(tenant_a, raw_db, publish_10b=False)
    res = await client.post(
        f"/api/v1/classes/{s['class_a'].id}/timetables/check-conflicts", headers=s["headers"]
    )
    assert res.status_code == 200
    assert res.json() == {"has_conflicts": False, "conflicts": []}

    # And publishing goes straight through without confirm_conflicts.
    pub = await client.post(
        f"/api/v1/classes/{s['class_a'].id}/timetables/publish",
        headers=s["headers"],
        json={"confirm_conflicts": False},
    )
    assert pub.status_code == 200
    assert pub.json()["status"] == "published"


async def test_publish_409_uses_same_conflict_shape_and_confirm_publishes(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _build_dev_scenario(tenant_a, raw_db)
    url = f"/api/v1/classes/{s['class_a'].id}/timetables/publish"

    blocked = await client.post(url, headers=s["headers"], json={"confirm_conflicts": False})
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["code"] == "timetable_conflicts_detected"
    assert len(detail["conflicts"]) == 1
    assert set(detail["conflicts"][0]) == CONFLICT_KEYS
    assert detail["conflicts"][0]["conflict_type"] == "teacher_overlap"

    ok = await client.post(url, headers=s["headers"], json={"confirm_conflicts": True})
    assert ok.status_code == 200
    assert ok.json()["status"] == "published"


async def test_room_and_intra_draft_conflicts_carry_full_details(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _build_dev_scenario(tenant_a, raw_db, publish_10b=False)
    c_a: Class = s["class_a"]
    c_b: Class = s["class_b"]

    # Another class published in Room 101 on Monday 09:30-10:30 with no teacher.
    pub_b = Timetable(tenant_id=tenant_a.id, class_id=c_b.id, status="published", version=1)
    raw_db.add(pub_b)
    await raw_db.flush()
    raw_db.add(
        TimetableEntry(
            tenant_id=tenant_a.id, timetable_id=pub_b.id, class_id=c_b.id, subject_id=s["math"].id,
            teacher_id=None, day_of_week=0, start_time=time(9, 30), end_time=time(10, 30), room="room 101",
        )
    )
    # Overlap inside the draft itself: a second Monday lecture 09:45-10:15 without teacher or room.
    draft_id = (
        await raw_db.execute(
            select(Timetable.id).where(
                Timetable.tenant_id == tenant_a.id, Timetable.class_id == c_a.id, Timetable.status == "draft"
            )
        )
    ).scalar_one()
    raw_db.add(
        TimetableEntry(
            tenant_id=tenant_a.id, timetable_id=draft_id, class_id=c_a.id, subject_id=s["math"].id,
            teacher_id=None, day_of_week=0, start_time=time(9, 45), end_time=time(10, 15), room=None,
        )
    )
    await raw_db.commit()

    res = await client.post(f"/api/v1/classes/{c_a.id}/timetables/check-conflicts", headers=s["headers"])
    assert res.status_code == 200
    conflicts = res.json()["conflicts"]
    for c in conflicts:
        assert set(c) == CONFLICT_KEYS

    room = [c for c in conflicts if c["conflict_type"] == "room_overlap"]
    assert len(room) == 1
    assert room[0]["room"] == "Room 101"
    assert room[0]["other_class_name"] == c_b.name
    assert room[0]["teacher_name"] == "Ravi Teacher"
    assert (room[0]["other_start_time"], room[0]["other_end_time"]) == ("09:30", "10:30")

    intra = [c for c in conflicts if c["conflict_type"] == "class_overlap"]
    assert len(intra) == 1
    assert intra[0]["other_class_name"] == c_a.name
    assert intra[0]["day_name"] == "Monday"
    # The 09:45 lecture also shares no room/teacher with others, so no extra teacher clash.
    assert not [c for c in conflicts if c["conflict_type"] == "teacher_overlap"]
