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
count found by parsing belongs to the stage hooks, not to the worker. The parse stage (5b-3) re-counts
pages with the parser (B33) in the child AND in the parent and raises `too_many_pages` /
`too_many_slides`; `check_pages_quota` and `make_pages_quota_hook` give an early `quota_pages_exceeded`;
the binding check is the `index` stage of 5c, inside its single transaction (design A3 step 8).

The real stages here: `make_validate_stage` (5b-2, re-runs the shared upload validation inside the
sandbox, backlog B37) and `make_parse_stage` (5b-3, real text extraction in the sandbox; the child's
answer is untrusted and validated in this process against app/services/parsed_document.py).
"""

import asyncio
import logging
import re
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
from app.services.parsed_document import (
    DEFAULT_MAX_TEXT_CHARS,
    MAX_JSON_DEPTH,
    MAX_JSON_STRUCTURE,
    ParsedDocument,
    ParsedDocumentError,
    validate_parsed_document,
)
from app.workers import errors
from app.workers.db import TenantSessions

if TYPE_CHECKING:
    from app.workers.ingest import IngestSettings
    from app.workers.queue import Job

logger = logging.getLogger(__name__)

DOCUMENT_PARSE_CHILD = "app.services.document_parse_child"
# What a legitimate answer of the document child can never exceed (parsed_document.py explains the numbers)
JSON_LIMITS = sandbox.JsonLimits(max_structure=MAX_JSON_STRUCTURE, max_depth=MAX_JSON_DEPTH)
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,60}")
SOFT_DEADLINE_SHARE = 0.8  # share of the wall clock the child may use before it gives up on its own

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
    """Limits of the sandboxed document parser (DOC_PARSE_*, DOC_MAX_TEXT_CHARS)."""

    timeout_seconds: float
    max_output_bytes: int
    memory_limit_mb: int
    cpu_seconds: int
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS

    @property
    def time_budget_seconds(self) -> float:
        """The child's own soft deadline: it answers parse_timeout before the wall clock kills it."""
        return self.timeout_seconds * SOFT_DEADLINE_SHARE

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
    result = await _run_document_child(data, params, parse, child_module, child_args)
    pages = result.get("page_count")
    return SandboxValidation(
        file_kind=str(result.get("file_kind", file_kind)),
        page_count=pages if isinstance(pages, int) and not isinstance(pages, bool) else None,
    )


async def _run_document_child(
    data: bytes, params: dict[str, Any], parse: ParseSettings, child_module: str, child_args: tuple[str, ...]
) -> dict[str, Any]:
    """
    One sandboxed child run, mapped to worker errors. Returns the child's object only when it says
    success; its other content is still untrusted (callers validate it).

      timeout, oversized output, a child failure code -> PermanentIngestError
      a crash or unreadable output (worker_crash)      -> TransientIngestError
    """
    try:
        raw = await sandbox.run_child(
            child_module,
            data,
            args=child_args,
            params=params,
            limits=parse.limits(),
            log_event="document_child_abnormal_exit",
        )
        # In a thread: the strict parse of a 12 MB answer takes a moment and must not stall the heartbeat
        result = await asyncio.to_thread(sandbox.parse_child_json_strict, raw, limits=JSON_LIMITS)
    except sandbox.SandboxTimeout:
        raise errors.PermanentIngestError("parse_timeout") from None
    except sandbox.SandboxOutputTooLarge:
        raise errors.PermanentIngestError("parse_output_too_large") from None
    except sandbox.SandboxOutputInvalid:
        raise errors.PermanentIngestError("parse_output_invalid") from None
    except sandbox.SandboxCrash:
        raise errors.TransientIngestError("worker_crash") from None
    if result.get("success") is not True:
        # The child's own text is ignored: only a known code is kept (anything else is parse_failed)
        code = str(result.get("error_code", "parse_failed"))
        error_type = result.get("error_type")
        if code == "parse_failed" and isinstance(error_type, str) and _CLASS_NAME.fullmatch(error_type):
            # The only thing logged about an unexpected exception in the child: its class name
            logger.warning("document_child_unexpected_error error_type=%s", error_type)
        raise errors.PermanentIngestError(code)
    return result


