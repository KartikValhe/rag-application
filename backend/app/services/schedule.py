"""
app/services/schedule.py
────────────────────────
Phase 4 Deterministic Schedule Lookup & Conflict Detection Service.

Pure functions over database rows. Zero LLM calls, zero network calls.
The clock is injectable (via `now: datetime | None = None`) to guarantee deterministic testing.
Tenant timezone is strictly respected (defaults to 'Asia/Kolkata').
Role authorization scope is enforced inside the service before results are assembled:
  - Student: only classes they are currently enrolled in.
  - Teacher: own lectures across all classes + classes they are assigned to.
  - Admin: any specified class or all tenant classes.
Unknown/unauthorized class queries return a generic 404 (prevent ID probing).
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.db.models.academic import Class, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from app.schemas.timetable import (
    DAY_NAMES,
    ConflictType,
    NextLectureResponse,
    ScheduleItem,
    TimetableConflict,
)


def get_tenant_zoneinfo(tz_name: str | None) -> ZoneInfo:
    """Safely resolve ZoneInfo for tenant timezone with Asia/Kolkata fallback."""
    if not tz_name or not tz_name.strip():
        return ZoneInfo("Asia/Kolkata")
    try:
        return ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, ValueError, Exception):
        return ZoneInfo("Asia/Kolkata")


def resolve_reference_time(now: datetime | None, tz: ZoneInfo) -> datetime:
    """Normalize datetime into the tenant's localized timezone."""
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _times_overlap(s1: time, e1: time, s2: time, e2: time) -> bool:
    """Check if two time intervals [s1, e1) and [s2, e2) overlap."""
    return s1 < e2 and s2 < e1


