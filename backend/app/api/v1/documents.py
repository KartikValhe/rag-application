"""
app/api/v1/documents.py
───────────────────────
Document routes, step 5b-1 (design A2-A4). Upload, replace, list, detail, PATCH, soft delete,
status, retry, version delete and download, plus the admin usage and purge routes.
Publish is here since 5c-2 (it calls services/document_lifecycle.publish_version, the same function the worker's
auto-publish uses). Test search and the read-only flags route are here since 5d-2 (docs/phase5d-plan.md
section 6; chunk exclusion is backlog B94).

Rules every route follows:
  - require(permission) first (default-deny); students hold no document:* permission, so they get 403.
  - Scope comes from ONE place, app/services/document_scope.py: generic 404 when the caller has no
    linked pair, 403 document_manage_requires_all_classes when only some.
  - Every change and its audit entry share one transaction, opened inside the route and committed
    before the response is sent (the request session only commits after the response).
  - Storage keys never leave the server; files are found through the database row (RLS-scoped).

Upload (A3 steps 1-8): scope -> rate limits -> stream with the tenant's max_file_bytes ->
validate_upload_async(DOCUMENT, tenant caps) -> sha256 -> then ONE transaction: quota advisory
lock, duplicate / in-flight checks, quota check, write the file to the private store, document,
links, version (queued), ingest job (queued) and audit. If anything after the file write fails the
file is deleted again (compensating delete), so a failed upload leaves no file and no rows.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TenantDB
from app.core.authz import GENERIC_NOT_FOUND, require
from app.core.config import settings
from app.core.tenant_db import tenant_session_context
from app.core.upload_security import (
    UploadProfile,
    attachment_header,
    sanitize_filename,
    stream_upload_with_limit,
    validate_upload_async,
)
from app.db.models.academic import Class, Subject
from app.db.models.document import (
    DOCUMENT_VERSION_STATUSES,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    VERSION_STATUS_ARCHIVED,
    VERSION_STATUS_FAILED,
    VERSION_STATUS_PUBLISHED,
    VERSION_STATUS_QUEUED,
    VERSION_STATUS_READY,
    Chunk,
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
    StorageDeletion,
)
from app.db.models.user import User
from app.schemas.document import (
    DocumentLimitsModel,
    DocumentListResponse,
    DocumentResponse,
    DocumentStatusResponse,
    DocumentUpdateRequest,
    DocumentUsageResponse,
    DocumentVersionResponse,
    FlaggedChunk,
    FlagMatch,
    NamedRef,
    PublishRequest,
    TestSearchHit,
    TestSearchRequest,
    TestSearchResponse,
    VersionFlagsResponse,
)
from app.services import (
    document_lifecycle,
    document_limits,
    document_scope,
    document_store,
    document_upload,
    injection_scan,
    retrieval,
)
from app.services.audit import record_audit_event
from app.services.document_limits import DocumentLimits
from app.services.document_upload import api_error

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])
admin_router = APIRouter(prefix="/admin/documents", tags=["documents"])

MAX_CLASSES_PER_DOCUMENT = 50
DEFAULT_PAGE_SIZE = 20
# A version may be deleted only while it is a draft (ready, not published) or failed.
DELETABLE_VERSION_STATUSES = (VERSION_STATUS_READY, VERSION_STATUS_FAILED)
# The download is a sandboxed attachment; a browser must never render or run it.
DOWNLOAD_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"

# The duplicate pre-check. A module-level name, so a test can switch it off and show that the
# partial unique index still answers with the same stable code.
_active_duplicate = document_upload.active_duplicate


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)


def _unique(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(ids))


def _version_response(v: DocumentVersion, warnings: Sequence[str] = ()) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        id=v.id,
        document_id=v.document_id,
        version_no=v.version_no,
        status=v.status,
        stage=v.stage,
        error_code=v.error_code,
        file_kind=v.file_kind,
        original_filename=v.original_filename,
        size_bytes=v.size_bytes,
        page_count=v.page_count,
        chunk_count=v.chunk_count,
        flagged_chunk_count=v.flagged_chunk_count,
        created_at=v.created_at,
        published_at=v.published_at,
        warnings=list(warnings),
    )


async def _build_documents(
    db: AsyncSession, user: User, documents: Sequence[Document], scope: document_scope.CallerScope
) -> list[DocumentResponse]:
    """DocumentResponse for each document, with names, versions and can_manage (from the shared rule)."""
    tid = user.tenant_id
    ids = [d.id for d in documents]
    if not ids:
        return []
    linked = await document_scope.linked_pairs(db, tid, ids)
    class_ids = {c for pairs in linked.values() for c, _ in pairs}
    subject_ids = {d.subject_id for d in documents}
    class_names: dict[uuid.UUID, str] = {}
    if class_ids:
        rows = await db.execute(select(Class.id, Class.name).where(Class.tenant_id == tid, Class.id.in_(class_ids)))
        class_names = {r[0]: r[1] for r in rows.all()}
    rows = await db.execute(
        select(Subject.id, Subject.name).where(Subject.tenant_id == tid, Subject.id.in_(subject_ids))
    )
    subject_names: dict[uuid.UUID, str] = {r[0]: r[1] for r in rows.all()}
    versions = (
        await db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.tenant_id == tid, DocumentVersion.document_id.in_(ids))
            .order_by(DocumentVersion.document_id, DocumentVersion.version_no)
        )
    ).scalars().all()
    latest: dict[uuid.UUID, DocumentVersion] = {}
    published: dict[uuid.UUID, DocumentVersion] = {}
    for v in versions:
        latest[v.document_id] = v
        if v.status == VERSION_STATUS_PUBLISHED:
            published[v.document_id] = v

    out: list[DocumentResponse] = []
    for d in documents:
        can_manage = scope.access(linked[d.id])[1]
        classes = sorted(
            (NamedRef(id=c, name=class_names.get(c, "")) for c, _ in linked[d.id]), key=lambda r: (r.name, str(r.id))
        )
        out.append(
            DocumentResponse(
                id=d.id,
                title=d.title,
                description=d.description,
                subject=NamedRef(id=d.subject_id, name=subject_names.get(d.subject_id, "")),
                classes=classes,
                created_by=d.created_by,
                created_at=d.created_at,
                updated_at=d.updated_at,
                latest_version=_version_response(latest[d.id]) if d.id in latest else None,
                published_version=_version_response(published[d.id]) if d.id in published else None,
                can_manage=can_manage,
                blocked_reason=None if can_manage else document_scope.MANAGE_BLOCKED_CODE,
            )
        )
    return out


async def _document_response(db: AsyncSession, user: User, document: Document) -> DocumentResponse:
    scope = await document_scope.caller_scope(db, user)
    return (await _build_documents(db, user, [document], scope))[0]


async def _load_version(
    db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID, *, for_update: bool = False
) -> DocumentVersion:
    """A version of THIS document, or the generic 404."""
    stmt = select(DocumentVersion).where(
        DocumentVersion.tenant_id == tenant_id,
        DocumentVersion.document_id == document_id,
        DocumentVersion.id == version_id,
    )
    if for_update:
        stmt = stmt.with_for_update(key_share=True)
    version = (await db.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise _not_found()
    return version


# ── Upload pipeline ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ReceivedFile:
    data: bytes
    filename: str
    kind: str
    sha256: str


@dataclass(frozen=True)
class _NewDocument:
    title: str
    subject_id: uuid.UUID
    class_ids: list[uuid.UUID]


async def _receive_file(file: UploadFile, limits: DocumentLimits) -> _ReceivedFile:
    """Read the file within the TENANT's size limit and validate it with the tenant's page and slide caps."""
    data = await stream_upload_with_limit(file, limits.max_file_bytes)
    filename = sanitize_filename(file.filename)
    kind = await validate_upload_async(
        data,
        filename,
        UploadProfile.DOCUMENT,
        max_file_bytes=limits.max_file_bytes,
        max_pdf_pages=limits.max_pdf_pages,
        max_pptx_slides=limits.max_pptx_slides,
    )
    return _ReceivedFile(data=data, filename=filename, kind=kind, sha256=hashlib.sha256(data).hexdigest())


async def _duplicate_content_warnings(
    db: AsyncSession, user: User, sha256: str, exclude_document: uuid.UUID | None
) -> list[str]:
    """'duplicate_content' when the same bytes exist in ANOTHER live document the caller can see; else silent."""
    stmt = (
        select(DocumentVersion.document_id)
        .join(
            Document,
            (Document.tenant_id == DocumentVersion.tenant_id) & (Document.id == DocumentVersion.document_id),
        )
        .where(
            DocumentVersion.tenant_id == user.tenant_id,
            DocumentVersion.sha256 == sha256,
            Document.deleted_at.is_(None),
        )
        .distinct()
    )
    if exclude_document is not None:
        stmt = stmt.where(DocumentVersion.document_id != exclude_document)
    others = list((await db.execute(stmt)).scalars())
    if not others:
        return []
    scope = await document_scope.caller_scope(db, user)
    linked = await document_scope.linked_pairs(db, user.tenant_id, others)
    return ["duplicate_content"] if any(scope.access(linked[d])[0] for d in others) else []


async def _discard_file(store: document_store.PrivateObjectStore, tenant_id: uuid.UUID, key: str) -> None:
    """Compensating delete after a failed transaction. Best effort; logged (without the key) if it fails."""
    try:
        await store.delete(tenant_id, key)
    except Exception as exc:  # the outbox cannot record it: the transaction that would have is gone
        logger.error("document_compensating_delete_failed", tenant_id=str(tenant_id), error=type(exc).__name__)


async def _discard_unless_committed(store: document_store.PrivateObjectStore, tenant_id: uuid.UUID, key: str) -> None:
    """
    Cleanup after an upload transaction that ended in an error or a cancellation. A lost connection or a
    cancellation during COMMIT can hide a commit that SUCCEEDED, and then a version references the file.
    So a FRESH session first asks whether any version uses the storage key; the file is deleted only when
    none does. If the check itself fails nothing is known, so the file is KEPT and logged: a stray file is
    harmless, a committed version without its file is not (orphan reconciliation: backlog B41).
    """
    try:
        async with tenant_session_context(str(tenant_id)) as db:
            referenced = (
                await db.execute(
                    select(
                        exists().where(DocumentVersion.tenant_id == tenant_id, DocumentVersion.storage_key == key)
                    )
                )
            ).scalar_one()
    except Exception as exc:
        logger.error("document_orphan_check_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
        return
    if referenced:
        logger.warning("document_file_kept_after_committed_upload", tenant_id=str(tenant_id))
        return
    await _discard_file(store, tenant_id, key)


async def _commit_version(
    request: Request,
    user: User,
    limits: DocumentLimits,
    received: _ReceivedFile,
    *,
    document_id: uuid.UUID | None = None,
    new_document: _NewDocument | None = None,
) -> DocumentVersionResponse:
    """A3 steps 5-8 in one transaction: a new document (new_document) or a new version (document_id)."""
    tid = user.tenant_id
    store = document_store.get_document_store()
    key = document_store.new_document_key(tid)
    stored = False
    try:
        async with tenant_session_context(str(tid)) as db:
            await document_upload.lock_quota(db, tid)
            if document_id is not None:
                scoped = await document_scope.load_document_for(db, user, document_id, action="manage", for_update=True)
                duplicate = await _active_duplicate(db, tid, document_id, received.sha256)
                if duplicate is not None:
                    raise api_error(status.HTTP_409_CONFLICT, "duplicate_version", version_id=str(duplicate))
                if await document_upload.in_flight_version(db, tid, document_id) is not None:
                    raise api_error(status.HTTP_409_CONFLICT, "document_processing_in_progress")
            usage = await document_upload.tenant_usage(db, tid)
            document_upload.check_quota(usage, limits, new_document=document_id is None, new_bytes=len(received.data))
            warnings = await _duplicate_content_warnings(db, user, received.sha256, document_id)

            try:
                await store.put(tid, key, received.data)
            except Exception as exc:
                logger.error("document_store_write_failed", tenant_id=str(tid), error=type(exc).__name__)
                raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable") from None
            stored = True

            if document_id is None:
                assert new_document is not None
                document = Document(
                    tenant_id=tid, subject_id=new_document.subject_id, title=new_document.title, created_by=user.id
                )
                db.add(document)
                await db.flush()
                db.add_all(
                    DocumentClass(tenant_id=tid, document_id=document.id, class_id=c, subject_id=document.subject_id)
                    for c in new_document.class_ids
                )
                version_no = 1
                action = "document:upload"
            else:
                document = scoped.document
                last = (
                    await db.execute(
                        select(func.max(DocumentVersion.version_no)).where(
                            DocumentVersion.tenant_id == tid, DocumentVersion.document_id == document.id
                        )
                    )
                ).scalar_one()
                version_no = (last or 0) + 1
                action = "document:version_upload"

            version = DocumentVersion(
                tenant_id=tid,
                document_id=document.id,
                version_no=version_no,
                status=VERSION_STATUS_QUEUED,
                file_kind=received.kind,
                original_filename=received.filename,
                size_bytes=len(received.data),
                sha256=received.sha256,
                storage_key=key,
                uploaded_by=user.id,
            )
            db.add(version)
            await db.flush()
            # Same transaction as the version: a job can never be lost between commit and enqueue.
            db.add(IngestJob(tenant_id=tid, version_id=version.id, status=JOB_STATUS_QUEUED))
            await db.flush()
            await record_audit_event(
                tenant_id=tid,
                action=action,
                target_type="document",
                target_id=str(document.id),
                details={
                    "version_id": str(version.id),
                    "version_no": version_no,
                    "subject_id": str(document.subject_id),
                    "class_ids": sorted(str(c) for c in new_document.class_ids) if new_document else None,
                    "file_kind": received.kind,
                    "size_bytes": len(received.data),
                },
                actor_id=user.id,
                request=request,
                db=db,
            )
            await db.refresh(version)
            response = _version_response(version, warnings)
    except IntegrityError as exc:
        if stored:
            await _discard_unless_committed(store, tid, key)
        mapped = document_upload.integrity_error_to_http(exc)
        if mapped is None:
            raise
        if isinstance(mapped.detail, dict) and mapped.detail.get("code") == "duplicate_version" and document_id:
            async with tenant_session_context(str(tid)) as db:
                existing = await document_upload.active_duplicate(db, tid, document_id, received.sha256)
            mapped.detail["version_id"] = str(existing) if existing else None
        raise mapped from None
    except BaseException:
        # Also on asyncio.CancelledError. The cleanup is shielded so a second cancellation cannot cut it
        # short, and the original exception (the cancellation included) is always re-raised.
        if stored:
            await asyncio.shield(_discard_unless_committed(store, tid, key))
        raise
    return response


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentVersionResponse)
async def upload_document(
    request: Request,
    db: TenantDB,
    user: Annotated[User, Depends(require("document:upload"))],
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(max_length=200)],
    subject_id: Annotated[uuid.UUID, Form()],
    class_ids: Annotated[list[uuid.UUID], Form()],
) -> DocumentVersionResponse:
    """Upload a new document for one subject and one or more classes. 202: the version is queued."""
    clean_title = title.strip()
    if not clean_title:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_title")
    classes = _unique(class_ids)
    if len(classes) > MAX_CLASSES_PER_DOCUMENT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "too_many_classes", "message": "A document can be linked to at most 50 classes."},
        )
    await document_scope.authorize_document_scope(db, user, subject_id, classes)
    limits = await document_limits.load_tenant_limits(db, user.tenant_id)
    await document_upload.check_upload_rate(user.id, user.tenant_id, limits)
    received = await _receive_file(file, limits)
    return await _commit_version(
        request, user, limits, received,
        new_document=_NewDocument(title=clean_title, subject_id=subject_id, class_ids=classes),
    )


@router.post("/{document_id}/versions", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentVersionResponse)
async def upload_document_version(
    document_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    user: Annotated[User, Depends(require("document:upload"))],
    file: Annotated[UploadFile, File()],
) -> DocumentVersionResponse:
    """Replace: upload a new version. Needs manage on ALL linked classes; one version in flight at a time."""
    await document_scope.load_document_for(db, user, document_id, action="manage")
    limits = await document_limits.load_tenant_limits(db, user.tenant_id)
    await document_upload.check_upload_rate(user.id, user.tenant_id, limits)
    received = await _receive_file(file, limits)
    return await _commit_version(request, user, limits, received, document_id=document_id)


# ── List and detail ──────────────────────────────────────────────────────────


def _encode_cursor(document: Document) -> str:
    raw = json.dumps({"t": document.created_at.isoformat(), "i": str(document.id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor") from None


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    db: TenantDB,
    user: Annotated[User, Depends(require("document:view"))],
    class_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
) -> DocumentListResponse:
    """
    Live documents the caller can see, newest first. Teachers only get documents with at least one
    linked pair in their scope. `status` filters on the latest version's status.
    """
    if status_filter is not None and status_filter not in DOCUMENT_VERSION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_status", "message": "Unknown status filter."},
        )
    tid = user.tenant_id
    scope = await document_scope.caller_scope(db, user)
    if not scope.is_admin and not scope.pairs:
        return DocumentListResponse(items=[], next_cursor=None)

    links = select(DocumentClass.document_id).where(
        DocumentClass.tenant_id == Document.tenant_id, DocumentClass.document_id == Document.id
    )
    stmt = select(Document).where(Document.tenant_id == tid, Document.deleted_at.is_(None))
    if not scope.is_admin:
        stmt = stmt.where(
            exists(links.where(tuple_(DocumentClass.class_id, DocumentClass.subject_id).in_(sorted(scope.pairs))))
        )
    if class_id is not None:
        stmt = stmt.where(exists(links.where(DocumentClass.class_id == class_id)))
    if subject_id is not None:
        stmt = stmt.where(Document.subject_id == subject_id)
    if status_filter is not None:
        latest_status = (
            select(DocumentVersion.status)
            .where(DocumentVersion.tenant_id == Document.tenant_id, DocumentVersion.document_id == Document.id)
            .order_by(DocumentVersion.version_no.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = stmt.where(latest_status == status_filter)
    if cursor:
        created_at, last_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Document.created_at, Document.id) < tuple_(created_at, last_id))
    rows = list(
        (await db.execute(stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1))).scalars()
    )
    page, more = rows[:limit], len(rows) > limit
    return DocumentListResponse(
        items=await _build_documents(db, user, page, scope),
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID, db: TenantDB, user: Annotated[User, Depends(require("document:view"))]
) -> DocumentResponse:
    scoped = await document_scope.load_document_for(db, user, document_id, action="view")
    return await _document_response(db, user, scoped.document)


# ── PATCH and DELETE ─────────────────────────────────────────────────────────


@router.patch("/{document_id}", response_model=DocumentResponse)
async def update_document(
    document_id: uuid.UUID,
    body: DocumentUpdateRequest,
    request: Request,
    user: Annotated[User, Depends(require("document:upload"))],
) -> DocumentResponse:
    """
    Change the title and the linked classes. The subject never changes. Linking a class needs that
    (class, subject) pair in the caller's manage set; renaming or unlinking needs manage on ALL
    linked classes.
    """
    tid = user.tenant_id
    async with tenant_session_context(str(tid)) as db:
        scoped = await document_scope.load_document_for(db, user, document_id, action="view", for_update=True)
        document = scoped.document
        new_title = body.title.strip() if body.title is not None else None
        if new_title == "":
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_title")
        current = {c for c, _ in scoped.linked}
        requested = set(_unique(body.class_ids)) if body.class_ids is not None else current
        added, removed = requested - current, current - requested
        title_changed = new_title is not None and new_title != document.title
        if (title_changed or removed) and not scoped.can_manage:
            raise document_scope.manage_blocked()
        if added:
            await document_scope.authorize_document_scope(db, user, document.subject_id, sorted(added))

        if title_changed:
            await db.execute(
                update(Document).where(Document.tenant_id == tid, Document.id == document.id).values(
                    title=new_title, updated_at=func.now()
                )
            )
        if removed:
            await db.execute(
                delete(DocumentClass).where(
                    DocumentClass.tenant_id == tid,
                    DocumentClass.document_id == document.id,
                    DocumentClass.class_id.in_(removed),
                )
            )
        db.add_all(
            DocumentClass(tenant_id=tid, document_id=document.id, class_id=c, subject_id=document.subject_id)
            for c in added
        )
        await db.flush()
        if title_changed or added or removed:
            await record_audit_event(
                tenant_id=tid,
                action="document:update",
                target_type="document",
                target_id=str(document.id),
                details={
                    "title_changed": title_changed,
                    "added_class_ids": sorted(str(c) for c in added),
                    "removed_class_ids": sorted(str(c) for c in removed),
                },
                actor_id=user.id,
                request=request,
                db=db,
            )
        await db.refresh(document)
        return await _document_response(db, user, document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, request: Request, user: Annotated[User, Depends(require("document:delete"))]
) -> Response:
    """
    Soft delete (design A2), one transaction: tombstone the document, delete its chunks (embeddings
    cascade), archive its versions, stop its queued jobs, put every file into the storage outbox
    (due now) and audit. The sweeper deletes the files; the tombstone stays until it is purged, and
    until then the subject cannot be deleted.
    """
    tid = user.tenant_id
    async with tenant_session_context(str(tid)) as db:
        scoped = await document_scope.load_document_for(db, user, document_id, action="delete", for_update=True)
        versions = (
            await db.execute(
                select(DocumentVersion.id, DocumentVersion.storage_key).where(
                    DocumentVersion.tenant_id == tid, DocumentVersion.document_id == document_id
                )
            )
        ).all()
        version_ids = [v[0] for v in versions]
        await db.execute(
            update(Document)
            .where(Document.tenant_id == tid, Document.id == document_id)
            .values(deleted_at=func.now(), deleted_by=user.id, updated_at=func.now())
        )
        await db.execute(delete(Chunk).where(Chunk.tenant_id == tid, Chunk.document_id == document_id))
        await db.execute(
            update(DocumentVersion)
            .where(
                DocumentVersion.tenant_id == tid,
                DocumentVersion.document_id == document_id,
                DocumentVersion.status != VERSION_STATUS_ARCHIVED,
            )
            .values(status=VERSION_STATUS_ARCHIVED, stage=None, updated_at=func.now())
        )
        if version_ids:
            await db.execute(
                update(IngestJob)
                .where(
                    IngestJob.tenant_id == tid,
                    IngestJob.version_id.in_(version_ids),
                    IngestJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
                )
                .values(status=JOB_STATUS_FAILED, last_error_code="document_deleted", updated_at=func.now())
            )
        await document_upload.schedule_storage_deletions(db, tid, [v[1] for v in versions], "document_deleted")
        await record_audit_event(
            tenant_id=tid,
            action="document:delete",
            target_type="document",
            target_id=str(scoped.document.id),
            details={"version_count": len(versions)},
            actor_id=user.id,
            request=request,
            db=db,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Versions: status, retry, delete, download ────────────────────────────────


@router.get("/{document_id}/versions/{version_id}/status", response_model=DocumentStatusResponse)
async def get_version_status(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    db: TenantDB,
    user: Annotated[User, Depends(require("document:view"))],
) -> DocumentStatusResponse:
    await document_scope.load_document_for(db, user, document_id, action="view")
    v = await _load_version(db, user.tenant_id, document_id, version_id)
    return DocumentStatusResponse(
        document_id=v.document_id,
        version_id=v.id,
        version_no=v.version_no,
        status=v.status,
        stage=v.stage,
        error_code=v.error_code,
        retryable=v.status == VERSION_STATUS_FAILED and v.error_code in document_upload.TRANSIENT_ERROR_CODES,
        updated_at=v.updated_at,
    )


@router.post(
    "/{document_id}/versions/{version_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentVersionResponse,
)
async def retry_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require("document:upload"))],
) -> DocumentVersionResponse:
    """
    Queue a failed version again. Only failures with a transient code qualify. The job goes back to
    queued with attempts 0, run_after now and no error (design A3); the lease and worker columns stay
    the worker's.
    """
    tid = user.tenant_id
    try:
        async with tenant_session_context(str(tid)) as db:
            await document_scope.load_document_for(db, user, document_id, action="manage", for_update=True)
            v = await _load_version(db, tid, document_id, version_id, for_update=True)
            await document_upload.check_retry_rate(user.id, v.id)  # after the 404/403 checks: no probing
            if v.status != VERSION_STATUS_FAILED or v.error_code not in document_upload.TRANSIENT_ERROR_CODES:
                raise api_error(status.HTTP_409_CONFLICT, "version_not_retryable")
            if await document_upload.count_manual_retries(db, tid, document_id, v.id) >= settings.DOC_RETRY_MAX_PER_VERSION:
                raise api_error(status.HTTP_409_CONFLICT, "retry_limit_reached")
            if await document_upload.in_flight_version(db, tid, document_id) is not None:
                raise api_error(status.HTTP_409_CONFLICT, "document_processing_in_progress")
            duplicate = await document_upload.active_duplicate(db, tid, document_id, v.sha256)
            if duplicate is not None:
                raise api_error(status.HTTP_409_CONFLICT, "duplicate_version", version_id=str(duplicate))
            await db.execute(
                update(DocumentVersion)
                .where(DocumentVersion.tenant_id == tid, DocumentVersion.id == v.id)
                .values(status=VERSION_STATUS_QUEUED, stage=None, error_code=None, updated_at=func.now())
            )
            job_id = (
                await db.execute(
                    select(IngestJob.id).where(IngestJob.tenant_id == tid, IngestJob.version_id == v.id)
                )
            ).scalar_one_or_none()
            if job_id is None:
                db.add(IngestJob(tenant_id=tid, version_id=v.id, status=JOB_STATUS_QUEUED))
            else:
                await db.execute(
                    update(IngestJob)
                    .where(IngestJob.tenant_id == tid, IngestJob.id == job_id)
                    .values(
                        status=JOB_STATUS_QUEUED,
                        attempts=0,
                        run_after=func.now(),
                        last_error_code=None,
                        updated_at=func.now(),
                    )
                )
            await db.flush()
            await record_audit_event(
                tenant_id=tid,
                action="document:retry",
                target_type="document",
                target_id=str(document_id),
                details={"version_id": str(v.id), "version_no": v.version_no},
                actor_id=user.id,
                request=request,
                db=db,
            )
            await db.refresh(v)
            return _version_response(v)
    except IntegrityError as exc:
        mapped = document_upload.integrity_error_to_http(exc)
        if mapped is None:
            raise
        raise mapped from None


@router.post("/{document_id}/versions/{version_id}/publish", response_model=DocumentVersionResponse)
async def publish_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require("document:publish"))],
    body: Annotated[PublishRequest, Body()] = PublishRequest(),
) -> DocumentVersionResponse:
    """
    Publish a ready version (design A2). A teacher needs an assignment in EVERY linked class (403
    document_manage_requires_all_classes otherwise, generic 404 when out of scope). One transaction: the
    previous published version is archived (its chunks deleted, its file queued for deletion after
    DOC_ARCHIVE_FILE_RETENTION_DAYS), archived versions beyond DOC_MAX_ARCHIVED_VERSIONS are pruned, the new
    version is published and everything is audited. 409 version_not_ready, flags_acknowledgement_required or
    publish_conflict (a concurrent publish won).
    """
    tid = user.tenant_id
    try:
        async with tenant_session_context(str(tid)) as db:
            await document_scope.load_document_for(db, user, document_id, action="publish", for_update=True)
            v = await _load_version(db, tid, document_id, version_id)  # the generic 404 for a foreign version id
            await document_lifecycle.publish_version(
                db,
                tenant_id=tid,
                document_id=document_id,
                version_id=version_id,
                actor_id=user.id,
                acknowledge_flags=body.acknowledge_flags,
                auto=False,
                request=request,
            )
            await db.refresh(v)
            return _version_response(v)
    except IntegrityError as exc:
        mapped = document_upload.integrity_error_to_http(exc)
        if mapped is None:
            raise
        raise mapped from None


@router.delete("/{document_id}/versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(require("document:upload"))],
) -> Response:
    """Delete a draft (ready) or failed version: the row goes (chunks and job cascade), the file goes to the outbox."""
    tid = user.tenant_id
    async with tenant_session_context(str(tid)) as db:
        await document_scope.load_document_for(db, user, document_id, action="manage", for_update=True)
        v = await _load_version(db, tid, document_id, version_id, for_update=True)
        if v.status not in DELETABLE_VERSION_STATUSES:
            raise api_error(status.HTTP_409_CONFLICT, "version_not_deletable")
        await document_upload.schedule_storage_deletions(db, tid, [v.storage_key], "version_deleted")
        await db.execute(delete(DocumentVersion).where(DocumentVersion.tenant_id == tid, DocumentVersion.id == v.id))
        await record_audit_event(
            tenant_id=tid,
            action="document:version_delete",
            target_type="document",
            target_id=str(document_id),
            details={"version_id": str(v.id), "version_no": v.version_no, "status": v.status},
            actor_id=user.id,
            request=request,
            db=db,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _guarded_stream(chunks: AsyncIterator[bytes], tenant_id: uuid.UUID) -> AsyncIterator[bytes]:
    """Pass the file through; a file that vanishes mid-download aborts the response (logged)."""
    try:
        async for chunk in chunks:
            yield chunk
    except document_store.ObjectNotFound:
        logger.error("document_file_vanished_during_download", tenant_id=str(tenant_id))
        raise


@router.get("/{document_id}/versions/{version_id}/download")
async def download_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    user: Annotated[User, Depends(require("document:view"))],
) -> StreamingResponse:
    """
    Stream the original file as an attachment (teachers and admins; students have no document:view).
    The key comes from the RLS-scoped row and the store checks it against the caller's tenant again.
    """
    tid = user.tenant_id
    await document_scope.load_document_for(db, user, document_id, action="view")
    v = await _load_version(db, tid, document_id, version_id)
    store = document_store.get_document_store()
    try:
        chunks = await store.stream(tid, v.storage_key)
    except document_store.ObjectNotFound:
        raise _not_found() from None
    except document_store.InvalidStorageKey:
        logger.error("document_key_outside_tenant", tenant_id=str(tid))
        raise _not_found() from None
    async with tenant_session_context(str(tid)) as audit_db:
        await record_audit_event(
            tenant_id=tid,
            action="document:download",
            target_type="document",
            target_id=str(document_id),
            details={"version_id": str(v.id), "version_no": v.version_no},
            actor_id=user.id,
            request=request,
            db=audit_db,
        )
    stem = v.original_filename.rsplit(".", 1)[0] if "." in v.original_filename else v.original_filename
    headers = {
        "Content-Disposition": attachment_header(stem, v.file_kind),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": DOWNLOAD_CSP,
        "Cache-Control": "private, no-store",
        "Content-Length": str(v.size_bytes),
    }
    return StreamingResponse(_guarded_stream(chunks, tid), media_type="application/octet-stream", headers=headers)


# ── Test search and flags (step 5d-2) ────────────────────────────────────────

QueryEmbedderLoader = Callable[[], Awaitable[retrieval.QueryEmbedder]]


async def _process_query_embedder() -> retrieval.QueryEmbedder:
    """The process-wide model, loaded on the first test search of the process (seconds; backlog B96)."""
    from app.ml import (
        loader,  # imported here: importing the API never loads a machine-learning library
    )

    return retrieval.QueryEmbedder.from_embedder(await loader.get_embedder_async())


def get_query_embedder() -> QueryEmbedderLoader:
    """FastAPI dependency: HOW to get the query embedder, not the embedder itself, so the model is only loaded
    after the permission, scope, rate-limit and query checks. Tests override it."""
    return _process_query_embedder


async def _test_search_scope(db: AsyncSession, user: User, body: TestSearchRequest) -> retrieval.RetrievalScope:
    """The caller's manage scope, narrowed by the ids (never widened); anything outside it is the generic 404.
    A module-level name, so a prove-it test can replace it with one that trusts the request and show the leak."""
    return await retrieval.build_retrieval_scope(
        db, user, purpose="manage", class_id=body.class_id, subject_id=body.subject_id,
        include_drafts=body.include_drafts,
    )


async def _guarded_query_embedder(load: QueryEmbedderLoader) -> retrieval.QueryEmbedder:
    """The loaded embedder, with every failure (loading or embedding) turned into search_unavailable. Only the
    exception class is logged: never a path, a library message or the query."""
    try:
        loaded = await load()
    except Exception as exc:
        logger.error("test_search_embedder_unavailable", stage="load", error=type(exc).__name__)
        raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "search_unavailable") from None

    async def embed(query: str) -> list[float]:
        try:
            return await loaded.embed(query)
        except Exception as exc:
            logger.error("test_search_embedder_unavailable", stage="embed", error=type(exc).__name__)
            raise retrieval.SearchUnavailable("search_unavailable") from None

    return retrieval.QueryEmbedder(model_key=loaded.model_key, dim=loaded.dim, embed=embed)


def _hit_response(hit: retrieval.Hit, scanned: dict[str, bool]) -> TestSearchHit:
    def flagged(value: str) -> bool:
        if value not in scanned:
            scanned[value] = bool(injection_scan.scan_metadata(value))
        return scanned[value]

    return TestSearchHit(
        rank=hit.rank,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_title=hit.document_title,
        version_id=hit.version_id,
        version_no=hit.version_no,
        version_status=hit.version_status,
        page_start=hit.page_start,
        page_end=hit.page_end,
        heading_path=hit.heading_path,
        content=hit.content,
        vector_rank=hit.vector_rank,
        vector_similarity=hit.vector_similarity,
        keyword_rank=hit.keyword_rank,
        keyword_matched_terms=hit.keyword_matched_terms,
        fused_score=hit.fused_score,
        flagged=hit.flagged,
        injection_flags=list(hit.injection_flags),
        title_flagged=flagged(hit.document_title),
        filename_flagged=flagged(hit.original_filename),
    )


@router.post("/test-search", response_model=TestSearchResponse)
async def test_search(
    body: TestSearchRequest,
    user: Annotated[User, Depends(require("document:view"))],
    load_query_embedder: Annotated[QueryEmbedderLoader, Depends(get_query_embedder)],
) -> TestSearchResponse:
    """
    Search the caller's material with scores, for teachers and admins (plan section 6). Checks, in order:
    permission (403), scope (generic 404 for a class, subject or pair the caller does not teach), the rate
    limit (429 test_search_rate_limit_exceeded; 503 when Redis is down outside development), the query (422
    invalid_query / query_too_long), the model (503 search_unavailable), the query embedding under the
    process-wide cap (503 search_busy after RETRIEVAL_EMBED_QUEUE_SECONDS), then the search (503
    search_timeout). include_drafts adds READY versions (never queued, processing, failed or archived). Hits are
    returned even when the material is not sufficient: this is the teacher's view, not the chat. Not audited (a
    read); the query is never logged.

    The scope is read in the first transaction and used by the search in the second one; an assignment revoked
    in between still applies to this one request (accepted, plan section 14). The permission dependency's own
    session stays open for the whole request (backlog B100).
    """
    started = time.perf_counter()
    tid = user.tenant_id
    config = retrieval.RetrievalConfig.from_settings()
    async with tenant_session_context(str(tid)) as db:
        scope = await _test_search_scope(db, user, body)
        await document_upload.check_test_search_rate(user.id)
        try:
            query = retrieval.normalize_query(body.query, config.max_query_chars)
            await retrieval.query_lexemes(db, query)
        except retrieval.QueryError as exc:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from None
    # The model is loaded, and the query embedded, with none of this route's transactions open.
    query_embedder = await _guarded_query_embedder(load_query_embedder)
    try:
        if scope.pairs:  # an empty scope has nothing to compare with: nothing is embedded
            query_embedder = await retrieval.embed_query(query_embedder, query)
        async with tenant_session_context(str(tid)) as db:
            result = await retrieval.search(db, scope, query, query_embedder, config, k=body.k)
    except retrieval.QueryError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from None
    except retrieval.SearchUnavailable as exc:
        logger.warning("test_search_unavailable", tenant_id=str(tid), code=exc.code)
        raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code) from None

    scanned: dict[str, bool] = {}
    hits = [_hit_response(h, scanned) for h in result.hits]
    logger.info(  # counts and timings only: never the query, a title or chunk text
        "test_search",
        tenant_id=str(tid),
        hits=len(hits),
        flagged_hits=sum(1 for h in hits if h.flagged),
        candidate_count=result.candidate_count,
        path=result.path,
        reason=result.reason,
        k=body.k,
        include_drafts=body.include_drafts,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return TestSearchResponse(
        sufficient=result.sufficient,
        reason=result.reason,
        message=result.message,
        threshold=result.threshold,
        threshold_calibrated=result.threshold_calibrated,
        keyword_coverage_threshold=result.keyword_coverage_threshold,
        model=result.model,
        candidate_count=result.candidate_count,
        hits=hits,
    )


MAX_FLAGS_PAGE = 200


@router.get("/{document_id}/versions/{version_id}/flags", response_model=VersionFlagsResponse)
async def get_version_flags(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    db: TenantDB,
    user: Annotated[User, Depends(require("document:view"))],
    after_ordinal: Annotated[int | None, Query(ge=-1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_FLAGS_PAGE)] = 50,
) -> VersionFlagsResponse:
    """
    The flagged chunks of one version, in ordinal order and paged (`after_ordinal`, `limit`), with their stored
    codes, an excerpt of the content (at most 300 characters around its first match) and, per code the current
    rules find, where it matched (heading_path or content) with the matched span (at most 300 characters around
    it): what a teacher acknowledges before publishing. Read-only and not audited. Excerpts, spans, headings and
    titles are UNTRUSTED text. The title and the file name are cleaned and scanned now (a title can change after
    ingest). flagged_chunk_count null means the version was never scanned (indexed before 5d-2).
    """
    scoped = await document_scope.load_document_for(db, user, document_id, action="view")
    v = await _load_version(db, user.tenant_id, document_id, version_id)
    stmt = (
        select(
            Chunk.id, Chunk.ordinal, Chunk.page_start, Chunk.page_end, Chunk.heading_path, Chunk.content,
            Chunk.injection_flags, Chunk.excluded,
        )
        .where(
            Chunk.tenant_id == user.tenant_id,
            Chunk.document_version_id == v.id,
            func.cardinality(Chunk.injection_flags) > 0,
        )
        .order_by(Chunk.ordinal)
        .limit(limit + 1)
    )
    if after_ordinal is not None:
        stmt = stmt.where(Chunk.ordinal > after_ordinal)
    rows = (await db.execute(stmt)).all()
    page, more = rows[:limit], len(rows) > limit
    reviews = await asyncio.to_thread(
        lambda: [
            (injection_scan.review_excerpt(r.content), injection_scan.review_matches(r.heading_path, r.content))
            for r in page
        ]
    )
    items = [
        FlaggedChunk(
            chunk_id=r.id,
            ordinal=r.ordinal,
            page_start=r.page_start,
            page_end=r.page_end,
            heading_path=r.heading_path,
            injection_flags=list(r.injection_flags),
            excluded=r.excluded,
            excerpt=excerpt,
            matches=[FlagMatch(code=m.code, source=m.source, excerpt=m.excerpt) for m in matches],
        )
        for r, (excerpt, matches) in zip(page, reviews, strict=True)
    ]
    return VersionFlagsResponse(
        document_id=v.document_id,
        version_id=v.id,
        version_no=v.version_no,
        version_status=v.status,
        flagged_chunk_count=v.flagged_chunk_count,
        flags_acknowledged=v.flags_acknowledged_by is not None,
        title_flagged=bool(injection_scan.scan_metadata(scoped.document.title)),
        filename_flagged=bool(injection_scan.scan_metadata(v.original_filename)),
        items=items,
        next_after_ordinal=page[-1].ordinal if more and page else None,
    )


# ── Admin: usage and purge ───────────────────────────────────────────────────


@admin_router.get("/usage", response_model=DocumentUsageResponse)
async def document_usage(
    db: TenantDB, user: Annotated[User, Depends(require("settings:manage"))]
) -> DocumentUsageResponse:
    usage = await document_upload.tenant_usage(db, user.tenant_id)
    limits = await document_limits.load_tenant_limits(db, user.tenant_id)
    return DocumentUsageResponse(
        documents=usage.documents,
        storage_bytes=usage.storage_bytes,
        pages=usage.pages,
        limits=DocumentLimitsModel(**limits.as_dict()),
    )


@admin_router.post("/{document_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_document(
    document_id: uuid.UUID, request: Request, user: Annotated[User, Depends(require("settings:manage"))]
) -> Response:
    """
    Hard-delete a soft-deleted document now: the tombstone, its versions (chunks, jobs and links
    cascade) and its files. The outbox rows are written in the same transaction as the delete, so a
    file that cannot be deleted right now is still deleted later by the sweeper.
    """
    tid = user.tenant_id
    async with tenant_session_context(str(tid)) as db:
        document = (
            await db.execute(
                select(Document).where(Document.tenant_id == tid, Document.id == document_id).with_for_update()
            )
        ).scalar_one_or_none()
        if document is None:
            raise _not_found()
        if document.deleted_at is None:
            raise api_error(status.HTTP_409_CONFLICT, "document_not_deleted")
        keys = list(
            (
                await db.execute(
                    select(DocumentVersion.storage_key).where(
                        DocumentVersion.tenant_id == tid, DocumentVersion.document_id == document_id
                    )
                )
            ).scalars()
        )
        await document_upload.schedule_storage_deletions(db, tid, keys, "document_purged")
        await db.execute(delete(Document).where(Document.tenant_id == tid, Document.id == document_id))
        await record_audit_event(
            tenant_id=tid,
            action="document:purge",
            target_type="document",
            target_id=str(document_id),
            details={"version_count": len(keys)},
            actor_id=user.id,
            request=request,
            db=db,
        )

    store = document_store.get_document_store()
    deleted: list[str] = []
    for key in keys:
        try:
            await store.delete(tid, key)
            deleted.append(key)
        except Exception as exc:  # left in the outbox for the sweeper
            logger.warning("document_purge_file_delete_deferred", tenant_id=str(tid), error=type(exc).__name__)
    if deleted:
        async with tenant_session_context(str(tid)) as db:
            await db.execute(
                delete(StorageDeletion).where(
                    StorageDeletion.tenant_id == tid, StorageDeletion.storage_key.in_(deleted)
                )
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
