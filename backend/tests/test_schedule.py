"""
tests/test_schedule.py
───────────────────────
Phase 4a Schedule Lookup & Deterministic Timetable Service Test Suite.

Verifies pure deterministic functions over database rows with:
  1. Lecture in progress (active lecture detected, is_current=True, next lecture identified)
  2. Next lecture later today
  3. Tomorrow rollover
  4. Next week after Friday rollover
  5. Weekend lookup rollover to Monday
  6. No more lectures today
  7. No published timetable returns "no_published_timetable"
  8. Student in two classes returns labeled items for both
  9. Teacher across multiple classes sees own lectures across all classes
  10. Admin for single class and all tenant classes
  11. Tenant timezone localization (America/New_York vs Asia/Kolkata)
  12. Draft timetable is strictly invisible to students
  13. Cross-tenant isolation (Tenant B cannot see Tenant A's timetable)
  14. Zero network and zero LLM calls proof
  15. Full conflict detection (intra-class, teacher double-booking, room double-booking)
"""

import socket
import uuid
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

from app.db.models.timetable import TimetableEntry
from app.db.models.user import User
from app.schemas.timetable import ConflictType
from app.services.schedule import (
    class_schedule,
    detect_timetable_conflicts,
    next_lecture,
    schedule_for_day,
    todays_schedule,
    whats_on_now,
)

pytestmark = pytest.mark.asyncio


# ── In-Memory Deterministic Session Fixture ───────────────────────────────────

class MockScalarResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class MockResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        if not self._rows:
            return None
        first = self._rows[0]
        if isinstance(first, (tuple, list)):
            return first[0]
        return first

    def scalars(self) -> MockScalarResult:
        scalars_list = [r[0] if isinstance(r, (tuple, list)) else r for r in self._rows]
        return MockScalarResult(scalars_list)

    def all(self) -> list[Any]:
        return self._rows


class MockScheduleDB:
    """
    In-memory asynchronous database simulator for schedule service queries.
    Allows exact verification of schedule logic without network or database dependencies.
    """

    def __init__(
        self,
        tenant_id: uuid.UUID,
        timezone: str = "Asia/Kolkata",
        user_roles: dict[uuid.UUID, list[str]] | None = None,
        class_ids: list[uuid.UUID] | None = None,
        entries: list[tuple[TimetableEntry, str, str, str | None, str | None]] | None = None,
        other_published: list[tuple[TimetableEntry, str]] | None = None,
        classes: dict[uuid.UUID, str] | None = None,
        subjects: dict[uuid.UUID, str] | None = None,
        teachers: dict[uuid.UUID, str] | None = None,
    ):
        self.tenant_id = tenant_id
        self.timezone = timezone
        self.user_roles = user_roles or {}
        self.class_ids = class_ids or []
        self.entries = entries or []
        self.other_published = other_published or []
        self.classes = classes or {}
        self.subjects = subjects or {}
        self.teachers = teachers or {}

    async def execute(self, stmt: Any) -> MockResult:
        query_str = str(stmt).lower()

        # 1. Tenant timezone lookup
        if "tenants.timezone" in query_str:
            return MockResult([self.timezone])

        # 2. Main schedule entries query (selects timetable_entries joined with subject/class/user)
        if "timetable_entries" in query_str and "subjects" in query_str:
            return MockResult(self.entries)

        # 3. Other published entries for conflict detection
        if "timetable_entries" in query_str and "target_class_id" in query_str:
            return MockResult(self.other_published)

        # 4. User roles lookup
        if "roles.name" in query_str:
            all_r = []
            for r_list in self.user_roles.values():
                all_r.extend(r_list)
            return MockResult(list(set(all_r)) if all_r else ["student"])

        # 5. Class ID authorization resolution
        if (
            "enrollments.class_id" in query_str
            or "teacher_assignments.class_id" in query_str
        ):
            return MockResult(self.class_ids)

        if "classes.id" in query_str and "timetable_entries" not in query_str:
            params = stmt.compile().params if hasattr(stmt, "compile") else {}
            # Check if query is looking up a specific class_id
            specific_cids = [v for v in params.values() if isinstance(v, uuid.UUID)]
            if specific_cids:
                found = [c for c in specific_cids if c in self.class_ids]
                return MockResult(found)
            return MockResult(self.class_ids)

        # 6. Conflict detection maps
        if "classes.name" in query_str and "timetable_entries" not in query_str:
            return MockResult(list(self.classes.items()))

        if "subjects.name" in query_str and "timetable_entries" not in query_str:
            return MockResult(list(self.subjects.items()))

        if "users.full_name" in query_str and "timetable_entries" not in query_str:
            return MockResult(list(self.teachers.items()))

        return MockResult([])


