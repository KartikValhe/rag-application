"""
app/services/document_upload.py
───────────────────────────────
Building blocks of the document upload pipeline (design A3 steps 2, 5, 6) and of the lifecycle
routes, shared by app/api/v1/documents.py and the admin usage endpoint:

  - check_upload_rate:  per-user and per-tenant hourly upload windows (SlotManager, one atomic step,
                        fails closed with 503 when Redis is down outside development);
  - check_retry_rate / count_manual_retries: the limits of the retry route (hourly windows per user and
                        per version, and a total cap of manual retries per version);
  - check_test_search_rate: the per-user window of the test-search route (5d-2);
  - tenant_usage / lock_quota / check_quota: the quota check, run inside the upload transaction
                        under pg_advisory_xact_lock(hashtext('doc_quota:' || tenant_id));
  - active_duplicate / in_flight_version: the duplicate and one-in-flight rules (the partial unique
                        indexes of 0008/0009 are the final word; integrity_error_to_http maps them);
  - schedule_storage_deletions: the storage outbox (storage_deletions) used by delete and purge.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import slots
from app.core.authz import GENERIC_NOT_FOUND
from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.document import (
    DOCUMENT_VERSION_STATUSES,
    IN_FLIGHT_VERSION_STATUSES,
    INACTIVE_VERSION_STATUSES,
    VERSION_STATUS_FAILED,
    Document,
    DocumentVersion,
    StorageDeletion,
)
from app.services.document_limits import DocumentLimits

logger = structlog.get_logger(__name__)

USER_RATE_KEY_PREFIX = "ratelimit:doc_upload:user:"
TENANT_RATE_KEY_PREFIX = "ratelimit:doc_upload:tenant:"
RETRY_USER_RATE_KEY_PREFIX = "ratelimit:doc_retry:user:"
RETRY_VERSION_RATE_KEY_PREFIX = "ratelimit:doc_retry:version:"
TEST_SEARCH_USER_RATE_KEY_PREFIX = "ratelimit:doc_test_search:user:"
RETRY_ACTION = "document:retry"
RATE_WINDOW_SECONDS = 3600
UNAVAILABLE_MESSAGE = "Document upload is temporarily unavailable. Please try again in a few minutes."
SEARCH_UNAVAILABLE_MESSAGE = "Search is temporarily unavailable. Please try again in a few minutes."

# Pages count while a version is active (it is, or will be, searchable). Storage counts every
# version of a live document, because each one still has its file.
PAGE_COUNTING_STATUSES: tuple[str, ...] = (*IN_FLIGHT_VERSION_STATUSES, "published")

# Failed versions with one of these codes may be retried (design A3: transient errors end as
# processing_failed after the last attempt; worker_crash is the crash code). Everything else is
# permanent (validation, scanned_pdf, parse_timeout, too_many_pages, quota ...).
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset({"processing_failed", "worker_crash"})


def is_retryable(version_status: str, error_code: str | None) -> bool:
    """
    THE retry rule: a version may be retried by hand only when it is failed with a transient error code.
    The status route, the retry route and `retryable` on the version responses all call this function.
    """
    return version_status == VERSION_STATUS_FAILED and error_code in TRANSIENT_ERROR_CODES

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
    "retry_limit_reached": "This file has been retried too many times. Upload it again as a new version.",
    "service_unavailable": "Document storage is temporarily unavailable. Please try again in a few minutes.",
    "version_not_ready": "Only a version that has finished processing (status ready) can be published.",
    "flags_acknowledgement_required": "This version has flagged passages. Review them and publish again with acknowledge_flags set to true.",
    "publish_conflict": "Another version of this document was published at the same time. Reload and try again.",
    # Test search (5d-2). Fixed texts: never the query, SQL or a library message.
    "invalid_query": "Type a question with at least one word.",
    "query_too_long": "The question is too long. Shorten it and try again.",
    "search_unavailable": SEARCH_UNAVAILABLE_MESSAGE,
    "search_timeout": "The search took too long. Please try again.",
    "search_busy": "Search is busy right now. Please try again in a moment.",
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


@dataclass(frozen=True)
class UploadWindowCounts:
    """Live entries of the two hourly upload windows: the tenant's, and the calling user's own."""

    tenant: int
    caller: int


def upload_window_keys(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, str]:
    """
    The Redis keys of the caller's and the tenant's upload windows, (caller_key, tenant_key). The same prefixes
    `check_upload_rate` writes. Redis has no row-level security: these keys are the isolation, so they are built
    from the authenticated user's own ids and from nothing else.
    """
    return f"{USER_RATE_KEY_PREFIX}{user_id}", f"{TENANT_RATE_KEY_PREFIX}{tenant_id}"


async def uploads_last_hour(user_id: uuid.UUID, tenant_id: uuid.UUID) -> UploadWindowCounts | None:
    """
    Upload ATTEMPTS in the rolling RATE_WINDOW_SECONDS window, read from the windows `check_upload_rate`
    enforces (so "limit minus count" is the real remaining quota). An attempt takes its slot once the scope check
    passed and before the file is read, so a rejected upload still counts.

    Read-only display: if Redis cannot be read (the slot manager answers 503 outside development, or any other
    error) this logs a warning and returns None, which the route sends as null values. The limiter itself stays
    fail-closed; this function never touches it.
    """
    caller_key, tenant_key = upload_window_keys(user_id, tenant_id)
    try:
        caller = await _slots().count(caller_key)
        tenant = await _slots().count(tenant_key)
    except Exception as exc:
        logger.warning("document_upload_counts_unavailable", error=type(exc).__name__)
        return None
    return UploadWindowCounts(tenant=tenant, caller=caller)


async def check_retry_rate(user_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """
    Record one manual retry in the user's and the version's hourly windows, atomically (a version-level
    rejection never uses up the user's window). 429 with Retry-After, or 503 (fail closed) when Redis is
    unreachable outside development.
    """
    windows = [
        slots.SlotLimit(f"{RETRY_USER_RATE_KEY_PREFIX}{user_id}", settings.DOC_RETRY_RATE_USER, RATE_WINDOW_SECONDS),
        slots.SlotLimit(
            f"{RETRY_VERSION_RATE_KEY_PREFIX}{version_id}", settings.DOC_RETRY_RATE_VERSION, RATE_WINDOW_SECONDS
        ),
    ]
    try:
        await _slots().acquire(windows, unavailable_message=UNAVAILABLE_MESSAGE)
    except slots.SlotRejected as rej:
        if rej.index == 0:
            raise _too_many(
                "retry_rate_limit_exceeded",
                "You are retrying too often. Please wait a while and try again.",
                rej.retry_after_seconds,
            ) from None
        raise _too_many(
            "retry_version_rate_limit_exceeded",
            "This file was retried too often just now. Please wait a while and try again.",
            rej.retry_after_seconds,
        ) from None


async def check_test_search_rate(user_id: uuid.UUID) -> None:
    """
    Record one test search in the user's window (DOC_TEST_SEARCH_RATE_USER per
    DOC_TEST_SEARCH_RATE_WINDOW_SECONDS). The route calls it AFTER the scope check, so a probe of foreign ids
    never uses up the window. 429 with Retry-After, or 503 (fail closed) when Redis is unreachable outside
    development.
    """
    limit, window = settings.DOC_TEST_SEARCH_RATE_USER, settings.DOC_TEST_SEARCH_RATE_WINDOW_SECONDS
    try:
        await _slots().acquire(
            [slots.SlotLimit(f"{TEST_SEARCH_USER_RATE_KEY_PREFIX}{user_id}", limit, window)],
            unavailable_message=SEARCH_UNAVAILABLE_MESSAGE,
        )
    except slots.SlotRejected as rej:
        raise _too_many(
            "test_search_rate_limit_exceeded",
            f"You can run {limit} test searches every {max(1, window // 60)} minutes. Please wait and try again.",
            rej.retry_after_seconds,
        ) from None


async def count_manual_retries(db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID) -> int:
    """
    How often this version was retried by hand: its `document:retry` entries in the append-only audit log
    (the retry route writes one in the same transaction as the retry itself). The caller holds the
    document and version row locks, so two retries of one version are serialized and cannot both pass.
    """
    count = await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == RETRY_ACTION,
            AuditLog.target_id == str(document_id),
            AuditLog.details["version_id"].astext == str(version_id),
        )
    )
    return int(count or 0)


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


async def version_status_counts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """
    Versions per status (all six keys, zero when none) of the tenant's LIVE documents: a soft delete archives
    every version, so counting those would inflate `archived`. A separate function from `tenant_usage` on
    purpose: that one feeds the quota check. Row-level security keeps tenants apart; the explicit tenant_id
    filter is the second layer.
    """
    live = (Document.tenant_id == DocumentVersion.tenant_id) & (Document.id == DocumentVersion.document_id)
    rows = (
        await db.execute(
            select(DocumentVersion.status, func.count())
            .select_from(DocumentVersion)
            .join(Document, live)
            .where(DocumentVersion.tenant_id == tenant_id, Document.deleted_at.is_(None))
            .group_by(DocumentVersion.status)
        )
    ).all()
    counts = dict.fromkeys(DOCUMENT_VERSION_STATUSES, 0)
    for version_status, n in rows:
        counts[version_status] = int(n)
    return counts


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
    "uq_document_versions_one_published": (status.HTTP_409_CONFLICT, "publish_conflict"),
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
