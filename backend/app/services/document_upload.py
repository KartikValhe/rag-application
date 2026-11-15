"""
app/services/document_upload.py
───────────────────────────────
Building blocks of the document upload pipeline (design A3 steps 2, 5, 6) and of the lifecycle
routes, shared by app/api/v1/documents.py and the admin usage endpoint:

  - check_upload_rate:  per-user and per-tenant hourly upload windows (SlotManager, one atomic step,
                        fails closed with 503 when Redis is down outside development);
  - tenant_usage / lock_quota / check_quota: the quota check, run inside the upload transaction
                        under pg_advisory_xact_lock(hashtext('doc_quota:' || tenant_id));
  - active_duplicate / in_flight_version: the duplicate and one-in-flight rules (the partial unique
                        indexes of 0008/0009 are the final word; integrity_error_to_http maps them);
  - schedule_storage_deletions: the storage outbox (storage_deletions) used by delete and purge.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import slots
from app.core.authz import GENERIC_NOT_FOUND
from app.db.models.document import (
    IN_FLIGHT_VERSION_STATUSES,
    INACTIVE_VERSION_STATUSES,
    Document,
    DocumentVersion,
    StorageDeletion,
)
from app.services.document_limits import DocumentLimits

USER_RATE_KEY_PREFIX = "ratelimit:doc_upload:user:"
TENANT_RATE_KEY_PREFIX = "ratelimit:doc_upload:tenant:"
RATE_WINDOW_SECONDS = 3600
UNAVAILABLE_MESSAGE = "Document upload is temporarily unavailable. Please try again in a few minutes."

# Pages count while a version is active (it is, or will be, searchable). Storage counts every
# version of a live document, because each one still has its file.
PAGE_COUNTING_STATUSES: tuple[str, ...] = (*IN_FLIGHT_VERSION_STATUSES, "published")

# Failed versions with one of these codes may be retried (design A3: transient errors end as
# processing_failed after the last attempt; worker_crash is the crash code). Everything else is
# permanent (validation, scanned_pdf, parse_timeout, too_many_pages, quota ...).
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset({"processing_failed", "worker_crash"})

ERROR_MESSAGES: dict[str, str] = {
    "duplicate_version": "This file is already an active version of this document.",
    "document_processing_in_progress": "A version of this document is still being processed. Try again when it is done.",
    "quota_documents_exceeded": "Your organisation has reached its document limit.",
    "quota_storage_exceeded": "Your organisation has reached its storage limit.",
    "quota_pages_exceeded": "Your organisation has reached its page limit.",
    "version_not_retryable": "Only versions that failed for a temporary reason can be retried.",
    "version_not_deletable": "Only draft or failed versions can be deleted.",
    "document_not_deleted": "Only deleted documents can be purged.",
    "invalid_title": "The title must not be empty.",
    "invalid_cursor": "The cursor is not valid.",
    "service_unavailable": "Document storage is temporarily unavailable. Please try again in a few minutes.",
}


def api_error(status_code: int, code: str, **extra: Any) -> HTTPException:
    """The standard {"detail": {"code", "message"}} error with the fixed message for `code`."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": ERROR_MESSAGES[code], **extra})


# ── Rate limits (A3 step 2) ──────────────────────────────────────────────────


def _slots() -> slots.SlotManager:
    return slots.default_manager


def _too_many(code: str, message: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": code, "message": message, "retry_after_seconds": retry_after},
        headers={"Retry-After": str(max(1, retry_after))},
    )


async def check_upload_rate(user_id: uuid.UUID, tenant_id: uuid.UUID, limits: DocumentLimits) -> None:
    """
    Record one upload in the user's and the tenant's hourly windows, atomically: a tenant-level
    rejection never uses up the user's window. Raises 429 with Retry-After, or 503 (fail closed)
    when Redis is unreachable outside development.
    """
    windows = [
        slots.SlotLimit(f"{USER_RATE_KEY_PREFIX}{user_id}", limits.uploads_per_user_per_hour, RATE_WINDOW_SECONDS),
        slots.SlotLimit(f"{TENANT_RATE_KEY_PREFIX}{tenant_id}", limits.uploads_per_tenant_per_hour, RATE_WINDOW_SECONDS),
    ]
    try:
        await _slots().acquire(windows, unavailable_message=UNAVAILABLE_MESSAGE)
    except slots.SlotRejected as rej:
        if rej.index == 0:
            raise _too_many(
                "rate_limit_exceeded",
                f"You can upload {limits.uploads_per_user_per_hour} documents per hour. Please wait and try again.",
                rej.retry_after_seconds,
            ) from None
        raise _too_many(
            "tenant_rate_limit_exceeded",
            "Your organisation has reached its hourly upload limit. Please wait and try again.",
            rej.retry_after_seconds,
        ) from None


# ── Quotas (A3 step 6) ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    documents: int
    storage_bytes: int
    pages: int