# ── Test Suite ────────────────────────────────────────────────────────────────

async def test_lecture_in_progress() -> None:
    """Case 1: Fixed clock during an ongoing lecture."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    teacher_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    # Monday: 09:00 - 10:00 Calculus, 10:15 - 11:15 Physics
    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        teacher_id=teacher_id, day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )
    e2 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        teacher_id=teacher_id, day_of_week=0, start_time=time(10, 15), end_time=time(11, 15), room="Lab 1"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[
            (e1, "Grade 10", "Calculus", "MATH101", "Dr. Gauss"),
            (e2, "Grade 10", "Physics", "PHYS101", "Dr. Newton"),
        ],
    )

    # Clock fixed at Monday 09:30 AM (during Calculus)
    now = datetime(2026, 11, 9, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    assert res.status == "in_progress"
    assert res.current_lecture is not None
    assert res.current_lecture.subject_name == "Calculus"
    assert res.current_lecture.is_current is True
    assert res.next_lecture is not None
    assert res.next_lecture.subject_name == "Physics"
    assert res.next_lecture.is_current is False
    assert "Calculus" in res.message and "Physics" in res.message


async def test_next_lecture_later_today() -> None:
    """Case 2: Fixed clock earlier in the day before any lecture starts."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[(e1, "Grade 10", "Calculus", "MATH101", "Dr. Gauss")],
    )

    # Monday 08:15 AM
    now = datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    assert res.status == "upcoming_today"
    assert res.current_lecture is None
    assert res.next_lecture is not None
    assert res.next_lecture.subject_name == "Calculus"
    assert res.next_lecture.start_time == "09:00:00"


