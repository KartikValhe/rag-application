"""
app/api/v1/timetable.py
───────────────────────
Phase 4 Timetable Endpoints:
  - User schedule lookups:
      GET /me/next-lecture
      GET /me/timetable
  - Class timetable management (Admin / Scoped Teacher):
      GET    /classes/{class_id}/timetables
      GET    /classes/{class_id}/timetables/draft
      POST   /classes/{class_id}/timetables/draft
      PUT    /classes/{class_id}/timetables/draft
      POST   /classes/{class_id}/timetables/draft/entries
      PUT    /classes/{class_id}/timetables/draft/entries/{entry_id}
      DELETE /classes/{class_id}/timetables/draft/entries/{entry_id}
      POST   /classes/{class_id}/timetables/check-conflicts
      POST   /classes/{class_id}/timetables/publish
      POST   /classes/{class_id}/timetables/unpublish

Strict RBAC and class/subject scoping enforced via require("timetable:publish", scope=Scope(ScopeType.CLASS)).
"""

import datetime
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import delete, desc, func, select

from app.api.deps import TenantDB
from app.core.authz import GENERIC_NOT_FOUND, Scope, ScopeType, require
from app.core.upload_security import (
    sanitize_filename,
    sniff_and_validate_file_type,
    stream_upload_with_limit,
)
from app.db.models.academic import Class, ClassSubject, Subject, TeacherAssignment
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from app.schemas.timetable import (
    DAY_NAMES,
    AvailableSubjectItem,
    AvailableTeacherItem,
    ConflictCheckResponse,
    ImportPreviewResponse,
    MyTimetableResponse,
    NextLectureResponse,
    ParsedPreviewRow,
    PublishTimetableRequest,
    SaveImportedDraftRequest,
    TimetableCreate,
    TimetableEntryCreate,
    TimetableEntryResponse,
    TimetableEntryUpdate,
    TimetableResponse,
    TimetableUpdate,
)
from app.services.audit import record_audit_event
from app.services.schedule import (
    detect_timetable_conflicts,
    get_tenant_timezone,
    next_lecture,
    resolve_authorized_class_ids,
)
from app.services.timetable_importer import (
    generate_csv_template,
    generate_xlsx_template,
    match_and_validate_records,
)
from app.services.timetable_parser_runner import (
    check_and_record_rate_limit,
    run_killable_parse,
)

router = APIRouter(tags=["Timetables"])


MAX_TIMETABLE_ENTRIES = 200


async def _validate_entry_relations(
    db: TenantDB,
    tenant_id: uuid.UUID,
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    teacher_id: uuid.UUID | None,
) -> None:
    """Validate that subject is linked to class and teacher is assigned to that class+subject."""
    cs_res = await db.execute(
        select(ClassSubject.class_id).where(
            ClassSubject.tenant_id == tenant_id,
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
        )
    )
    if cs_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "subject_not_linked", "message": "Subject is not linked to this class."},
        )

    if teacher_id is not None:
        ta_res = await db.execute(
            select(TeacherAssignment.id).where(
                TeacherAssignment.tenant_id == tenant_id,
                TeacherAssignment.class_id == class_id,
                TeacherAssignment.subject_id == subject_id,
                TeacherAssignment.user_id == teacher_id,
            )
        )
        if ta_res.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "teacher_not_assigned", "message": "Teacher is not assigned to this class and subject."},
            )


