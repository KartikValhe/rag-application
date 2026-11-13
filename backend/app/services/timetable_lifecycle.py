"""
app/services/timetable_lifecycle.py
───────────────────────────────────
Timetable status lifecycle for one class:

    draft --publish--> published --(next publish)--> archived
      ^                    |
      +----unpublish-------+   (only when the class has no draft)

Rules:
  - At most one draft and one published timetable per class (partial unique indexes).
  - Archived timetables are read-only history. They are never returned as a draft, never
    shown to students and never restored through the API.
  - Only the newest ARCHIVE_RETENTION archived timetables per class are kept; older ones
    are deleted in the same transaction as the publish that archived them.
"""

import uuid
from collections.abc import Sequence

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.timetable import Timetable, TimetableEntry
from app.services.audit import record_audit_event

ARCHIVE_RETENTION = 5

DRAFT_EXISTS_DETAIL = {
    "code": "timetable_draft_exists",
    "message": "This class already has a draft timetable. Publish or delete the draft first.",
}


async def get_class_timetable(
    db: AsyncSession, tenant_id: uuid.UUID, class_id: uuid.UUID, state: str
) -> Timetable | None:
    """Return the class's draft or published timetable (there is at most one of each)."""
    res = await db.execute(
        select(Timetable).where(
            Timetable.tenant_id == tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == state,
        )
    )
    return res.scalar_one_or_none()


async def next_draft_version(db: AsyncSession, tenant_id: uuid.UUID, class_id: uuid.UUID) -> int:
    """The version a new draft would get if published now (publish recomputes it anyway)."""
    res = await db.execute(
        select(func.max(Timetable.version)).where(
            Timetable.tenant_id == tenant_id,
            Timetable.class_id == class_id,
            Timetable.status.in_(("published", "archived")),
        )
    )
    return (res.scalar_one_or_none() or 0) + 1


async def flush_or_draft_conflict(db: AsyncSession) -> None:
    """
    Flush pending changes. A unique-index violation here means another request created a
    draft for the same class at the same time; report it with the stable draft-exists code.
    """
    try:
        await db.flush()
    except IntegrityError as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DRAFT_EXISTS_DETAIL) from err


async def clone_published_to_draft(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    class_id: uuid.UUID,
    actor_id: uuid.UUID,
    request: Request,
) -> Timetable:
    """Create a draft that is an exact copy of the class's published timetable."""
    if await get_class_timetable(db, tenant_id, class_id, "draft") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DRAFT_EXISTS_DETAIL)

    published = await get_class_timetable(db, tenant_id, class_id, "published")
    if published is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_published", "message": "No published timetable found for this class."},
        )

    draft = Timetable(
        tenant_id=tenant_id,
        class_id=class_id,
        academic_year=published.academic_year,
        term=published.term,
        status="draft",
        version=published.version + 1,
    )
    db.add(draft)
    await flush_or_draft_conflict(db)

    entries_res = await db.execute(
        select(TimetableEntry).where(
            TimetableEntry.tenant_id == tenant_id,
            TimetableEntry.timetable_id == published.id,
        )
    )
    copied = 0
    for old in entries_res.scalars().all():
        db.add(
            TimetableEntry(
                tenant_id=tenant_id,
                timetable_id=draft.id,
                class_id=class_id,
                subject_id=old.subject_id,
                teacher_id=old.teacher_id,
                day_of_week=old.day_of_week,
                start_time=old.start_time,
                end_time=old.end_time,
                room=old.room,
                note=old.note,
            )
        )
        copied += 1
    await db.flush()

    await record_audit_event(
        tenant_id=tenant_id,
        action="timetable:clone",
        target_type="timetable",
        target_id=str(draft.id),
        details={
            "class_id": str(class_id),
            "source_timetable_id": str(published.id),
            "source_version": published.version,
            "entries_count": copied,
        },
        actor_id=actor_id,
        request=request,
        db=db,
    )
    return draft


async def _prune_archived(
    db: AsyncSession, tenant_id: uuid.UUID, class_id: uuid.UUID
) -> list[uuid.UUID]:
    """Delete archived timetables beyond the retention limit, newest kept first."""
    res = await db.execute(
        select(Timetable.id)
        .where(
            Timetable.tenant_id == tenant_id,
            Timetable.class_id == class_id,
            Timetable.status == "archived",
        )
        .order_by(desc(Timetable.version), desc(Timetable.updated_at), desc(Timetable.id))
        .offset(ARCHIVE_RETENTION)
    )
    old_ids: Sequence[uuid.UUID] = res.scalars().all()
    if old_ids:
        # Entries go with their timetable (ON DELETE CASCADE).
        await db.execute(
            delete(Timetable).where(
                Timetable.tenant_id == tenant_id,
                Timetable.id.in_(old_ids),
                Timetable.status == "archived",
            )
        )
    return list(old_ids)


async def publish_draft(
    db: AsyncSession,
    draft: Timetable,
    actor_id: uuid.UUID,
    request: Request,
) -> Timetable | None:
    """
    Archive the current published timetable (if any), then publish the draft with the next
    version number and prune old history. Returns the archived timetable, if one existed.
    The caller writes the publish audit entry.
    """
    tenant_id, class_id = draft.tenant_id, draft.class_id
    previous = await get_class_timetable(db, tenant_id, class_id, "published")

    if previous is not None:
        previous.status = "archived"
        # Flush now so the published slot is free before the draft takes it.
        await db.flush()
        await record_audit_event(
            tenant_id=tenant_id,
            action="timetable:archive",
            target_type="timetable",
            target_id=str(previous.id),
            details={
                "class_id": str(class_id),
                "version": previous.version,
                "replaced_by": str(draft.id),
            },
            actor_id=actor_id,
            request=request,
            db=db,
        )

    draft.version = await next_draft_version(db, tenant_id, class_id)
    draft.status = "published"
    await db.flush()

    pruned = await _prune_archived(db, tenant_id, class_id)
    if pruned:
        await record_audit_event(
            tenant_id=tenant_id,
            action="timetable:prune_archived",
            target_type="class",
            target_id=str(class_id),
            details={"deleted_timetable_ids": [str(i) for i in pruned], "retention": ARCHIVE_RETENTION},
            actor_id=actor_id,
            request=request,
            db=db,
        )
    return previous