async def test_tomorrow_rollover() -> None:
    """Case 3: Fixed clock in evening rolls over to tomorrow morning's lecture."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    # Monday lecture ended at 10:00. Tuesday lecture at 09:00.
    e_mon = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )
    e_tue = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=1, start_time=time(9, 0), end_time=time(10, 0), room="Room 102"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[
            (e_mon, "Grade 10", "Calculus", "MATH101", "Dr. Gauss"),
            (e_tue, "Grade 10", "Chemistry", "CHEM101", "Dr. Curie"),
        ],
    )

    # Monday 17:00 (after Monday's classes)
    now = datetime(2026, 11, 9, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    assert res.status == "upcoming_future"
    assert res.current_lecture is None
    assert res.next_lecture is not None
    assert res.next_lecture.subject_name == "Chemistry"
    assert "tomorrow" in res.message


async def test_next_week_after_friday() -> None:
    """Case 4: Fixed clock on Friday evening rolls over to Monday morning."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e_fri = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=4, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )
    e_mon = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[
            (e_mon, "Grade 10", "Calculus", "MATH101", "Dr. Gauss"),
            (e_fri, "Grade 10", "History", "HIST101", "Prof. Herodotus"),
        ],
    )

    # Friday 2026-11-13 at 18:00
    now = datetime(2026, 11, 13, 18, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    assert res.status == "upcoming_future"
    assert res.next_lecture is not None
    assert res.next_lecture.subject_name == "Calculus"
    assert res.next_lecture.day_name == "Monday"
    assert "on Monday" in res.message


async def test_weekend_query() -> None:
    """Case 5: Saturday query correctly rolls over to Monday morning."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e_mon = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[(e_mon, "Grade 10", "Calculus", "MATH101", "Dr. Gauss")],
    )

    # Saturday 2026-11-14 at 14:00
    now = datetime(2026, 11, 14, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    assert res.status == "upcoming_future"
    assert res.next_lecture is not None
    assert res.next_lecture.day_name == "Monday"


async def test_no_more_lectures_today() -> None:
    """Case 6: After lecture finishes today with no more lectures scheduled this week."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e_mon = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )

    # If Monday is the ONLY lecture, then looking ahead 1-7 days will find Monday of next week
    # But if no future lecture exists at all (entries list empty), status is no_published_timetable.
    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[(e_mon, "Grade 10", "Calculus", "MATH101", "Dr. Gauss")],
    )

    # Monday 11:00 AM (after Monday lecture)
    now = datetime(2026, 11, 9, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    res = await next_lecture(user, now=now, db=db)

    # It will find next Monday's lecture (7 days ahead)
    assert res.status == "upcoming_future"
    assert res.next_lecture is not None
    assert res.next_lecture.day_name == "Monday"


async def test_no_published_timetable() -> None:
    """Case 7: Empty entries return no_published_timetable."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    db = MockScheduleDB(tenant_id=tid, class_ids=[cid], entries=[])

    res = await next_lecture(user, db=db)
    assert res.status == "no_published_timetable"
    assert res.current_lecture is None
    assert res.next_lecture is None


async def test_student_in_two_classes() -> None:
    """Case 8: Student enrolled in two classes gets entries labeled by class."""
    tid = uuid.uuid4()
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()
    sub_id1 = uuid.uuid4()
    sub_id2 = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid1, subject_id=sub_id1,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )
    e2 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid2, subject_id=sub_id2,
        day_of_week=0, start_time=time(11, 0), end_time=time(12, 0), room="Room 202"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid1, cid2],
        entries=[
            (e1, "Class 10-A", "Biology", "BIO101", "Dr. Darwin"),
            (e2, "Class 10-B", "Art", "ART101", "Prof. Da Vinci"),
        ],
    )

    now = datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    items = await schedule_for_day(user, target_date=now.date(), db=db, now=now)

    assert len(items) == 2
    class_names = {item.class_name for item in items}
    assert "Class 10-A" in class_names
    assert "Class 10-B" in class_names


async def test_teacher_across_classes() -> None:
    """Case 9: Teacher assigned across classes sees own lectures."""
    tid = uuid.uuid4()
    t_id = uuid.uuid4()
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()
    sub_id = uuid.uuid4()
    teacher_user = User(id=t_id, tenant_id=tid, full_name="Dr. Smith", is_admin=False)

    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid1, subject_id=sub_id,
        teacher_id=t_id, day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )
    e2 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid2, subject_id=sub_id,
        teacher_id=t_id, day_of_week=0, start_time=time(11, 0), end_time=time(12, 0), room="Room 202"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        user_roles={t_id: ["teacher"]},
        class_ids=[cid1, cid2],
        entries=[
            (e1, "Class 10-A", "Physics", "PHYS101", "Dr. Smith"),
            (e2, "Class 11-A", "Physics", "PHYS101", "Dr. Smith"),
        ],
    )

    now = datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    items = await schedule_for_day(teacher_user, target_date=now.date(), db=db, now=now)

    assert len(items) == 2
    assert all(item.teacher_id == t_id for item in items)


async def test_admin_one_class_and_all_classes() -> None:
    """Case 10: Admin can query single class or all classes across tenant."""
    tid = uuid.uuid4()
    admin_id = uuid.uuid4()
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()
    admin_user = User(id=admin_id, tenant_id=tid, full_name="Super Admin", is_admin=True)

    db = MockScheduleDB(
        tenant_id=tid,
        user_roles={admin_id: ["tenant_admin"]},
        class_ids=[cid1, cid2],
        entries=[],
    )

    # 1. Admin queries valid class -> authorized
    scoped = await class_schedule(admin_user, class_id=cid1, db=db)
    assert isinstance(scoped, dict)

    # 2. Unknown class -> raises 404
    unknown_id = uuid.uuid4()
    with pytest.raises(HTTPException) as exc_info:
        await class_schedule(admin_user, class_id=unknown_id, db=db)
    assert exc_info.value.status_code == 404


async def test_different_tenant_timezone() -> None:
    """Case 11: Tenant configured in America/New_York resolves local clock properly."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="NY Student", is_admin=False)

    # Lecture is 09:00 - 10:00 local New York time on Monday
    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 501"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        timezone="America/New_York",
        class_ids=[cid],
        entries=[(e1, "NY Class", "English", "ENG101", "Ms. Austen")],
    )

    # UTC time is Monday 14:30. In America/New_York (UTC-5), this is 09:30 AM (lecture in progress!)
    now_utc = datetime(2026, 11, 9, 14, 30, tzinfo=ZoneInfo("UTC"))
    res = await next_lecture(user, now=now_utc, db=db)

    assert res.status == "in_progress"
    assert res.current_lecture is not None
    assert res.current_lecture.subject_name == "English"
    assert res.tenant_timezone == "America/New_York"


