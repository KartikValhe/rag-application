"""
app/services/document_lifecycle.py
──────────────────────────────────
Publish and archive (step 5c-2, design A2 "Lifecycle" and A4). ONE function, `publish_version`, is used by
POST /api/v1/documents/{document_id}/versions/{version_id}/publish and by the worker's auto-publish (inside the
index transaction), so the two can never disagree.

Everything runs in the CALLER's transaction (a tenant session with app.tenant_id set); nothing here commits:

  1. the document row is locked (FOR NO KEY UPDATE): concurrent publishes, deletes and replacements of one
     document are serialised, and the loser sees the winner's committed state;
  2. the version must be `ready` (409 version_not_ready) and, when it has injection flags OR was never scanned
     (flagged_chunk_count NULL, indexed before 5d-2), the caller must have acknowledged that (409
     flags_acknowledgement_required with reason "flagged" or "not_scanned"; the acknowledging user is recorded);
  3. `archive_published`: the previous published version becomes `archived`, its chunks are deleted (their
     embeddings go with them by cascade) and its file goes into the storage outbox with
     not_before = now() + DOC_ARCHIVE_FILE_RETENTION_DAYS (the same transaction time as published_at);
  4. `mark_published`: the new version becomes `published` with published_at / published_by;
  5. `prune_archived`: archived versions beyond DOC_MAX_ARCHIVED_VERSIONS (newest by version number are kept)
     are deleted and their files queued for deletion at once; audited as document:prune_archived;
  6. audit: document:publish (and document:archive per archived version, document:flags_acknowledged).

Because steps 3 and 4 share one transaction a reader sees exactly one published version at every moment
(the partial unique index uq_document_versions_one_published is the last line of defence: a violation is mapped
to the stable code `publish_conflict` by the route). The steps are separate functions only so a test can hold the
transaction between them and prove that; production code never commits in between.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.core.config import settings
from app.services import document_upload
from app.services.audit import record_audit_event
from app.services.document_upload import api_error

REASON_ARCHIVED = "version_archived"
REASON_PRUNED = "version_pruned"


@dataclass(frozen=True)
class ArchivedVersion:
    id: uuid.UUID
    version_no: int
    storage_key: str


@dataclass(frozen=True)
class PrunedVersion:
    id: uuid.UUID
    version_no: int


@dataclass(frozen=True)
class PublishResult:
    version_id: uuid.UUID
    version_no: int
    published_at: datetime
    archived: list[ArchivedVersion]
    pruned: list[PrunedVersion]
    flags_acknowledged: bool


def needs_acknowledgement(flagged_chunk_count: int | None) -> bool:
    """True when publishing needs acknowledge_flags: the version has flagged chunks, OR it was never scanned
    (flagged_chunk_count NULL: indexed before the 5d-2 scanner; python -m app.scripts.rescan_flags fills it).
    "Not scanned" is not "clean" (review of 5d-2, finding 4). A module-level function so a prove-it test can
    switch the rule off."""
    return flagged_chunk_count is None or flagged_chunk_count > 0


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)


async def archive_published(
    db: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID, retention_days: int
) -> list[ArchivedVersion]:
    """The published version(s) of the document become archived; chunks deleted, file queued (not_before = now + retention)."""
    rows = (
        await db.execute(
            text(
                "UPDATE app.document_versions SET status = 'archived', stage = NULL, updated_at = now() "
                "WHERE tenant_id = :t AND document_id = :d AND status = 'published' "
                "RETURNING id, version_no, storage_key"
            ),
            {"t": tenant_id, "d": document_id},
        )
    ).all()
    archived = [ArchivedVersion(id=r[0], version_no=r[1], storage_key=r[2]) for r in rows]
    if archived:
        await db.execute(
            text("DELETE FROM app.chunks WHERE tenant_id = :t AND document_version_id = ANY(CAST(:ids AS uuid[]))"),
            {"t": tenant_id, "ids": [a.id for a in archived]},
        )
        await db.execute(
            text(
                "INSERT INTO app.storage_deletions (tenant_id, storage_key, reason, not_before) "
                "SELECT CAST(:t AS uuid), k, CAST(:reason AS varchar), now() + make_interval(days => CAST(:days AS integer)) "
                "FROM unnest(CAST(:keys AS text[])) AS k "
                "WHERE NOT EXISTS (SELECT 1 FROM app.storage_deletions s WHERE s.tenant_id = :t AND s.storage_key = k)"
            ),
            {"t": tenant_id, "reason": REASON_ARCHIVED, "days": retention_days, "keys": [a.storage_key for a in archived]},
        )
    return archived


async def mark_published(
    db: AsyncSession, *, tenant_id: uuid.UUID, version_id: uuid.UUID, actor_id: uuid.UUID, acknowledge_flags: bool
) -> datetime:
    """The version becomes published (it must still be ready). Returns published_at (the transaction time)."""
    row = (
        await db.execute(
            text(
                "UPDATE app.document_versions SET status = 'published', stage = NULL, error_code = NULL, "
                "published_at = now(), published_by = CAST(:actor AS uuid), "
                "flags_acknowledged_by = CASE WHEN CAST(:ack AS boolean) THEN CAST(:actor AS uuid) ELSE flags_acknowledged_by END, "
                "updated_at = now() "
                "WHERE tenant_id = :t AND id = :v AND status = 'ready' RETURNING published_at"
            ),
            {"t": tenant_id, "v": version_id, "actor": actor_id, "ack": acknowledge_flags},
        )
    ).first()
    if row is None:
        raise api_error(status.HTTP_409_CONFLICT, "version_not_ready")
    published_at: datetime = row[0]
    return published_at


async def prune_archived(
    db: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID, keep: int
) -> list[PrunedVersion]:
    """Delete the archived versions beyond the newest `keep` (by version number); their files are queued at once."""
    rows = (
        await db.execute(
            text(
                "SELECT id, version_no, storage_key FROM app.document_versions "
                "WHERE tenant_id = :t AND document_id = :d AND status = 'archived' "
                "ORDER BY version_no DESC OFFSET :keep"
            ),
            {"t": tenant_id, "d": document_id, "keep": keep},
        )
    ).all()
    if not rows:
        return []
    await db.execute(
        text("DELETE FROM app.document_versions WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))"),
        {"t": tenant_id, "ids": [r[0] for r in rows]},  # chunks and the ingest job go with the row (cascade)
    )
    await document_upload.schedule_storage_deletions(db, tenant_id, [r[2] for r in rows], REASON_PRUNED)
    return [PrunedVersion(id=r[0], version_no=r[1]) for r in rows]


async def publish_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    actor_id: uuid.UUID,
    acknowledge_flags: bool = False,
    auto: bool = False,
    request: Request | None = None,
    retention_days: int | None = None,
    max_archived: int | None = None,
) -> PublishResult:
    """Publish a ready version in the caller's transaction (see the module docstring)."""
    retention = settings.DOC_ARCHIVE_FILE_RETENTION_DAYS if retention_days is None else retention_days
    keep = settings.DOC_MAX_ARCHIVED_VERSIONS if max_archived is None else max_archived

    locked = (
        await db.execute(
            text(
                "SELECT 1 FROM app.documents WHERE tenant_id = :t AND id = :d AND deleted_at IS NULL "
                "FOR NO KEY UPDATE"
            ),
            {"t": tenant_id, "d": document_id},
        )
    ).first()
    if locked is None:
        raise _not_found()
    version = (
        await db.execute(
            text(
                "SELECT version_no, status, flagged_chunk_count FROM app.document_versions "
                "WHERE tenant_id = :t AND document_id = :d AND id = :v FOR UPDATE"
            ),
            {"t": tenant_id, "d": document_id, "v": version_id},
        )
    ).first()
    if version is None:
        raise _not_found()
    version_no, version_status = int(version[0]), version[1]
    flagged: int | None = None if version[2] is None else int(version[2])
    if version_status != "ready":
        raise api_error(status.HTTP_409_CONFLICT, "version_not_ready")
    needs_ack = needs_acknowledgement(flagged)
    if needs_ack and not acknowledge_flags:
        reason = "not_scanned" if flagged is None else "flagged"
        raise api_error(status.HTTP_409_CONFLICT, "flags_acknowledgement_required", reason=reason)
    acknowledged = needs_ack and acknowledge_flags

    archived = await archive_published(db, tenant_id=tenant_id, document_id=document_id, retention_days=retention)
    published_at = await mark_published(
        db, tenant_id=tenant_id, version_id=version_id, actor_id=actor_id, acknowledge_flags=acknowledged
    )
    pruned = await prune_archived(db, tenant_id=tenant_id, document_id=document_id, keep=keep)

    async def audit(action: str, details: dict[str, Any]) -> None:
        await record_audit_event(
            tenant_id=tenant_id, action=action, target_type="document", target_id=str(document_id),
            details=details, actor_id=actor_id, request=request, db=db,
        )

    if acknowledged:
        details: dict[str, Any] = {"version_id": str(version_id), "version_no": version_no, "flagged_chunk_count": flagged}
        if flagged is None:
            details["not_scanned"] = True
        await audit("document:flags_acknowledged", details)
    for old in archived:
        await audit(
            "document:archive",
            {"version_id": str(old.id), "version_no": old.version_no, "superseded_by": str(version_id)},
        )
    if pruned:
        await audit(
            "document:prune_archived",
            {
                "count": len(pruned),
                "pruned": [{"version_id": str(p.id), "version_no": p.version_no} for p in pruned],
            },
        )
    await audit(
        "document:publish",
        {
            "version_id": str(version_id),
            "version_no": version_no,
            "auto": auto,
            "archived_version_ids": [str(a.id) for a in archived],
            "pruned_count": len(pruned),
            "flags_acknowledged": acknowledged,
        },
    )
    return PublishResult(
        version_id=version_id, version_no=version_no, published_at=published_at, archived=archived,
        pruned=pruned, flags_acknowledged=acknowledged,
    )