async def _serialize_timetable(db: TenantDB, timetable: Timetable) -> TimetableResponse:
    """Helper to load and serialize a timetable with joined subject and teacher details."""
    await db.refresh(timetable)
    entries_query = (
        select(
            TimetableEntry,
            Subject.name.label("subject_name"),
            Subject.code.label("subject_code"),
            User.full_name.label("teacher_name"),
        )
        .join(Subject, (Subject.tenant_id == TimetableEntry.tenant_id) & (Subject.id == TimetableEntry.subject_id))
        .outerjoin(User, (User.tenant_id == TimetableEntry.tenant_id) & (User.id == TimetableEntry.teacher_id))
        .where(
            TimetableEntry.tenant_id == timetable.tenant_id,
            TimetableEntry.timetable_id == timetable.id,
        )
        .order_by(TimetableEntry.day_of_week, TimetableEntry.start_time)
    )
    res = await db.execute(entries_query)
    rows = res.all()

    class_res = await db.execute(
        select(Class.name).where(Class.tenant_id == timetable.tenant_id, Class.id == timetable.class_id)
    )
    class_name = class_res.scalar_one_or_none()

    entry_responses = [
        TimetableEntryResponse(
            id=entry.id,
            timetable_id=entry.timetable_id,
            class_id=entry.class_id,
            class_name=class_name,
            subject_id=entry.subject_id,
            subject_name=s_name,
            subject_code=s_code,
            teacher_id=entry.teacher_id,
            teacher_name=t_name,
            day_of_week=entry.day_of_week,
            day_name=DAY_NAMES.get(entry.day_of_week, ""),
            start_time=entry.start_time.isoformat(),
            end_time=entry.end_time.isoformat(),
            room=entry.room,
            note=entry.note,
            created_at=entry.created_at,
        )
        for entry, s_name, s_code, t_name in rows
    ]

    return TimetableResponse(
        id=timetable.id,
        tenant_id=timetable.tenant_id,
        class_id=timetable.class_id,
        class_name=class_name,
        academic_year=timetable.academic_year,
        term=timetable.term,
        status=timetable.status,
        version=timetable.version,
        entries=entry_responses,
        created_at=timetable.created_at,
        updated_at=timetable.updated_at,
    )


# ── User Schedule Endpoints (/me) ─────────────────────────────────────────────

@router.get("/me/next-lecture", response_model=NextLectureResponse)
async def get_my_next_lecture(
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:view"))],
    class_id: uuid.UUID | None = Query(None, description="Optional class filter"),
) -> NextLectureResponse:
    """
    Deterministic schedule lookup: answers 'What is my next lecture?'
    using published timetables and caller's academic enrollment/assignment boundaries.
    """
    return await next_lecture(
        user=current_user,
        now=None,
        class_id=class_id,
        db=db,
    )


@router.get("/me/timetable", response_model=MyTimetableResponse)
async def get_my_timetable(
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:view"))],
    class_id: uuid.UUID | None = Query(None, description="Optional class filter"),
    day_of_week: int | None = Query(None, ge=0, le=6, description="Optional weekday filter (0=Mon..6=Sun)"),
) -> MyTimetableResponse:
    """
    Returns published class timetables and entries for the authenticated user.
    """
    tz_str = await get_tenant_timezone(db, current_user.tenant_id)
    authorized_class_ids = await resolve_authorized_class_ids(db, current_user, class_id)

    # Fetch published timetables
    tt_query = (
        select(Timetable)
        .where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id.in_(authorized_class_ids),
            Timetable.status == "published",
        )
        .order_by(Timetable.class_id)
    )
    tt_res = await db.execute(tt_query)
    published_timetables = tt_res.scalars().all()

    serialized_timetables: list[TimetableResponse] = []
    for tt in published_timetables:
        serialized = await _serialize_timetable(db, tt)
        if day_of_week is not None:
            serialized.entries = [e for e in serialized.entries if e.day_of_week == day_of_week]
        serialized_timetables.append(serialized)

    # For teachers: also query individual sessions taught by user across all classes
    teaching_entries: list[TimetableEntryResponse] = []
    teacher_query = (
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
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.teacher_id == current_user.id,
            Timetable.status == "published",
        )
    )
    if day_of_week is not None:
        teacher_query = teacher_query.where(TimetableEntry.day_of_week == day_of_week)
    teacher_query = teacher_query.order_by(TimetableEntry.day_of_week, TimetableEntry.start_time)
    t_res = await db.execute(teacher_query)

    for entry, c_name, s_name, s_code, t_name in t_res.all():
        teaching_entries.append(
            TimetableEntryResponse(
                id=entry.id,
                timetable_id=entry.timetable_id,
                class_id=entry.class_id,
                class_name=c_name,
                subject_id=entry.subject_id,
                subject_name=s_name,
                subject_code=s_code,
                teacher_id=entry.teacher_id,
                teacher_name=t_name,
                day_of_week=entry.day_of_week,
                day_name=DAY_NAMES.get(entry.day_of_week, ""),
                start_time=entry.start_time.isoformat(),
                end_time=entry.end_time.isoformat(),
                room=entry.room,
                note=entry.note,
                created_at=entry.created_at,
            )
        )

    return MyTimetableResponse(
        tenant_timezone=tz_str,
        classes=serialized_timetables,
        teaching_entries=teaching_entries,
    )