async def get_tenant_timezone(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Retrieve the configured timezone string for a tenant."""
    res = await db.execute(select(Tenant.timezone).where(Tenant.id == tenant_id))
    tz_val = res.scalar_one_or_none()
    return tz_val or "Asia/Kolkata"


async def get_user_role_names(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> set[str]:
    """Fetch assigned system and custom role names for a tenant user."""
    res = await db.execute(
        select(Role.name)
        .join(UserRole, (UserRole.tenant_id == Role.tenant_id) & (UserRole.role_id == Role.id))
        .where(UserRole.tenant_id == tenant_id, UserRole.user_id == user_id)
    )
    return set(res.scalars().all())


async def resolve_authorized_class_ids(
    db: AsyncSession,
    user: User,
    class_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """
    Determine the authorized class IDs based on the caller's role.
    Raises generic 404 if a requested class_id is outside the caller's authorized scope.
    """
    roles = await get_user_role_names(db, user.tenant_id, user.id)
    is_admin_user = user.is_admin or ("tenant_admin" in roles)

    # 1. Admin: Can query any specific class or all classes in the tenant
    if is_admin_user:
        if class_id is not None:
            c_res = await db.execute(
                select(Class.id).where(Class.tenant_id == user.tenant_id, Class.id == class_id)
            )
            if c_res.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
            return [class_id]

        all_c_res = await db.execute(
            select(Class.id).where(Class.tenant_id == user.tenant_id).order_by(Class.name)
        )
        return list(all_c_res.scalars().all())

    # 2. Teacher: Classes assigned to teach
    if "teacher" in roles:
        ta_res = await db.execute(
            select(TeacherAssignment.class_id)
            .where(TeacherAssignment.tenant_id == user.tenant_id, TeacherAssignment.user_id == user.id)
            .distinct()
        )
        assigned_class_ids = set(ta_res.scalars().all())

        if class_id is not None:
            if class_id not in assigned_class_ids:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
            return [class_id]

        return list(assigned_class_ids)

    # 3. Student / Member: Classes enrolled in
    enr_res = await db.execute(
        select(Enrollment.class_id)
        .where(Enrollment.tenant_id == user.tenant_id, Enrollment.user_id == user.id)
        .distinct()
    )
    enrolled_class_ids = set(enr_res.scalars().all())

    if class_id is not None:
        if class_id not in enrolled_class_ids:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
        return [class_id]

    return list(enrolled_class_ids)


async def detect_timetable_conflicts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    target_class_id: uuid.UUID,
    candidate_entries: list[dict[str, Any]],
    exclude_timetable_id: uuid.UUID | None = None,
) -> list[TimetableConflict]:
    """
    Conflict detection across the entire tenant:
      1. Class overlap: Two overlapping entries in the same draft.
      2. Teacher overlap: Same teacher scheduled in multiple places at the same time.
      3. Room overlap: Same room booked for multiple sessions at the same time.
    Compares candidate entries among themselves and against the PUBLISHED timetables of
    OTHER classes. The class's own published timetable is ignored: the draft replaces it.
    """
    conflicts: list[TimetableConflict] = []

    # Load context dictionaries for names
    classes_res = await db.execute(select(Class.id, Class.name).where(Class.tenant_id == tenant_id))
    class_name_map: dict[uuid.UUID, str] = dict(classes_res.all())

    subjects_res = await db.execute(select(Subject.id, Subject.name).where(Subject.tenant_id == tenant_id))
    subject_name_map: dict[uuid.UUID, str] = dict(subjects_res.all())

    teachers_res = await db.execute(select(User.id, User.full_name).where(User.tenant_id == tenant_id))
    teacher_name_map: dict[uuid.UUID, str] = dict(teachers_res.all())

    own_class_name = class_name_map.get(target_class_id, "This class")
    lectures = [_to_lecture(c) for c in candidate_entries]

    def make(
        kind: ConflictType,
        lec: _Lecture,
        other: _Lecture,
        other_class_name: str,
        description: str,
    ) -> TimetableConflict:
        return TimetableConflict(
            conflict_type=kind,
            day_of_week=lec.day_of_week,
            day_name=DAY_NAMES.get(lec.day_of_week, ""),
            start_time=_hhmm(lec.start),
            end_time=_hhmm(lec.end),
            subject_name=_subject_name(subject_name_map, lec.subject_id),
            teacher_name=teacher_name_map.get(lec.teacher_id) if lec.teacher_id else None,
            room=lec.room,
            other_class_name=other_class_name,
            other_subject_name=_subject_name(subject_name_map, other.subject_id),
            other_start_time=_hhmm(other.start),
            other_end_time=_hhmm(other.end),
            description=description,
        )

    # 1. Overlaps inside the draft itself
    for i, a in enumerate(lectures):
        for b in lectures[i + 1:]:
            if a.day_of_week != b.day_of_week or not _times_overlap(a.start, a.end, b.start, b.end):
                continue
            day = DAY_NAMES.get(a.day_of_week, "")
            when = f"{day} {_hhmm(a.start)}-{_hhmm(a.end)} and {_hhmm(b.start)}-{_hhmm(b.end)}"
            conflicts.append(
                make(
                    ConflictType.CLASS_OVERLAP, a, b, own_class_name,
                    f"Class '{own_class_name}' has overlapping lectures on {when}.",
                )
            )
            if a.teacher_id and a.teacher_id == b.teacher_id:
                t_name = teacher_name_map.get(a.teacher_id, "Teacher")
                conflicts.append(
                    make(
                        ConflictType.TEACHER_OVERLAP, a, b, own_class_name,
                        f"Teacher '{t_name}' is double-booked in '{own_class_name}' on {when}.",
                    )
                )
            if a.room and b.room and a.room.lower() == b.room.lower():
                conflicts.append(
                    make(
                        ConflictType.ROOM_OVERLAP, a, b, own_class_name,
                        f"Room '{a.room}' is double-booked in '{own_class_name}' on {when}.",
                    )
                )

    # 2. Inter-timetable checks against other classes' published timetables
    pub_query = (
        select(TimetableEntry, Class.name.label("class_name"))
        .join(Timetable, (Timetable.tenant_id == TimetableEntry.tenant_id) & (Timetable.id == TimetableEntry.timetable_id))
        .join(Class, (Class.tenant_id == TimetableEntry.tenant_id) & (Class.id == TimetableEntry.class_id))
        .where(
            TimetableEntry.tenant_id == tenant_id,
            Timetable.status == "published",
            Timetable.class_id != target_class_id,
        )
    )
    if exclude_timetable_id:
        pub_query = pub_query.where(Timetable.id != exclude_timetable_id)

    pub_res = await db.execute(pub_query)
    other_published_entries = pub_res.all()

    for lec in lectures:
        for pub_entry, other_class_name in other_published_entries:
            other = _Lecture(
                day_of_week=pub_entry.day_of_week,
                start=pub_entry.start_time,
                end=pub_entry.end_time,
                subject_id=pub_entry.subject_id,
                teacher_id=pub_entry.teacher_id,
                room=(pub_entry.room or "").strip() or None,
            )
            if other.day_of_week != lec.day_of_week or not _times_overlap(
                lec.start, lec.end, other.start, other.end
            ):
                continue
            day = DAY_NAMES.get(lec.day_of_week, "")
            other_when = f"{day} {_hhmm(other.start)}-{_hhmm(other.end)}"

            if lec.teacher_id and lec.teacher_id == other.teacher_id:
                t_name = teacher_name_map.get(lec.teacher_id, "Teacher")
                conflicts.append(
                    make(
                        ConflictType.TEACHER_OVERLAP, lec, other, other_class_name,
                        f"Teacher '{t_name}' is already scheduled in '{other_class_name}' on {other_when}.",
                    )
                )
            if lec.room and other.room and lec.room.lower() == other.room.lower():
                conflicts.append(
                    make(
                        ConflictType.ROOM_OVERLAP, lec, other, other_class_name,
                        f"Room '{lec.room}' is already booked by '{other_class_name}' on {other_when}.",
                    )
                )

    return conflicts


class _Lecture(NamedTuple):
    """A lecture normalised for conflict checks."""
    day_of_week: int
    start: time
    end: time
    subject_id: uuid.UUID | None
    teacher_id: uuid.UUID | None
    room: str | None


def _as_time(v: Any) -> time:
    return v if isinstance(v, time) else time.fromisoformat(str(v))


def _as_uuid(v: Any) -> uuid.UUID | None:
    return uuid.UUID(str(v)) if v else None


def _to_lecture(raw: dict[str, Any]) -> _Lecture:
    return _Lecture(
        day_of_week=int(raw["day_of_week"]),
        start=_as_time(raw["start_time"]),
        end=_as_time(raw["end_time"]),
        subject_id=_as_uuid(raw.get("subject_id")),
        teacher_id=_as_uuid(raw.get("teacher_id")),
        room=str(raw.get("room") or "").strip() or None,
    )


def _hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def _subject_name(names: dict[uuid.UUID, str], subject_id: uuid.UUID | None) -> str:
    return names.get(subject_id, "Unknown subject") if subject_id else "Unknown subject"


async def _load_published_schedule_entries(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    class_ids: list[uuid.UUID],
    teacher_id: uuid.UUID | None = None,
) -> list[tuple[TimetableEntry, str, str, str | None, str | None]]:
    """
    Load published timetable entries joined with Class, Subject, and User names.
    """
    if not class_ids and not teacher_id:
        return []

    query = (
        select(
            TimetableEntry,
            Class.name.label("class_name"),
            Subject.name.label("subject_name"),
            Subject.code.label("subject_code"),
            User.full_name.label("teacher_name"),
        )
        .join(Timetable, (Timetable.tenant_id == TimetableEntry.tenant_id) & (Timetable.id == TimetableEntry.timetable_id))
        .join(Class, (Class.tenant_id == TimetableEntry.tenant_id) & (Class.id == TimetableEntry.class_id))
        .join(Subject, (Subject.tenant_id == TimetableEntry.tenant_id) & (Subject.id == TimetableEntry.subject_id))
        .outerjoin(User, (User.tenant_id == TimetableEntry.tenant_id) & (User.id == TimetableEntry.teacher_id))
        .where(
            TimetableEntry.tenant_id == tenant_id,
            Timetable.status == "published",
        )
    )

    if class_ids and teacher_id:
        query = query.where(
            (TimetableEntry.class_id.in_(class_ids)) | (TimetableEntry.teacher_id == teacher_id)
        )
    elif class_ids:
        query = query.where(TimetableEntry.class_id.in_(class_ids))
    elif teacher_id:
        query = query.where(TimetableEntry.teacher_id == teacher_id)

    query = query.order_by(TimetableEntry.day_of_week, TimetableEntry.start_time)
    res = await db.execute(query)
    return res.all()  # type: ignore[return-value]


def _build_schedule_item(
    entry: TimetableEntry,
    class_name: str,
    subject_name: str,
    subject_code: str | None,
    teacher_name: str | None,
    lecture_date: date,
    is_current: bool = False,
) -> ScheduleItem:
    return ScheduleItem(
        entry_id=entry.id,
        class_id=entry.class_id,
        class_name=class_name,
        subject_id=entry.subject_id,
        subject_name=subject_name,
        subject_code=subject_code,
        teacher_id=entry.teacher_id,
        teacher_name=teacher_name,
        room=entry.room,
        day_of_week=entry.day_of_week,
        day_name=DAY_NAMES.get(entry.day_of_week, ""),
        start_time=entry.start_time.isoformat(),
        end_time=entry.end_time.isoformat(),
        date=lecture_date.isoformat(),
        is_current=is_current,
        note=entry.note,
    )


# ── Public Deterministic Schedule Lookup Functions ────────────────────────────

async def whats_on_now(
    user: User,
    now: datetime | None = None,
    class_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> list[ScheduleItem]:
    """
    Returns active lectures in progress right now.
    """
    if db is None:
        raise ValueError("db session required")

    tz_str = await get_tenant_timezone(db, user.tenant_id)
    tz = get_tenant_zoneinfo(tz_str)
    ref_time = resolve_reference_time(now, tz)
    curr_weekday = ref_time.weekday()
    curr_time = ref_time.time()
    curr_date = ref_time.date()

    authorized_class_ids = await resolve_authorized_class_ids(db, user, class_id)
    roles = await get_user_role_names(db, user.tenant_id, user.id)
    teacher_filter = user.id if "teacher" in roles and class_id is None else None

    entries = await _load_published_schedule_entries(
        db, user.tenant_id, authorized_class_ids, teacher_filter
    )

    results: list[ScheduleItem] = []
    for entry, c_name, s_name, s_code, t_name in entries:
        if entry.day_of_week == curr_weekday and entry.start_time <= curr_time < entry.end_time:
            results.append(
                _build_schedule_item(entry, c_name, s_name, s_code, t_name, curr_date, is_current=True)
            )

    return results


async def todays_schedule(
    user: User,
    now: datetime | None = None,
    class_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> list[ScheduleItem]:
    """
    Returns all published lectures scheduled for today.
    """
    if db is None:
        raise ValueError("db session required")

    tz_str = await get_tenant_timezone(db, user.tenant_id)
    tz = get_tenant_zoneinfo(tz_str)
    ref_time = resolve_reference_time(now, tz)
    return await schedule_for_day(user, ref_time.date(), class_id=class_id, db=db, now=ref_time)


async def schedule_for_day(
    user: User,
    target_date: date | datetime,
    class_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    now: datetime | None = None,
) -> list[ScheduleItem]:
    """
    Returns published lectures for a target date.
    """
    if db is None:
        raise ValueError("db session required")

    tz_str = await get_tenant_timezone(db, user.tenant_id)
    tz = get_tenant_zoneinfo(tz_str)
    ref_time = resolve_reference_time(now, tz)

    cal_date = target_date.date() if isinstance(target_date, datetime) else target_date
    target_weekday = cal_date.weekday()

    authorized_class_ids = await resolve_authorized_class_ids(db, user, class_id)
    roles = await get_user_role_names(db, user.tenant_id, user.id)
    teacher_filter = user.id if "teacher" in roles and class_id is None else None

    entries = await _load_published_schedule_entries(
        db, user.tenant_id, authorized_class_ids, teacher_filter
    )

    results: list[ScheduleItem] = []
    for entry, c_name, s_name, s_code, t_name in entries:
        if entry.day_of_week == target_weekday:
            is_curr = (
                cal_date == ref_time.date()
                and entry.start_time <= ref_time.time() < entry.end_time
            )
            results.append(
                _build_schedule_item(entry, c_name, s_name, s_code, t_name, cal_date, is_current=is_curr)
            )

    return results


async def class_schedule(
    user: User,
    class_id: uuid.UUID,
    db: AsyncSession | None = None,
) -> dict[int, list[ScheduleItem]]:
    """
    Returns the full weekly published schedule for an authorized class, grouped by weekday 0-6.
    """
    if db is None:
        raise ValueError("db session required")

    authorized_class_ids = await resolve_authorized_class_ids(db, user, class_id)
    if class_id not in authorized_class_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    entries = await _load_published_schedule_entries(db, user.tenant_id, [class_id])
    grouped: dict[int, list[ScheduleItem]] = {d: [] for d in range(7)}

    ref_date = date.today()
    for entry, c_name, s_name, s_code, t_name in entries:
        item = _build_schedule_item(entry, c_name, s_name, s_code, t_name, ref_date)
        grouped[entry.day_of_week].append(item)

    return grouped


async def next_lecture(
    user: User,
    now: datetime | None = None,
    class_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> NextLectureResponse:
    """
    Deterministic schedule lookup answering 'What is my next lecture?':
      - Detects lectures currently in progress.
      - Finds next lecture later today.
      - Rolls over to tomorrow, the weekend, or next week.
      - Handles no more lectures today, no lectures scheduled, or no published timetable.
    """
    if db is None:
        raise ValueError("db session required")

    tz_str = await get_tenant_timezone(db, user.tenant_id)
    tz = get_tenant_zoneinfo(tz_str)
    ref_time = resolve_reference_time(now, tz)
    curr_weekday = ref_time.weekday()
    curr_time = ref_time.time()
    curr_date = ref_time.date()

    authorized_class_ids = await resolve_authorized_class_ids(db, user, class_id)

    # Check if any published timetable exists for authorized classes
    roles = await get_user_role_names(db, user.tenant_id, user.id)
    teacher_filter = user.id if "teacher" in roles and class_id is None else None

    entries = await _load_published_schedule_entries(
        db, user.tenant_id, authorized_class_ids, teacher_filter
    )

    if not entries:
        return NextLectureResponse(
            status="no_published_timetable",
            message="No timetable has been published for your class yet.",
            current_lecture=None,
            next_lecture=None,
            tenant_timezone=tz_str,
        )

    # 1. Check for lecture currently in progress
    current_lecture: ScheduleItem | None = None
    for entry, c_name, s_name, s_code, t_name in entries:
        if entry.day_of_week == curr_weekday and entry.start_time <= curr_time < entry.end_time:
            current_lecture = _build_schedule_item(
                entry, c_name, s_name, s_code, t_name, curr_date, is_current=True
            )
            break

    # 2. Check for next lecture later today
    next_lecture_item: ScheduleItem | None = None
    for entry, c_name, s_name, s_code, t_name in entries:
        if entry.day_of_week == curr_weekday and entry.start_time > curr_time:
            next_lecture_item = _build_schedule_item(
                entry, c_name, s_name, s_code, t_name, curr_date, is_current=False
            )
            break

    if next_lecture_item:
        stat = "in_progress" if current_lecture else "upcoming_today"
        msg = (
            f"You have '{current_lecture.subject_name}' in progress. Next is '{next_lecture_item.subject_name}' at {next_lecture_item.start_time}."
            if current_lecture
            else f"Your next lecture today is '{next_lecture_item.subject_name}' at {next_lecture_item.start_time}."
        )
        return NextLectureResponse(
            status=stat,
            message=msg,
            current_lecture=current_lecture,
            next_lecture=next_lecture_item,
            tenant_timezone=tz_str,
        )

    # 3. Look forward across upcoming days (1 to 7 days ahead)
    for offset in range(1, 8):
        future_date = curr_date + timedelta(days=offset)
        future_weekday = future_date.weekday()

        day_entries = [e for e in entries if e[0].day_of_week == future_weekday]
        if day_entries:
            # Sort by start time and pick the earliest
            day_entries.sort(key=lambda x: x[0].start_time)
            first_entry, c_name, s_name, s_code, t_name = day_entries[0]
            next_lecture_item = _build_schedule_item(
                first_entry, c_name, s_name, s_code, t_name, future_date, is_current=False
            )
            break

    if next_lecture_item:
        day_str = (
            "tomorrow"
            if next_lecture_item.date == (curr_date + timedelta(days=1)).isoformat()
            else f"on {next_lecture_item.day_name} ({next_lecture_item.date})"
        )
        stat = "in_progress" if current_lecture else "upcoming_future"
        msg = (
            f"Current lecture is '{current_lecture.subject_name}'. Next lecture is '{next_lecture_item.subject_name}' {day_str} at {next_lecture_item.start_time}."
            if current_lecture
            else f"No more lectures today. Your next lecture is '{next_lecture_item.subject_name}' {day_str} at {next_lecture_item.start_time}."
        )
        return NextLectureResponse(
            status=stat,
            message=msg,
            current_lecture=current_lecture,
            next_lecture=next_lecture_item,
            tenant_timezone=tz_str,
        )

    # 4. No future lectures scheduled in published timetable
    return NextLectureResponse(
        status="no_more_today" if current_lecture else "no_lectures_scheduled",
        message="No upcoming lectures scheduled.",
        current_lecture=current_lecture,
        next_lecture=None,
        tenant_timezone=tz_str,
    )
