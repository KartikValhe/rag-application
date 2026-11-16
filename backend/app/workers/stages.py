"""
app/workers/stages.py
─────────────────────
The pipeline stages of the ingestion worker are INJECTABLE functions (step 5b-2). The worker
(workers/ingest.py) runs them in order and handles failures, retries and cleanup around them; what a
stage does is decided later: 5b-3 brings the real parsers, 5c chunking, embedding and indexing.

  validate -> parse -> chunk -> embed -> index          (each: async def stage(ctx: StageContext) -> None)

A stage reads the job and version from the StageContext, keeps its intermediate results in
`ctx.results`, writes to the database only through `ctx.session()` (one transaction, tenant set from
the claimed row, version checked to still be queued or processing), and signals failure by raising
PermanentIngestError (the file or the limits make the job impossible) or TransientIngestError (an
outage). Any other exception is treated as a crash (transient).

Backlog B39 (pages quota): the re-check of the tenant's page quota and `max_pdf_pages` with the page
count found by parsing belongs to the stage hooks, not to the worker. The `index` stage of 5c re-checks
`quota_pages_exceeded` inside its single transaction (design A3 step 8), and the parse stage of 5b-3
re-counts pages with the parser (B33) and raises `too_many_pages`. The sandbox validation below already
refuses a PDF whose uncompressed page objects exceed the tenant's `max_pdf_pages`.

This module also holds the one real stage 5b-2 delivers: `make_validate_stage`, which re-runs the
shared upload validation inside the sandbox (backlog B37).
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import sandbox
from app.services import document_store
from app.services.document_limits import DocumentLimits
from app.workers import errors
from app.workers.db import TenantSessions

if TYPE_CHECKING:
    from app.workers.ingest import IngestSettings
    from app.workers.queue import Job

DOCUMENT_PARSE_CHILD = "app.services.document_parse_child"

# The ingest stage names written to document_versions.stage while each stage runs.
STAGE_NAMES = ("validate", "parse", "chunk", "embed", "index")
STAGE_COLUMN_VALUE = {
    "validate": "parsing",
    "parse": "parsing",
    "chunk": "chunking",
    "embed": "embedding",
    "index": "indexing",
}


@dataclass(frozen=True)
class ParseSettings:
    """Limits of the sandboxed document parser (DOC_PARSE_*)."""

    timeout_seconds: float
    max_output_bytes: int
    memory_limit_mb: int
    cpu_seconds: int

    def limits(self) -> sandbox.SandboxLimits:
        return sandbox.SandboxLimits(
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            memory_limit_bytes=self.memory_limit_mb * 1024 * 1024,
            cpu_seconds=self.cpu_seconds,
        )


@dataclass(frozen=True)
class VersionInfo:
    """A snapshot of the version row (and its document's subject) taken when the job starts."""

    id: uuid.UUID
    document_id: uuid.UUID
    subject_id: uuid.UUID
    file_kind: str
    storage_key: str
    size_bytes: int


@dataclass
class StageContext:
    job: "Job"
    version: VersionInfo
    limits: DocumentLimits
    config: "IngestSettings"
    store: document_store.PrivateObjectStore
    tenant_sessions: TenantSessions
    # Opens one transaction with the tenant set and the version still active (set by the worker)
    _session: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    results: dict[str, Any] = field(default_factory=dict)

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """One tenant transaction. Raises JobStopped when the version is no longer queued/processing or the job is not this worker's."""
        return self._session()


Stage = Callable[[StageContext], Awaitable[None]]


@dataclass(frozen=True)
class IngestStages:
    validate: Stage
    parse: Stage
    chunk: Stage
    embed: Stage
    index: Stage

    def ordered(self) -> list[tuple[str, Stage]]:
        return [(name, getattr(self, name)) for name in STAGE_NAMES]


# ── Sandboxed validation (backlog B37) ───────────────────────────────────────


@dataclass(frozen=True)
class SandboxValidation:
    file_kind: str
    page_count: int | None  # PDF: uncompressed page objects the scan saw; PPTX: slides; else None


async def validate_in_sandbox(
    data: bytes,
    *,
    file_kind: str,
    limits: DocumentLimits,
    parse: ParseSettings,
    child_module: str = DOCUMENT_PARSE_CHILD,
    child_args: tuple[str, ...] = (),
    extra_params: Mapping[str, Any] | None = None,
) -> SandboxValidation:
    """
    Run the shared upload validation (DOCUMENT profile, including the PDF byte scan) on `data` in the
    killable sandbox child. Sandbox and child failures become worker errors:

      timeout, oversized output, a validation code     -> PermanentIngestError
      a crash or unreadable output (worker_crash)      -> TransientIngestError
    `child_module`, `child_args` and `extra_params` exist so tests can run a test child.
    """
    params: dict[str, Any] = {
        "file_kind": file_kind,
        "max_file_bytes": limits.max_file_bytes,
        "max_pdf_pages": limits.max_pdf_pages,
        "max_pptx_slides": limits.max_pptx_slides,
        **(extra_params or {}),
    }
    try:
        raw = await sandbox.run_child(
            child_module,
            data,
            args=child_args,
            params=params,
            limits=parse.limits(),
            log_event="document_child_abnormal_exit",
        )
        result = sandbox.parse_child_json(raw)
    except sandbox.SandboxTimeout:
        raise errors.PermanentIngestError("parse_timeout") from None
    except sandbox.SandboxOutputTooLarge:
        raise errors.PermanentIngestError("parse_output_too_large") from None
    except sandbox.SandboxCrash:
        raise errors.TransientIngestError("worker_crash") from None
    if not result.get("success"):
        # The child's own text is ignored: only a known code is kept (anything else is parse_failed)
        raise errors.PermanentIngestError(str(result.get("error_code", "parse_failed")))
    pages = result.get("page_count")
    return SandboxValidation(
        file_kind=str(result.get("file_kind", file_kind)),
        page_count=pages if isinstance(pages, int) and not isinstance(pages, bool) else None,
    )


def make_validate_stage(store: document_store.PrivateObjectStore, parse: ParseSettings) -> Stage:
    """
    The real `validate` stage: read the stored file and re-run validation in the sandbox. A missing
    file is permanent (`processing_failed`); the page count goes to ctx.results["validation"] for the
    later stages (and for the quota re-check of the hooks, see the module note on B39).
    """

    async def validate(ctx: StageContext) -> None:
        try:
            data = await store.get(ctx.job.tenant_id, ctx.version.storage_key)
        except document_store.InvalidStorageKey:
            raise errors.PermanentIngestError("processing_failed") from None
        if data is None:
            raise errors.PermanentIngestError("processing_failed")
        ctx.results["validation"] = await validate_in_sandbox(
            data, file_kind=ctx.version.file_kind, limits=ctx.limits, parse=parse
        )

    return validate


# ── Tenant transaction used by the worker ────────────────────────────────────

# The job row of a version, locked FOR SHARE, only while it is still THIS worker's: running, with this
# worker's id and this claim's attempt number (the same guard as the queue's own statements).
OWNERSHIP_SQL = """
SELECT 1 FROM app.ingest_jobs
WHERE tenant_id = :t AND version_id = :v AND status = 'running' AND worker_id = :w AND attempts = :a
FOR SHARE
"""


async def lock_job_ownership(db: AsyncSession, job: "Job") -> None:
    """
    Raise JobStopped("job_lost") unless the job row is still this worker's, and lock the row for the rest
    of the transaction. The lock closes the gap between check and write: while it is held no other worker
    can reclaim the job (the claim uses FOR UPDATE SKIP LOCKED, so it skips the row) and the sweeper and
    the delete route wait. A worker whose lease expired and whose job was reclaimed finds its claim
    (worker id, attempts) gone and writes nothing. Every write of version, chunk or job state by the worker
    calls this first, inside the SAME transaction as the write. Lock order everywhere: version row, then job row.
    """
    owned = (
        await db.execute(
            text(OWNERSHIP_SQL),
            {"t": job.tenant_id, "v": job.version_id, "w": job.worker_id, "a": job.attempts},
        )
    ).first()
    if owned is None:
        raise errors.JobStopped("job_lost")


@asynccontextmanager
async def active_version_session(
    tenant_sessions: TenantSessions, job: "Job"
) -> AsyncIterator[AsyncSession]:
    """
    One transaction for a job: the tenant is set from the claimed row first, then the version row is
    locked FOR SHARE and must still be queued or processing, and the job row must still be this worker's.
    A deleted or archived version (backlog B40) raises JobStopped("cancelled") and a lost job
    (reclaimed after a lease expiry, or frozen) raises JobStopped("job_lost"), both before anything is
    written. The share locks make a concurrent delete or reclaim wait until this transaction ends.
    """
    async with tenant_sessions.for_job(job) as db:
        status = (
            await db.execute(
                text(
                    "SELECT status FROM app.document_versions "
                    "WHERE tenant_id = :t AND id = :v FOR SHARE"
                ),
                {"t": job.tenant_id, "v": job.version_id},
            )
        ).scalar_one_or_none()
        if status not in ("queued", "processing"):
            raise errors.JobStopped("cancelled")
        await lock_job_ownership(db, job)
        yield db
