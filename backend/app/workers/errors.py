"""
app/workers/errors.py
─────────────────────
Error classification for the ingestion worker (design A3, "Failures, retries and the sweeper").

- PermanentIngestError: the file or the tenant's limits make the job impossible (validation,
  scanned_pdf, parse_timeout, too_many_pages, quotas ...). The version becomes `failed` with the
  code, the job `failed`. No retry.
- TransientIngestError: an outage (database, Redis, storage) or a crashed child (worker_crash). The
  job goes back to `queued` with exponential backoff, up to INGEST_MAX_ATTEMPTS, then the version
  fails with `processing_failed` (which the API lets a user retry).

Codes are stable strings; no exception text, path or document content is ever stored or logged.
"""

import asyncio

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from app.core.slots import SlotBackendUnavailable

# Codes that end a job for good. Every one is a client-safe code from docs/api/phase5.md.
PERMANENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        # Upload validation (re-run inside the sandbox)
        "file_too_large",
        "empty_file",
        "disallowed_file_type",
        "file_type_mismatch",
        "invalid_openxml_format",
        "macro_enabled_file",
        "embedded_content",
        "external_relationship",
        "invalid_xml",
        "xml_entity_expansion_forbidden",
        "xml_too_complex",
        "corrupt_archive",
        "encrypted_file",
        "zip_bomb_detected",
        "zip_member_too_large",
        "pdf_active_content",
        "pdf_too_complex",
        "unsupported_format",
        "invalid_encoding",
        "too_many_pages",
        "too_many_slides",
        # Parser and quality gates (5b-3)
        "scanned_pdf",
        "password_protected_pdf",
        "corrupt_pdf",
        "parse_timeout",
        "parse_failed",
        "parse_output_too_large",
        "parse_output_invalid",  # the parent refused the child's answer (5b-3)
        "text_too_large",
        "no_text_found",
        "unreadable_text",
        # Quotas re-checked after parsing (backlog B39)
        "quota_documents_exceeded",
        "quota_storage_exceeded",
        "quota_pages_exceeded",
        # The stored file is gone, or a stage broke its contract: retrying cannot help
        "processing_failed",
        # The job ran for INGEST_MAX_JOB_SECONDS: a retry would only run into the same cap again
        "job_time_limit_exceeded",
    }
)

# Codes that send the job back to the queue (job.last_error_code only; the version gets
# processing_failed when the attempts run out).
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset(
    {"worker_crash", "db_unavailable", "redis_unavailable", "storage_unavailable", "slot_lease_lost"}
)

# The code a version gets when the transient attempts run out (design A3).
EXHAUSTED_ERROR_CODE = "processing_failed"

# The code of a job that reached its maximum lifetime (permanent, see PERMANENT_ERROR_CODES).
JOB_TIME_LIMIT_CODE = "job_time_limit_exceeded"


class IngestError(Exception):
    """Base class. `code` is a stable, client-safe error code."""

    permanent = False

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PermanentIngestError(IngestError):
    permanent = True

    def __init__(self, code: str) -> None:
        # An unknown code is never stored as such: it could carry text from a child or a library
        super().__init__(code if code in PERMANENT_ERROR_CODES else "parse_failed")


class TenantContextViolation(PermanentIngestError):
    """
    A stage tried to change the tenant context of its session, or the context was not the job's at
    commit time (workers/db.py). A bug in a stage: retrying cannot help, so it is permanent
    (`processing_failed`). `reason` is a fixed category word, never SQL text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__("processing_failed")
        self.reason = reason


class TransientIngestError(IngestError):
    permanent = False

    def __init__(self, code: str) -> None:
        super().__init__(code if code in TRANSIENT_ERROR_CODES else "worker_crash")


class JobStopped(Exception):  # noqa: N818  (a control-flow signal, not an error)
    """The job lost its lease or slot, or the version was cancelled: stop and write nothing more."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify(exc: BaseException) -> IngestError:
    """Map any exception raised by a stage to an IngestError. Unknown errors count as a crash."""
    if isinstance(exc, IngestError):
        return exc
    if isinstance(exc, SlotBackendUnavailable):
        return TransientIngestError("redis_unavailable")
    if isinstance(exc, HTTPException) and exc.status_code == 503:
        return TransientIngestError("redis_unavailable")  # SlotManager.acquire fails closed with 503
    if isinstance(exc, IntegrityError):
        return TransientIngestError("worker_crash")  # a bug in a stage, not an outage
    if isinstance(exc, OperationalError | InterfaceError | DBAPIError | ConnectionError | asyncio.TimeoutError):
        return TransientIngestError("db_unavailable")
    if isinstance(exc, OSError):
        return TransientIngestError("storage_unavailable")
    return TransientIngestError("worker_crash")