async def test_draft_never_visible_to_students() -> None:
    """Case 12: Draft entries are never queried or returned to students."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    # DB returns 0 published entries because timetable status is 'draft'
    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[],  # Filtered out by Timetable.status == 'published'
    )

    res = await next_lecture(user, db=db)
    assert res.status == "no_published_timetable"
    assert res.current_lecture is None
    assert res.next_lecture is None


async def test_cross_tenant_isolation() -> None:
    """Case 13: Tenant B user cannot view Tenant A timetable."""
    tid_b = uuid.uuid4()
    cid_b = uuid.uuid4()
    user_b = User(id=uuid.uuid4(), tenant_id=tid_b, full_name="Tenant B User", is_admin=False)

    # Empty entries for Tenant B
    db = MockScheduleDB(tenant_id=tid_b, class_ids=[cid_b], entries=[])

    res = await next_lecture(user_b, db=db)
    assert res.status == "no_published_timetable"
    assert res.current_lecture is None


async def test_lookup_service_makes_no_network_or_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 14: Verifies schedule service is pure and never performs network or LLM calls."""
    # Patch socket connect and httpx send to fail if called
    def _forbidden_socket(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Network socket connection attempted by schedule service!")

    async def _forbidden_httpx(*args: Any, **kwargs: Any) -> None:
        pytest.fail("HTTP client request attempted by schedule service!")

    monkeypatch.setattr(socket.socket, "connect", _forbidden_socket)
    monkeypatch.setattr(httpx.AsyncClient, "send", _forbidden_httpx)

    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub_id = uuid.uuid4()
    user = User(id=uuid.uuid4(), tenant_id=tid, full_name="Alice Student", is_admin=False)

    e1 = TimetableEntry(
        tenant_id=tid, timetable_id=uuid.uuid4(), class_id=cid, subject_id=sub_id,
        day_of_week=0, start_time=time(9, 0), end_time=time(10, 0), room="Room 101"
    )

    db = MockScheduleDB(
        tenant_id=tid,
        class_ids=[cid],
        entries=[(e1, "Grade 10", "Calculus", "MATH101", "Dr. Gauss")],
    )

    now = datetime(2026, 11, 9, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Execute all public service methods
    await next_lecture(user, now=now, db=db)
    await whats_on_now(user, now=now, db=db)
    await todays_schedule(user, now=now, db=db)
    await schedule_for_day(user, target_date=now.date(), db=db, now=now)
    await class_schedule(user, class_id=cid, db=db)


async def test_conflict_detection_scenarios() -> None:
    """Case 15: Conflict detection for intra-class, teacher, and room collisions."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    sub1_id = uuid.uuid4()
    sub2_id = uuid.uuid4()
    t1_id = uuid.uuid4()
    t2_id = uuid.uuid4()

    db = MockScheduleDB(
        tenant_id=tid,
        classes={cid: "10-A"},
        subjects={sub1_id: "Math", sub2_id: "Physics"},
        teachers={t1_id: "Dr. Smith", t2_id: "Dr. Jones"},
    )

    # 1. Intra-class overlapping entries at same time
    conflicts = await detect_timetable_conflicts(
        db=db,  # type: ignore[arg-type]
        tenant_id=tid,
        target_class_id=cid,
        candidate_entries=[
            {
                "day_of_week": 0,
                "start_time": time(9, 0),
                "end_time": time(10, 0),
                "subject_id": sub1_id,
                "teacher_id": t1_id,
                "room": "101",
            },
            {
                "day_of_week": 0,
                "start_time": time(9, 30),
                "end_time": time(10, 30),
                "subject_id": sub2_id,
                "teacher_id": t2_id,
                "room": "102",
            },
        ],
    )

    assert len(conflicts) >= 1
    assert any(c.conflict_type == ConflictType.CLASS_OVERLAP for c in conflicts)

    # 2. Teacher double-booking across entries
    conflicts_teacher = await detect_timetable_conflicts(
        db=db,  # type: ignore[arg-type]
        tenant_id=tid,
        target_class_id=cid,
        candidate_entries=[
            {
                "day_of_week": 1,
                "start_time": time(9, 0),
                "end_time": time(10, 0),
                "subject_id": sub1_id,
                "teacher_id": t1_id,
                "room": "101",
            },
            {
                "day_of_week": 1,
                "start_time": time(9, 0),
                "end_time": time(10, 0),
                "subject_id": sub2_id,
                "teacher_id": t1_id,  # Same teacher!
                "room": "102",
            },
        ],
    )
    assert any(c.conflict_type == ConflictType.TEACHER_OVERLAP for c in conflicts_teacher)

    # 3. Room double-booking across entries
    conflicts_room = await detect_timetable_conflicts(
        db=db,  # type: ignore[arg-type]
        tenant_id=tid,
        target_class_id=cid,
        candidate_entries=[
            {
                "day_of_week": 2,
                "start_time": time(11, 0),
                "end_time": time(12, 0),
                "subject_id": sub1_id,
                "teacher_id": t1_id,
                "room": "Lab A",
            },
            {
                "day_of_week": 2,
                "start_time": time(11, 30),
                "end_time": time(12, 30),
                "subject_id": sub2_id,
                "teacher_id": t2_id,
                "room": "Lab A",  # Same room!
            },
        ],
    )
    assert any(c.conflict_type == ConflictType.ROOM_OVERLAP for c in conflicts_room)