# ── Class Timetable Management (Admin & Assigned Teachers) ─────────────────────

@router.get(
    "/classes/{class_id}/timetables",
    response_model=list[TimetableResponse],
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def list_class_timetables(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> list[TimetableResponse]:
    """List all timetables (published and draft) for a class."""
    query = (
        select(Timetable)
        .where(Timetable.tenant_id == current_user.tenant_id, Timetable.class_id == class_id)
        .order_by(desc(Timetable.updated_at))
    )
    res = await db.execute(query)
    timetables = res.scalars().all()
    return [await _serialize_timetable(db, tt) for tt in timetables]


@router.get(
    "/classes/{class_id}/timetables/draft",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def get_or_create_draft_timetable(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """
    Get the active draft timetable for this class.
    If no draft exists but a published timetable exists, creates a new draft copy of it.
    If neither exists, initializes a fresh blank draft timetable.
    """
    # 1. Look for existing draft
    draft_query = select(Timetable).where(
        Timetable.tenant_id == current_user.tenant_id,
        Timetable.class_id == class_id,
        Timetable.status == "draft",
    )
    draft_res = await db.execute(draft_query)
    draft = draft_res.scalar_one_or_none()
    if draft:
        return await _serialize_timetable(db, draft)

    # 2. Look for published timetable to clone
    pub_query = select(Timetable).where(
        Timetable.tenant_id == current_user.tenant_id,
        Timetable.class_id == class_id,
        Timetable.status == "published",
    )
    pub_res = await db.execute(pub_query)
    published = pub_res.scalar_one_or_none()

    if published:
        # Clone published into a new draft with version + 1
        new_draft = Timetable(
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            academic_year=published.academic_year,
            term=published.term,
            status="draft",
            version=published.version + 1,
        )
        db.add(new_draft)
        await db.flush()

        # Copy entries
        old_entries_res = await db.execute(
            select(TimetableEntry).where(
                TimetableEntry.tenant_id == current_user.tenant_id,
                TimetableEntry.timetable_id == published.id,
            )
        )
        for old_e in old_entries_res.scalars().all():
            new_e = TimetableEntry(
                tenant_id=current_user.tenant_id,
                timetable_id=new_draft.id,
                class_id=class_id,
                subject_id=old_e.subject_id,
                teacher_id=old_e.teacher_id,
                day_of_week=old_e.day_of_week,
                start_time=old_e.start_time,
                end_time=old_e.end_time,
                room=old_e.room,
                note=old_e.note,
            )
            db.add(new_e)

        await db.flush()
        return await _serialize_timetable(db, new_draft)

    # 3. Create fresh blank draft
    blank_draft = Timetable(
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        status="draft",
        version=1,
    )
    db.add(blank_draft)
    await db.flush()
    return await _serialize_timetable(db, blank_draft)


@router.post(
    "/classes/{class_id}/timetables/draft",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def create_draft_timetable(
    class_id: uuid.UUID,
    payload: TimetableCreate,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """Create a new draft timetable for this class."""
    if len(payload.entries) > MAX_TIMETABLE_ENTRIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "entries_cap_exceeded", "message": f"A timetable cannot have more than {MAX_TIMETABLE_ENTRIES} entries."},
        )

    # Validate all entries relations
    for item in payload.entries:
        await _validate_entry_relations(
            db=db,
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            subject_id=item.subject_id,
            teacher_id=item.teacher_id,
        )

    # Check if a draft already exists; if so, replace it
    existing = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    old_draft = existing.scalar_one_or_none()
    if old_draft:
        await db.execute(
            delete(TimetableEntry).where(
                TimetableEntry.tenant_id == current_user.tenant_id,
                TimetableEntry.timetable_id == old_draft.id,
            )
        )
        draft = old_draft
        draft.academic_year = payload.academic_year
        draft.term = payload.term
    else:
        draft = Timetable(
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            academic_year=payload.academic_year,
            term=payload.term,
            status="draft",
            version=1,
        )
        db.add(draft)
        await db.flush()

    for item in payload.entries:
        entry = TimetableEntry(
            tenant_id=current_user.tenant_id,
            timetable_id=draft.id,
            class_id=class_id,
            subject_id=item.subject_id,
            teacher_id=item.teacher_id,
            day_of_week=item.day_of_week,
            start_time=item.start_time,
            end_time=item.end_time,
            room=item.room,
            note=item.note,
        )
        db.add(entry)

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:create_draft",
        target_type="timetable",
        target_id=str(draft.id),
        details={"class_id": str(class_id), "entries_count": len(payload.entries)},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    return await _serialize_timetable(db, draft)


@router.put(
    "/classes/{class_id}/timetables/draft",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def update_draft_timetable(
    class_id: uuid.UUID,
    payload: TimetableUpdate,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """Bulk update draft timetable metadata and entries."""
    res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    draft = res.scalar_one_or_none()
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "no_draft", "message": "No draft timetable found for this class."},
        )

    if payload.academic_year is not None:
        draft.academic_year = payload.academic_year
    if payload.term is not None:
        draft.term = payload.term

    if payload.entries is not None:
        if len(payload.entries) > MAX_TIMETABLE_ENTRIES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "entries_cap_exceeded", "message": f"A timetable cannot have more than {MAX_TIMETABLE_ENTRIES} entries."},
            )

        for item in payload.entries:
            await _validate_entry_relations(
                db=db,
                tenant_id=current_user.tenant_id,
                class_id=class_id,
                subject_id=item.subject_id,
                teacher_id=item.teacher_id,
            )

        # Replace entries
        await db.execute(
            delete(TimetableEntry).where(
                TimetableEntry.tenant_id == current_user.tenant_id,
                TimetableEntry.timetable_id == draft.id,
            )
        )
        for item in payload.entries:
            entry = TimetableEntry(
                tenant_id=current_user.tenant_id,
                timetable_id=draft.id,
                class_id=class_id,
                subject_id=item.subject_id,
                teacher_id=item.teacher_id,
                day_of_week=item.day_of_week,
                start_time=item.start_time,
                end_time=item.end_time,
                room=item.room,
                note=item.note,
            )
            db.add(entry)

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:update_draft",
        target_type="timetable",
        target_id=str(draft.id),
        details={"class_id": str(class_id), "entries_count": len(payload.entries) if payload.entries is not None else None},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    return await _serialize_timetable(db, draft)


@router.post(
    "/classes/{class_id}/timetables/draft/entries",
    response_model=TimetableEntryResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def add_draft_entry(
    class_id: uuid.UUID,
    payload: TimetableEntryCreate,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableEntryResponse:
    """Add a single scheduled entry to the draft timetable."""
    # 1. Validate subject linked and teacher assigned
    await _validate_entry_relations(
        db=db,
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        subject_id=payload.subject_id,
        teacher_id=payload.teacher_id,
    )

    # 2. Ensure draft timetable exists
    res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    draft = res.scalar_one_or_none()
    if not draft:
        draft = Timetable(tenant_id=current_user.tenant_id, class_id=class_id, status="draft", version=1)
        db.add(draft)
        await db.flush()

    # 3. Check 200 entries cap
    count_res = await db.execute(
        select(func.count(TimetableEntry.id)).where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.timetable_id == draft.id,
        )
    )
    total_entries = count_res.scalar_one() or 0
    if total_entries >= MAX_TIMETABLE_ENTRIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "entries_cap_exceeded", "message": f"A timetable cannot have more than {MAX_TIMETABLE_ENTRIES} entries."},
        )

    entry = TimetableEntry(
        tenant_id=current_user.tenant_id,
        timetable_id=draft.id,
        class_id=class_id,
        subject_id=payload.subject_id,
        teacher_id=payload.teacher_id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        room=payload.room,
        note=payload.note,
    )
    db.add(entry)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:create_entry",
        target_type="timetable_entry",
        target_id=str(entry.id),
        details={"class_id": str(class_id), "subject_id": str(entry.subject_id), "day_of_week": entry.day_of_week},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    # Fetch names
    s_name_res = await db.execute(select(Subject.name, Subject.code).where(Subject.id == entry.subject_id))
    s_name, s_code = s_name_res.one()

    t_name = None
    if entry.teacher_id:
        t_name_res = await db.execute(select(User.full_name).where(User.id == entry.teacher_id))
        t_name = t_name_res.scalar_one_or_none()

    return TimetableEntryResponse(
        id=entry.id,
        timetable_id=entry.timetable_id,
        class_id=entry.class_id,
        subject_id=entry.subject_id,
        subject_name=s_name,
        subject_code=s_code,
        teacher_id=entry.teacher_id,
        teacher_name=t_name,
        day_of_week=entry.day_of_week,
        day_name=DAY_NAMES.get(entry.day_of_week, ""),
        start_time=entry.start_time.isoformat(),
        end_time=entry.end_time.isoformat(),
        room=entry.room,
        note=entry.note,
        created_at=entry.created_at,
    )


@router.put(
    "/classes/{class_id}/timetables/draft/entries/{entry_id}",
    response_model=TimetableEntryResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def update_draft_entry(
    class_id: uuid.UUID,
    entry_id: uuid.UUID,
    payload: TimetableEntryUpdate,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableEntryResponse:
    """Update an existing scheduled entry inside the draft timetable."""
    # 1. Validate subject linked and teacher assigned
    await _validate_entry_relations(
        db=db,
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        subject_id=payload.subject_id,
        teacher_id=payload.teacher_id,
    )

    res = await db.execute(
        select(TimetableEntry)
        .join(Timetable, (Timetable.tenant_id == TimetableEntry.tenant_id) & (Timetable.id == TimetableEntry.timetable_id))
        .where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.id == entry_id,
            TimetableEntry.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    entry.day_of_week = payload.day_of_week
    entry.start_time = payload.start_time
    entry.end_time = payload.end_time
    entry.subject_id = payload.subject_id
    entry.teacher_id = payload.teacher_id
    entry.room = payload.room
    entry.note = payload.note

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:update_entry",
        target_type="timetable_entry",
        target_id=str(entry.id),
        details={"class_id": str(class_id), "subject_id": str(entry.subject_id), "day_of_week": entry.day_of_week},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    s_name_res = await db.execute(select(Subject.name, Subject.code).where(Subject.id == entry.subject_id))
    s_name, s_code = s_name_res.one()

    t_name = None
    if entry.teacher_id:
        t_name_res = await db.execute(select(User.full_name).where(User.id == entry.teacher_id))
        t_name = t_name_res.scalar_one_or_none()

    return TimetableEntryResponse(
        id=entry.id,
        timetable_id=entry.timetable_id,
        class_id=entry.class_id,
        subject_id=entry.subject_id,
        subject_name=s_name,
        subject_code=s_code,
        teacher_id=entry.teacher_id,
        teacher_name=t_name,
        day_of_week=entry.day_of_week,
        day_name=DAY_NAMES.get(entry.day_of_week, ""),
        start_time=entry.start_time.isoformat(),
        end_time=entry.end_time.isoformat(),
        room=entry.room,
        note=entry.note,
        created_at=entry.created_at,
    )


@router.delete(
    "/classes/{class_id}/timetables/draft/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def delete_draft_entry(
    class_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> None:
    """Delete an entry from the draft timetable."""
    res = await db.execute(
        select(TimetableEntry)
        .join(Timetable, (Timetable.tenant_id == TimetableEntry.tenant_id) & (Timetable.id == TimetableEntry.timetable_id))
        .where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.id == entry_id,
            TimetableEntry.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:delete_entry",
        target_type="timetable_entry",
        target_id=str(entry.id),
        details={"class_id": str(class_id), "subject_id": str(entry.subject_id)},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    await db.delete(entry)
    await db.flush()


@router.post(
    "/classes/{class_id}/timetables/check-conflicts",
    response_model=ConflictCheckResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def check_conflicts(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> ConflictCheckResponse:
    """
    Check draft entries for teacher, room, and class overlaps across the tenant.
    """
    draft_res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    draft = draft_res.scalar_one_or_none()
    if not draft:
        return ConflictCheckResponse(has_conflicts=False, conflicts=[])

    entries_res = await db.execute(
        select(TimetableEntry).where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.timetable_id == draft.id,
        )
    )
    entries = entries_res.scalars().all()
    candidate_dicts = [
        {
            "day_of_week": e.day_of_week,
            "start_time": e.start_time,
            "end_time": e.end_time,
            "teacher_id": e.teacher_id,
            "subject_id": e.subject_id,
            "room": e.room,
        }
        for e in entries
    ]

    conflicts = await detect_timetable_conflicts(
        db, current_user.tenant_id, class_id, candidate_dicts, exclude_timetable_id=draft.id
    )
    return ConflictCheckResponse(has_conflicts=len(conflicts) > 0, conflicts=conflicts)


@router.post(
    "/classes/{class_id}/timetables/publish",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:publish", scope=Scope(ScopeType.CLASS)))],
)
async def publish_timetable(
    class_id: uuid.UUID,
    request: Request,
    payload: PublishTimetableRequest,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:publish", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """
    Publish the draft timetable for this class:
      1. Performs conflict check across the whole tenant.
      2. If conflicts are found and confirm_conflicts is False, returns HTTP 409 Conflict.
      3. Archives/unpublishes any previously published timetable for this class.
      4. Marks the draft as published and writes an audit log entry.
    """
    draft_res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    draft = draft_res.scalar_one_or_none()
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "no_draft", "message": "No draft timetable found to publish."},
        )

    # 1. Conflict detection
    entries_res = await db.execute(
        select(TimetableEntry).where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.timetable_id == draft.id,
        )
    )
    entries = entries_res.scalars().all()
    candidate_dicts = [
        {
            "day_of_week": e.day_of_week,
            "start_time": e.start_time,
            "end_time": e.end_time,
            "teacher_id": e.teacher_id,
            "subject_id": e.subject_id,
            "room": e.room,
        }
        for e in entries
    ]

    conflicts = await detect_timetable_conflicts(
        db, current_user.tenant_id, class_id, candidate_dicts, exclude_timetable_id=draft.id
    )

    if conflicts and not payload.confirm_conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "timetable_conflicts_detected",
                "message": "Schedule conflicts detected. Pass confirm_conflicts=true to override.",
                "conflicts": [c.model_dump() for c in conflicts],
            },
        )

    # 2. Archive previous published timetable for this class
    prev_res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "published",
            Timetable.id != draft.id,
        )
    )
    prev_published = prev_res.scalars().all()
    for prev in prev_published:
        prev.status = "draft"

    # 3. Mark current draft as published
    draft.status = "published"
    draft.version = (draft.version or 1)
    await db.flush()

    # 4. Write audit log entry
    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:publish",
        target_type="timetable",
        target_id=str(draft.id),
        details={
            "class_id": str(class_id),
            "version": draft.version,
            "entries_count": len(entries),
            "conflicts_count": len(conflicts),
            "conflicts_confirmed": bool(conflicts and payload.confirm_conflicts),
        },
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    return await _serialize_timetable(db, draft)


@router.post(
    "/classes/{class_id}/timetables/unpublish",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:publish", scope=Scope(ScopeType.CLASS)))],
)
async def unpublish_timetable(
    class_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:publish", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """
    Unpublish the published timetable for this class (reverts to draft).
    """
    pub_res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "published",
        )
    )
    published = pub_res.scalar_one_or_none()
    if not published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_published", "message": "No published timetable found for this class."},
        )

    published.status = "draft"
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:unpublish",
        target_type="timetable",
        target_id=str(published.id),
        details={"class_id": str(class_id), "version": published.version},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    return await _serialize_timetable(db, published)


# ─── File Import & Downloadable Templates ──────────────────────────────────────

@router.get(
    "/classes/{class_id}/timetables/template.csv",
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def download_csv_template(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> Response:
    """Download a CSV timetable template pre-populated with valid class subjects & teachers."""
    class_res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    class_obj = class_res.scalar_one_or_none()
    if not class_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # Fetch subjects
    subj_res = await db.execute(
        select(Subject)
        .join(ClassSubject, (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id))
        .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == class_id)
        .order_by(Subject.name)
    )
    subjects = subj_res.scalars().all()

    # Fetch assigned teachers
    teach_res = await db.execute(
        select(User)
        .join(TeacherAssignment, (TeacherAssignment.tenant_id == User.tenant_id) & (TeacherAssignment.user_id == User.id))
        .where(TeacherAssignment.tenant_id == current_user.tenant_id, TeacherAssignment.class_id == class_id)
        .distinct()
    )
    teachers = teach_res.scalars().all()

    csv_content = generate_csv_template(class_obj.name, list(subjects), list(teachers))
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="timetable_template_{class_obj.name.replace(" ", "_")}.csv"'},
    )


@router.get(
    "/classes/{class_id}/timetables/template.xlsx",
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def download_xlsx_template(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> Response:
    """Download an Excel (.xlsx) timetable template pre-populated with valid class subjects & teachers."""
    class_res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    class_obj = class_res.scalar_one_or_none()
    if not class_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # Fetch subjects
    subj_res = await db.execute(
        select(Subject)
        .join(ClassSubject, (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id))
        .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == class_id)
        .order_by(Subject.name)
    )
    subjects = subj_res.scalars().all()

    # Fetch assigned teachers
    teach_res = await db.execute(
        select(User)
        .join(TeacherAssignment, (TeacherAssignment.tenant_id == User.tenant_id) & (TeacherAssignment.user_id == User.id))
        .where(TeacherAssignment.tenant_id == current_user.tenant_id, TeacherAssignment.class_id == class_id)
        .distinct()
    )
    teachers = teach_res.scalars().all()

    xlsx_content = generate_xlsx_template(class_obj.name, list(subjects), list(teachers))
    return Response(
        content=xlsx_content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="timetable_template_{class_obj.name.replace(" ", "_")}.xlsx"'},
    )


@router.post(
    "/classes/{class_id}/timetables/import-preview",
    response_model=ImportPreviewResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def preview_timetable_import(
    class_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
    file: UploadFile = File(...),
) -> ImportPreviewResponse:
    """
    Parse an uploaded timetable file (CSV, XLSX, DOCX, text PDF) and return a review preview
    with row-level validation, matching, and error flags. Never saves to database.
    """
    # 1. Rate limit check (per user and per tenant)
    await check_and_record_rate_limit(current_user.id, current_user.tenant_id)

    # 2. Check class exists
    class_res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    class_obj = class_res.scalar_one_or_none()
    if not class_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # 3. Stream upload bytes with 2 MB cap
    file_bytes = await stream_upload_with_limit(file, max_bytes=2 * 1024 * 1024)

    # 4. Sanitize filename and validate file type (magic bytes)
    clean_name = sanitize_filename(file.filename)
    format_type = sniff_and_validate_file_type(file_bytes, clean_name)

    # 5. Execute parsing in a kill-able process with 10s timeout and concurrency caps
    records = await run_killable_parse(file_bytes, format_type, current_user.tenant_id, timeout_seconds=10.0)

    # 6. Fetch class subjects & assigned teachers for exact matching
    subj_res = await db.execute(
        select(Subject)
        .join(ClassSubject, (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id))
        .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == class_id)
        .order_by(Subject.name)
    )
    class_subjects = subj_res.scalars().all()

    ta_res = await db.execute(
        select(TeacherAssignment, User)
        .join(User, (User.tenant_id == TeacherAssignment.tenant_id) & (User.id == TeacherAssignment.user_id))
        .where(TeacherAssignment.tenant_id == current_user.tenant_id, TeacherAssignment.class_id == class_id)
    )
    teacher_assignments_rows = ta_res.all()

    # 7. Perform entity matching and validation
    matched_data = match_and_validate_records(
        records=records,
        target_class_id=class_id,
        target_class_name=class_obj.name,
        class_subjects=list(class_subjects),
        teacher_assignments=list(teacher_assignments_rows),
    )

    # 8. Record audit log (sanitized filename & row counts only, NO file content)
    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:import_preview",
        target_type="class",
        target_id=str(class_id),
        details={
            "filename": clean_name,
            "format": format_type,
            "total_rows": matched_data["total_rows"],
            "valid_rows": matched_data["valid_rows"],
            "error_rows": matched_data["error_rows"],
        },
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    # Build available options
    available_subjects = [
        AvailableSubjectItem(id=s.id, code=s.code, name=s.name) for s in class_subjects
    ]

    # Collect unique assigned teachers
    seen_teachers = set()
    available_teachers = []
    for _ta, u in teacher_assignments_rows:
        if u and u.id not in seen_teachers:
            seen_teachers.add(u.id)
            available_teachers.append(
                AvailableTeacherItem(id=u.id, full_name=u.full_name, email=u.email)
            )

    return ImportPreviewResponse(
        filename=clean_name,
        total_rows=matched_data["total_rows"],
        valid_rows=matched_data["valid_rows"],
        error_rows=matched_data["error_rows"],
        requires_period_times=matched_data["requires_period_times"],
        period_labels=matched_data["period_labels"],
        rows=[ParsedPreviewRow(**row) for row in matched_data["rows"]],
        available_subjects=available_subjects,
        available_teachers=available_teachers,
    )


@router.post(
    "/classes/{class_id}/timetables/save-imported-draft",
    response_model=TimetableResponse,
    dependencies=[Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
)
async def save_imported_draft(
    class_id: uuid.UUID,
    payload: SaveImportedDraftRequest,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("timetable:manage", scope=Scope(ScopeType.CLASS)))],
) -> TimetableResponse:
    """
    Save validated imported timetable rows to the draft timetable.
    Re-validates EVERY row server-side against class subjects and teacher assignments.
    Requires replace_confirmed=True if a draft timetable already contains entries.
    Never modifies published timetables and never auto-publishes.
    """
    # 1. Check class exists
    class_res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    class_obj = class_res.scalar_one_or_none()
    if not class_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    # 2. Re-validate EVERY single row server-side
    validated_entries_data = []
    for idx, item in enumerate(payload.entries):
        # Validate time strings
        try:
            s_t = datetime.time.fromisoformat(item.start_time)
            e_t = datetime.time.fromisoformat(item.end_time)
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_time_format", "message": f"Row {idx+1}: invalid time format."},
            ) from err
        if e_t <= s_t:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_time_order", "message": f"Row {idx+1}: end_time must be after start_time."},
            )

        # Validate subject linked to class and teacher assigned to that class+subject
        await _validate_entry_relations(
            db=db,
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            subject_id=item.subject_id,
            teacher_id=item.teacher_id,
        )

        validated_entries_data.append({
            "day_of_week": item.day_of_week,
            "start_time": s_t,
            "end_time": e_t,
            "subject_id": item.subject_id,
            "teacher_id": item.teacher_id,
            "room": item.room,
            "note": item.note,
        })

    # 3. Check existing draft timetable
    draft_res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == current_user.tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "draft",
        )
    )
    draft = draft_res.scalar_one_or_none()

    existing_count = 0
    if draft:
        count_res = await db.execute(
            select(func.count(TimetableEntry.id)).where(
                TimetableEntry.tenant_id == current_user.tenant_id,
                TimetableEntry.timetable_id == draft.id,
            )
        )
        existing_count = count_res.scalar_one() or 0

    # 4. Require explicit confirmation if replacing existing draft entries
    if existing_count > 0 and not payload.replace_confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "confirmation_required",
                "message": f"Draft timetable already contains {existing_count} entries. Please confirm replacement with {len(payload.entries)} imported entries.",
                "existing_row_count": existing_count,
                "new_row_count": len(payload.entries),
            },
        )

    # 5. Ensure draft exists
    if not draft:
        draft = Timetable(
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            status="draft",
            version=1,
        )
        db.add(draft)
        await db.flush()

    # 6. Delete old draft entries and insert new entries
    await db.execute(
        delete(TimetableEntry).where(
            TimetableEntry.tenant_id == current_user.tenant_id,
            TimetableEntry.timetable_id == draft.id,
        )
    )

    for entry_dict in validated_entries_data:
        new_entry = TimetableEntry(
            tenant_id=current_user.tenant_id,
            timetable_id=draft.id,
            class_id=class_id,
            subject_id=entry_dict["subject_id"],
            teacher_id=entry_dict["teacher_id"],
            day_of_week=entry_dict["day_of_week"],
            start_time=entry_dict["start_time"],
            end_time=entry_dict["end_time"],
            room=entry_dict["room"],
            note=entry_dict["note"],
        )
        db.add(new_entry)

    await db.flush()

    # 7. Audit log
    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="timetable:save_imported_draft",
        target_type="timetable",
        target_id=str(draft.id),
        details={
            "class_id": str(class_id),
            "entries_count": len(payload.entries),
            "previous_entries_count": existing_count,
            "replaced": existing_count > 0,
        },
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    return await _serialize_timetable(db, draft)