async def _load_original(store: document_store.PrivateObjectStore, ctx: StageContext) -> bytes:
    """The stored original, through the store's tenant-prefix check. Missing or foreign: permanent."""
    try:
        data = await store.get(ctx.job.tenant_id, ctx.version.storage_key)
    except document_store.InvalidStorageKey:
        raise errors.PermanentIngestError("processing_failed") from None
    if data is None:
        raise errors.PermanentIngestError("processing_failed")
    return data


def make_validate_stage(store: document_store.PrivateObjectStore, parse: ParseSettings) -> Stage:
    """
    The real `validate` stage: read the stored file and re-run validation in the sandbox. A missing
    file is permanent (`processing_failed`); the page count goes to ctx.results["validation"] for the
    later stages (and for the quota re-check of the hooks, see the module note on B39).
    """

    async def validate(ctx: StageContext) -> None:
        data = await _load_original(store, ctx)
        ctx.results["validation"] = await validate_in_sandbox(
            data, file_kind=ctx.version.file_kind, limits=ctx.limits, parse=parse
        )

    return validate


# ── Sandboxed parsing (step 5b-3) ────────────────────────────────────────────


@dataclass(frozen=True)
class ChildParse:
    document: ParsedDocument
    peak_memory_kb: int | None  # the child's own peak resident memory (for benchmarks), None if unknown


def page_cap(file_kind: str, limits: DocumentLimits) -> int:
    """The tenant's page cap for a kind: PDF pages, PPTX slides; flow formats have one page."""
    if file_kind == "pdf":
        return limits.max_pdf_pages
    if file_kind == "pptx":
        return limits.max_pptx_slides
    return 1


def check_page_limits(parsed: ParsedDocument, limits: DocumentLimits) -> None:
    """The parent's own re-check of the page (slide) count against the tenant's caps (B33, B39)."""
    if parsed.page_count > page_cap(parsed.file_kind, limits):
        raise errors.PermanentIngestError("too_many_slides" if parsed.file_kind == "pptx" else "too_many_pages")


async def run_parse_child(
    data: bytes,
    *,
    file_kind: str,
    limits: DocumentLimits,
    parse: ParseSettings,
    child_module: str = DOCUMENT_PARSE_CHILD,
    child_args: tuple[str, ...] = (),
    extra_params: Mapping[str, Any] | None = None,
) -> ChildParse:
    """
    Parse `data` in the sandbox child with the tenant's caps and VALIDATE the answer here, in the
    parent: the child is untrusted (parsed_document.validate_parsed_document, then the page caps).
    A rejected answer is permanent: parse_output_invalid, parse_output_too_large or text_too_large.
    """
    params: dict[str, Any] = {
        "mode": "parse",
        "file_kind": file_kind,
        "max_file_bytes": limits.max_file_bytes,
        "max_pdf_pages": limits.max_pdf_pages,
        "max_pptx_slides": limits.max_pptx_slides,
        "max_text_chars": parse.max_text_chars,
        "time_budget_seconds": parse.time_budget_seconds,
        **(extra_params or {}),
    }
    result = await _run_document_child(data, params, parse, child_module, child_args)
    try:
        document = validate_parsed_document(
            result.get("document"),
            max_pages=page_cap(file_kind, limits),
            max_text_chars=parse.max_text_chars,
            expected_kind=file_kind,
        )
    except ParsedDocumentError as exc:
        raise errors.PermanentIngestError(exc.code) from None
    check_page_limits(document, limits)
    peak = result.get("peak_memory_kb")
    return ChildParse(
        document=document,
        peak_memory_kb=peak if isinstance(peak, int) and not isinstance(peak, bool) and peak >= 0 else None,
    )