async def lock_quota(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Serialize quota checks of one tenant until the end of the caller's transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('doc_quota:' || :tid))"), {"tid": str(tenant_id)}
    )


async def tenant_usage(db: AsyncSession, tenant_id: uuid.UUID) -> Usage:
    """What the tenant uses now. Soft-deleted documents do not count."""
    live = (Document.tenant_id == DocumentVersion.tenant_id) & (Document.id == DocumentVersion.document_id)
    documents = (
        await db.execute(
            select(func.count()).select_from(Document).where(
                Document.tenant_id == tenant_id, Document.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(DocumentVersion.size_bytes), 0),
                func.coalesce(
                    func.sum(DocumentVersion.page_count).filter(DocumentVersion.status.in_(PAGE_COUNTING_STATUSES)), 0
                ),
            )
            .join(Document, live)
            .where(DocumentVersion.tenant_id == tenant_id, Document.deleted_at.is_(None))
        )
    ).one()
    return Usage(documents=int(documents), storage_bytes=int(totals[0] or 0), pages=int(totals[1] or 0))


def check_quota(usage: Usage, limits: DocumentLimits, *, new_document: bool, new_bytes: int) -> None:
    """
    422 with a stable code when the upload would go over a quota. A value exactly at the limit is
    allowed. The page count of the new file is unknown until it is parsed, so here the pages quota
    refuses an upload once the pages already used reach the limit; the parser re-checks (5b-2).
    """
    if new_document and usage.documents + 1 > limits.max_documents:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "quota_documents_exceeded")
    if usage.storage_bytes + new_bytes > limits.max_storage_bytes:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "quota_storage_exceeded")
    if usage.pages >= limits.max_pages:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "quota_pages_exceeded")


# ── Duplicates and the one-in-flight rule (A3 step 5) ────────────────────────


async def active_duplicate(
    db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, sha256: str
) -> uuid.UUID | None:
    """The ACTIVE version of this document with the same bytes (failed and archived do not count)."""
    return (
        await db.execute(
            select(DocumentVersion.id).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.document_id == document_id,
                DocumentVersion.sha256 == sha256,
                DocumentVersion.status.not_in(INACTIVE_VERSION_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def in_flight_version(db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(DocumentVersion.id).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.document_id == document_id,
                DocumentVersion.status.in_(IN_FLIGHT_VERSION_STATUSES),
            )
        )
    ).scalar_one_or_none()


def constraint_name(exc: IntegrityError) -> str | None:
    """The name of the violated constraint, from the asyncpg error behind SQLAlchemy's wrapper."""
    for candidate in (getattr(exc, "orig", None), getattr(getattr(exc, "orig", None), "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


_CONSTRAINT_ERRORS: dict[str, tuple[int, str]] = {
    "uq_document_versions_active_sha256": (status.HTTP_409_CONFLICT, "duplicate_version"),
    "uq_document_versions_one_in_flight": (status.HTTP_409_CONFLICT, "document_processing_in_progress"),
    "uq_document_versions_version_no": (status.HTTP_409_CONFLICT, "document_processing_in_progress"),
    "uq_ingest_jobs_version": (status.HTTP_409_CONFLICT, "document_processing_in_progress"),
}
_NOT_FOUND_CONSTRAINTS = frozenset(
    {"fk_document_classes_class_subject", "fk_documents_subject", "fk_document_classes_document"}
)


def integrity_error_to_http(exc: IntegrityError) -> HTTPException | None:
    """A stable API error for a known constraint, or None (then the error is a real bug: re-raise)."""
    name = constraint_name(exc)
    if name in _CONSTRAINT_ERRORS:
        code_status, code = _CONSTRAINT_ERRORS[name]
        return api_error(code_status, code)
    if name in _NOT_FOUND_CONSTRAINTS:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
    return None


# ── Storage outbox ───────────────────────────────────────────────────────────


async def schedule_storage_deletions(
    db: AsyncSession, tenant_id: uuid.UUID, keys: list[str], reason: str
) -> None:
    """
    Make sure every key has an outbox row that is due now. A key that already waits (for example an
    archived version kept for 30 days) is moved up instead of being added twice.
    """
    if not keys:
        return
    existing = set(
        (
            await db.execute(
                select(StorageDeletion.storage_key).where(
                    StorageDeletion.tenant_id == tenant_id, StorageDeletion.storage_key.in_(keys)
                )
            )
        ).scalars()
    )
    if existing:
        await db.execute(
            update(StorageDeletion)
            .where(StorageDeletion.tenant_id == tenant_id, StorageDeletion.storage_key.in_(existing))
            .values(not_before=func.now())
        )
    db.add_all(
        StorageDeletion(tenant_id=tenant_id, storage_key=key, reason=reason)
        for key in dict.fromkeys(keys)
        if key not in existing
    )
    await db.flush()