async def parse_in_sandbox(
    data: bytes,
    *,
    file_kind: str,
    limits: DocumentLimits,
    parse: ParseSettings,
    child_module: str = DOCUMENT_PARSE_CHILD,
    child_args: tuple[str, ...] = (),
    extra_params: Mapping[str, Any] | None = None,
) -> ParsedDocument:
    """run_parse_child, returning the validated document only."""
    outcome = await run_parse_child(
        data, file_kind=file_kind, limits=limits, parse=parse, child_module=child_module,
        child_args=child_args, extra_params=extra_params,
    )
    return outcome.document


# Called after a successful parse with the validated document (backlog B39). Wired in 5c.
PagesQuotaHook = Callable[[StageContext, ParsedDocument], Awaitable[None]]


def make_parse_stage(
    store: document_store.PrivateObjectStore,
    parse: ParseSettings,
    *,
    pages_quota_hook: PagesQuotaHook | None = None,
    child_module: str = DOCUMENT_PARSE_CHILD,
    child_args: tuple[str, ...] = (),
) -> Stage:
    """
    The real `parse` stage: read the original from the private store (tenant-prefix check), parse it
    in the sandbox with the tenant's caps, validate the answer in the parent, then run the pages-quota
    hook if one is given. The result goes to ctx.results["parsed"] (a ParsedDocument) and the child's
    peak memory to ctx.results["parse_peak_memory_kb"]. Nothing is logged: no file name, content or path.
    """

    async def parse_stage(ctx: StageContext) -> None:
        data = await _load_original(store, ctx)
        outcome = await run_parse_child(
            data, file_kind=ctx.version.file_kind, limits=ctx.limits, parse=parse,
            child_module=child_module, child_args=child_args,
        )
        if pages_quota_hook is not None:
            await pages_quota_hook(ctx, outcome.document)
        ctx.results["parsed"] = outcome.document
        ctx.results["parse_peak_memory_kb"] = outcome.peak_memory_kb

    return parse_stage


# ── The pages quota re-check (backlog B39) ───────────────────────────────────


def check_pages_quota(limits: DocumentLimits, *, used_pages: int, new_pages: int) -> None:
    """Permanent quota_pages_exceeded when the new version would take the tenant over max_pages.
    A total exactly at the limit is allowed (the same rule as the upload check)."""
    if used_pages + new_pages > limits.max_pages:
        raise errors.PermanentIngestError("quota_pages_exceeded")


PAGES_USED_SQL = """
SELECT COALESCE(SUM(v.page_count), 0)
FROM app.document_versions v
JOIN app.documents d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
WHERE v.tenant_id = :t AND d.deleted_at IS NULL AND v.id <> :v AND v.status = ANY(:statuses)
"""


async def pages_used_by_others(db: AsyncSession, tenant_id: uuid.UUID, version_id: uuid.UUID) -> int:
    """Pages the tenant already uses, without this version (the same statuses as the upload quota)."""
    from app.services.document_upload import PAGE_COUNTING_STATUSES

    used = (
        await db.execute(
            text(PAGES_USED_SQL), {"t": tenant_id, "v": version_id, "statuses": list(PAGE_COUNTING_STATUSES)}
        )
    ).scalar_one()
    return int(used or 0)


def make_pages_quota_hook() -> PagesQuotaHook:
    """
    The default hook (wired in 5c): counts the pages under the tenant's quota lock in one tenant
    transaction and refuses with quota_pages_exceeded. This is an EARLY refusal only: the binding
    check is the one in the 5c index transaction, which also writes page_count (design A3 step 8).
    """

    async def hook(ctx: StageContext, parsed: ParsedDocument) -> None:
        from app.services.document_upload import lock_quota

        async with ctx.session() as db:
            await lock_quota(db, ctx.job.tenant_id)
            used = await pages_used_by_others(db, ctx.job.tenant_id, ctx.version.id)
        check_pages_quota(ctx.limits, used_pages=used, new_pages=parsed.page_count)

    return hook


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
