"""
app/workers/ingest.py
─────────────────────
The document ingestion worker (design A3, "Worker job", "Failures, retries and the sweeper").

One job, start to finish (`ingest_document_version`):

  1. the job was CLAIMED by workers/queue.py (role app_worker, job row only);
  2. both slots are taken through the SlotManager: `concurrency:doc_ingest:global`
     (INGEST_GLOBAL_CONCURRENCY) and `concurrency:doc_ingest:tenant:{id}` (INGEST_TENANT_CONCURRENCY),
     TTL INGEST_LEASE_SECONDS. A busy slot sends the job back to `queued` (run_after = now + 15-45 s
     jitter, attempt refunded) before anything else is touched. So does an UNAVAILABLE backend at this
     point (Redis down: the slots cannot be taken; the database down when the job starts): an outage is
     not the job's fault, so it never uses up attempts (outcome BACKEND_DOWN);
  3. in a TENANT session (tenant from the claimed row) the version moves to `processing`;
  4. a heartbeat task renews the job lease and both slots every INGEST_HEARTBEAT_SECONDS. The stages are
     CANCELLED (the sandbox kills its child) and the job is released, never continued, when
       - the slot lease is lost (expired or released): requeued with backoff (attempt counted);
       - Redis stays unavailable for HEARTBEAT_MAX_BACKEND_FAILURES renewals in a row (one slow or
         failed call alone is not a reason to stop): requeued with backoff;
       - the lease reached its maximum lifetime (INGEST_MAX_JOB_SECONDS): a PERMANENT failure with the
         code `job_time_limit_exceeded`, because a retry would only run into the same cap;
       - the job row is no longer this worker's: nothing is written;
  5. the injectable stages run in order (workers/stages.py): validate, parse, chunk, embed, index;
  6. the job is marked `done`.

Failures: a PermanentIngestError fails the version (error_code) and the job; a TransientIngestError
(or any unexpected exception, counted as worker_crash) requeues the job with 30*2^(n-1) s + 0..15 s
jitter until INGEST_MAX_ATTEMPTS, then the version and the job fail with `processing_failed`.
A job whose version was deleted or archived (backlog B40), or whose row was frozen, is CANCELLED and
writes nothing more. Slots are always released (also on SIGTERM and on failures).

Ownership: every write of version, chunk or job state by the worker runs in a transaction that first
locks the job row and checks that it is still this worker's (`stages.lock_job_ownership`: running, this
worker id, this attempt). A worker whose lease expired and whose job another worker reclaimed therefore
raises JobStopped("job_lost") at its next write and changes nothing, so it can neither overwrite the new
owner's chunks nor mark the version ready, failed or queued.

Deviation from the written order (documented in docs/phase5-design.md): the slots are taken BEFORE the
version moves to `processing`, so a busy slot never makes a version flap between processing and queued.

`python -m app.workers.ingest` REFUSES to start until the real stages are assembled in step 5c: a
half pipeline would leave versions stuck in `processing`.
"""

import asyncio
import contextlib
import enum
import logging
import random
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from app.core import sandbox_child
from app.core.slots import (
    RenewResult,
    SlotBackendUnavailable,
    SlotLease,
    SlotLimit,
    SlotManager,
    SlotRejected,
)
from app.services import document_limits, document_store
from app.workers import errors
from app.workers import stages as stage_module
from app.workers.db import TenantSessions, WorkerDatabases
from app.workers.queue import Job, JobQueue
from app.workers.stages import (
    STAGE_COLUMN_VALUE,
    IngestStages,
    ParseSettings,
    StageContext,
    VersionInfo,
    active_version_session,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

GLOBAL_SLOT_KEY = "concurrency:doc_ingest:global"
TENANT_SLOT_PREFIX = "concurrency:doc_ingest:tenant:"
SLOT_UNAVAILABLE_MESSAGE = "Document processing is temporarily unavailable."

# The heartbeat gives up on Redis after this many failed renewals IN A ROW. With the production values
# (a renewal every 30 s, a 120 s lease) the third failure comes 90 s after the last good renewal, while
# the slots are still alive.
HEARTBEAT_MAX_BACKEND_FAILURES = 3
STOP_SLOT_LEASE_LOST = "slot_lease_lost"
STOP_REDIS_UNAVAILABLE = "redis_unavailable"
STOP_MAX_LIFETIME = "max_job_lifetime"


def tenant_slot_key(tenant_id: uuid.UUID) -> str:
    return f"{TENANT_SLOT_PREFIX}{tenant_id}"


@dataclass(frozen=True)
class IngestSettings:
    global_concurrency: int
    tenant_concurrency: int
    lease_seconds: float
    heartbeat_seconds: float
    max_job_seconds: float
    max_attempts: int
    retry_base_seconds: float
    retry_jitter_seconds: float
    busy_min_seconds: float
    busy_max_seconds: float
    poll_seconds: float
    shutdown_grace_seconds: float
    parse: ParseSettings
    # worker_crash (a dead parser child, or a bug in a stage) is retried, but only this many attempts in all
    max_crash_attempts: int = 2

    @classmethod
    def from_settings(cls, s: "Settings") -> "IngestSettings":
        return cls(
            global_concurrency=s.INGEST_GLOBAL_CONCURRENCY,
            tenant_concurrency=s.INGEST_TENANT_CONCURRENCY,
            lease_seconds=float(s.INGEST_LEASE_SECONDS),
            heartbeat_seconds=float(s.INGEST_HEARTBEAT_SECONDS),
            max_job_seconds=float(s.INGEST_MAX_JOB_SECONDS),
            max_attempts=s.INGEST_MAX_ATTEMPTS,
            retry_base_seconds=float(s.INGEST_RETRY_BASE_SECONDS),
            retry_jitter_seconds=float(s.INGEST_RETRY_JITTER_SECONDS),
            busy_min_seconds=float(s.INGEST_BUSY_MIN_SECONDS),
            busy_max_seconds=float(s.INGEST_BUSY_MAX_SECONDS),
            poll_seconds=float(s.WORKER_POLL_SECONDS),
            shutdown_grace_seconds=float(s.INGEST_SHUTDOWN_GRACE_SECONDS),
            parse=ParseSettings(
                timeout_seconds=float(s.DOC_PARSE_TIMEOUT_SECONDS),
                max_output_bytes=s.DOC_PARSE_MAX_OUTPUT_BYTES,
                memory_limit_mb=s.DOC_PARSE_MEMORY_LIMIT_MB,
                cpu_seconds=s.DOC_PARSE_CPU_SECONDS,
                max_text_chars=s.DOC_MAX_TEXT_CHARS,
            ),
            max_crash_attempts=s.INGEST_MAX_CRASH_ATTEMPTS,
        )


class JobOutcome(enum.Enum):
    DONE = "done"
    BUSY = "busy"  # a slot was full: requeued, attempt refunded
    RETRY = "retry"  # transient error: requeued with backoff
    FAILED = "failed"  # permanent error (also the maximum job lifetime), or attempts exhausted
    STOPPED = "stopped"  # heartbeat lost a slot or Redis: released and requeued
    BACKEND_DOWN = "backend_down"  # Redis or the database unavailable at the start: requeued, attempt refunded
    CANCELLED = "cancelled"  # the version or the job row was taken away (document deleted)


@dataclass
class WorkerContext:
    queue: JobQueue
    tenant_sessions: TenantSessions
    store: document_store.PrivateObjectStore
    slots: SlotManager
    config: IngestSettings
    rng: random.Random
    worker_id: str
    clock: Callable[[], datetime] | None = None  # None: the database's now()
    # The heartbeat's wait between two renewals. Tests inject a manual tick instead of real time.
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


# ── Timing ───────────────────────────────────────────────────────────────────


def retry_delay_seconds(attempt: int, rng: random.Random, config: IngestSettings) -> float:
    """30 * 2^(attempt-1) seconds plus 0..15 s of jitter (design A3)."""
    return float(config.retry_base_seconds * 2 ** (attempt - 1) + rng.uniform(0.0, config.retry_jitter_seconds))


def busy_delay_seconds(rng: random.Random, config: IngestSettings) -> float:
    return rng.uniform(config.busy_min_seconds, config.busy_max_seconds)


def poll_delay_seconds(rng: random.Random, config: IngestSettings) -> float:
    """The idle sleep: WORKER_POLL_SECONDS with +-20% jitter."""
    return config.poll_seconds * rng.uniform(0.8, 1.2)


# ── Version writes (tenant sessions) ─────────────────────────────────────────

_ACTIVE = "status IN ('queued', 'processing')"


async def _begin(ctx: WorkerContext, job: Job) -> tuple[VersionInfo, document_limits.DocumentLimits]:
    """Move the version to processing and read what the stages need. Cancelled if it is not active."""
    async with ctx.tenant_sessions.for_job(job) as db:
        row = (
            await db.execute(
                text(
                    "SELECT v.id, v.document_id, d.subject_id, v.file_kind, v.storage_key, v.size_bytes, v.status "
                    "FROM app.document_versions v JOIN app.documents d "
                    "  ON d.tenant_id = v.tenant_id AND d.id = v.document_id "
                    "WHERE v.tenant_id = :t AND v.id = :v FOR UPDATE OF v"
                ),
                {"t": job.tenant_id, "v": job.version_id},
            )
        ).first()
        if row is None or row[6] not in ("queued", "processing"):
            raise errors.JobStopped("cancelled")
        await stage_module.lock_job_ownership(db, job)
        limits = await document_limits.load_tenant_limits(db, job.tenant_id)
        await db.execute(
            text(
                "UPDATE app.document_versions SET status = 'processing', stage = 'parsing', error_code = NULL, "
                "updated_at = now() WHERE tenant_id = :t AND id = :v"
            ),
            {"t": job.tenant_id, "v": job.version_id},
        )
    info = VersionInfo(
        id=row[0], document_id=row[1], subject_id=row[2], file_kind=row[3], storage_key=row[4], size_bytes=row[5]
    )
    return info, limits


async def _set_version(ctx: WorkerContext, job: Job, status: str, error_code: str | None) -> None:
    """
    Failed or back to queued. Only a version that is still queued or processing is touched, and only
    by the worker that still owns the job: anyone else gets JobStopped("job_lost") and writes nothing.
    """
    async with ctx.tenant_sessions.for_job(job) as db:
        # Lock order is version row first, then job row (as in the delete and retry routes)
        await db.execute(
            text("SELECT 1 FROM app.document_versions WHERE tenant_id = :t AND id = :v FOR UPDATE"),
            {"t": job.tenant_id, "v": job.version_id},
        )
        await stage_module.lock_job_ownership(db, job)
        await db.execute(
            text(
                f"UPDATE app.document_versions SET status = :s, stage = NULL, error_code = :c, updated_at = now() "
                f"WHERE tenant_id = :t AND id = :v AND {_ACTIVE}"
            ),
            {"s": status, "c": error_code, "t": job.tenant_id, "v": job.version_id},
        )


async def _enter_stage(ctx: WorkerContext, job: Job, name: str) -> None:
    async with active_version_session(ctx.tenant_sessions, job) as db:
        await db.execute(
            text(
                "UPDATE app.document_versions SET stage = :s, updated_at = now() "
                "WHERE tenant_id = :t AND id = :v"
            ),
            {"s": STAGE_COLUMN_VALUE[name], "t": job.tenant_id, "v": job.version_id},
        )


# ── The job ──────────────────────────────────────────────────────────────────


async def run_one(ctx: WorkerContext, stages: IngestStages) -> JobOutcome | None:
    """Claim the next job and run it. None when nothing is claimable."""
    job = await ctx.queue.claim(ctx.worker_id)
    if job is None:
        return None
    return await ingest_document_version(job, stages, ctx)


async def ingest_document_version(job: Job, stages: IngestStages, ctx: WorkerContext) -> JobOutcome:
    """Run one claimed job. See the module docstring for the steps and the failure paths."""
    cfg = ctx.config
    lease: SlotLease | None = None
    try:
        try:
            lease = await ctx.slots.acquire(
                [
                    SlotLimit(GLOBAL_SLOT_KEY, cfg.global_concurrency, cfg.lease_seconds),
                    SlotLimit(tenant_slot_key(job.tenant_id), cfg.tenant_concurrency, cfg.lease_seconds),
                ],
                unavailable_message=SLOT_UNAVAILABLE_MESSAGE,
            )
        except SlotRejected:
            await ctx.queue.retry(
                job, delay_seconds=busy_delay_seconds(ctx.rng, cfg), error_code=None, refund_attempt=True
            )
            return JobOutcome.BUSY
        except Exception as exc:
            # Redis unavailable (SlotManager fails closed with a 503): like a busy slot, not a failed attempt
            if errors.classify(exc).code != "redis_unavailable":
                raise
            return await _on_backend_down(ctx, job, "redis_unavailable")
        try:
            info, limits = await _begin(ctx, job)
        except errors.JobStopped:
            raise
        except Exception as exc:
            if errors.classify(exc).code != "db_unavailable":
                raise
            return await _on_backend_down(ctx, job, "db_unavailable")
        return await _supervise(ctx, job, lease, stages, info, limits)
    except errors.JobStopped as stop:
        return await _on_stopped(ctx, job, stop.reason)
    except asyncio.CancelledError:
        await asyncio.shield(_release_for_shutdown(ctx, job))
        raise
    except Exception as exc:
        return await _on_error(ctx, job, errors.classify(exc))
    finally:
        if lease is not None:
            await asyncio.shield(ctx.slots.release(lease))


async def _supervise(
    ctx: WorkerContext,
    job: Job,
    lease: SlotLease,
    stages: IngestStages,
    info: VersionInfo,
    limits: document_limits.DocumentLimits,
) -> JobOutcome:
    work = asyncio.create_task(_run_stages(ctx, job, stages, info, limits))
    beat = asyncio.create_task(_heartbeat(ctx, job, lease))
    try:
        await asyncio.wait({work, beat}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        work.cancel()
        beat.cancel()
        await asyncio.gather(work, beat, return_exceptions=True)
        raise
    if work.done():
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        work.result()  # a stage error is raised here and classified by the caller
        if await ctx.queue.complete(job):
            return JobOutcome.DONE
        return JobOutcome.CANCELLED  # the job row was frozen meanwhile (document deleted)
    # The heartbeat ended first: the job or a slot is lost. Cancel the stages (the sandbox kills its
    # child), write nothing more, and release the job. It is never continued.
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    raise errors.JobStopped(beat.result())


async def _run_stages(
    ctx: WorkerContext, job: Job, stages: IngestStages, info: VersionInfo, limits: document_limits.DocumentLimits
) -> None:
    sctx = StageContext(
        job=job,
        version=info,
        limits=limits,
        config=ctx.config,
        store=ctx.store,
        tenant_sessions=ctx.tenant_sessions,
        _session=lambda: active_version_session(ctx.tenant_sessions, job),
    )
    for name, stage in stages.ordered():
        await _enter_stage(ctx, job, name)
        await stage(sctx)


async def _heartbeat(ctx: WorkerContext, job: Job, lease: SlotLease) -> str:
    """Renew the job lease and both slots until something is lost. Returns why it ended."""
    cfg = ctx.config
    redis_failures = 0
    while True:
        await ctx.sleep(cfg.heartbeat_seconds)
        try:
            renewed = await ctx.slots.renew_lease(lease, cfg.lease_seconds, max_total_seconds=cfg.max_job_seconds)
        except SlotBackendUnavailable:
            # A slow or failed Redis call says nothing about the lease: try again at the next tick,
            # and give up only when Redis stays unavailable
            redis_failures += 1
            if redis_failures >= HEARTBEAT_MAX_BACKEND_FAILURES:
                return STOP_REDIS_UNAVAILABLE
        else:
            redis_failures = 0
            if renewed is RenewResult.LEASE_LOST:
                return STOP_SLOT_LEASE_LOST
            if renewed is RenewResult.MAX_LIFETIME:
                return STOP_MAX_LIFETIME
        try:
            if not await ctx.queue.heartbeat(job):
                return "job_lost"
        except Exception:
            return "db_unavailable"


# ── Endings ──────────────────────────────────────────────────────────────────


async def _on_stopped(ctx: WorkerContext, job: Job, reason: str) -> JobOutcome:
    logger.warning("ingest_job_stopped job=%s reason=%s", job.id, reason)
    try:
        if reason in ("cancelled", "job_lost"):
            # Not this worker's job any more (or its version was deleted). Both updates are guarded, so a
            # frozen job or one reclaimed by another worker is left exactly as it is.
            await ctx.queue.fail(job, "cancelled")
            return JobOutcome.CANCELLED
        if reason == STOP_MAX_LIFETIME:
            # The job ran as long as it may: permanent, a retry would run into the same cap
            return await _fail_for_good(ctx, job, errors.JOB_TIME_LIMIT_CODE)
        return await _requeue_transient(ctx, job, reason, JobOutcome.STOPPED)
    except errors.JobStopped as lost:
        return _lost_while_ending(job, lost)


async def _on_error(ctx: WorkerContext, job: Job, err: errors.IngestError) -> JobOutcome:
    try:
        if err.permanent:
            return await _fail_for_good(ctx, job, err.code)
        return await _requeue_transient(ctx, job, err.code, JobOutcome.RETRY)
    except errors.JobStopped as lost:
        return _lost_while_ending(job, lost)


def _lost_while_ending(job: Job, lost: errors.JobStopped) -> JobOutcome:
    """The job stopped being this worker's while it was being ended: another worker owns it, write nothing."""
    logger.warning("ingest_job_lost_while_ending job=%s reason=%s", job.id, lost.reason)
    return JobOutcome.CANCELLED


async def _fail_for_good(ctx: WorkerContext, job: Job, code: str) -> JobOutcome:
    await _set_version(ctx, job, "failed", code)
    await ctx.queue.fail(job, code)
    logger.info("ingest_job_failed job=%s code=%s", job.id, code)
    return JobOutcome.FAILED


async def _on_backend_down(ctx: WorkerContext, job: Job, code: str) -> JobOutcome:
    """
    Redis or the database was unavailable when the job started. The version is untouched (still queued) and
    the attempt is handed back: a few minutes of outage must not fail versions with processing_failed. The
    job waits like a busy slot (15-45 s with jitter) and the cause is kept in last_error_code.
    """
    delay = busy_delay_seconds(ctx.rng, ctx.config)
    await ctx.queue.retry(job, delay_seconds=delay, error_code=code, refund_attempt=True)
    logger.warning("ingest_job_backend_down job=%s code=%s", job.id, code)
    return JobOutcome.BACKEND_DOWN


async def _requeue_transient(ctx: WorkerContext, job: Job, code: str, outcome: JobOutcome) -> JobOutcome:
    # An outage may last a while and says nothing about the file: it gets the full budget. A crash may be
    # the file killing its parser: it gets the smaller one. Both count the job's attempts (an earlier outage
    # retry also counts towards the crash budget, which is the conservative direction).
    budget = ctx.config.max_attempts
    if code == errors.CRASH_CODE:
        budget = min(budget, ctx.config.max_crash_attempts)
    if job.attempts >= budget:
        await _set_version(ctx, job, "failed", errors.EXHAUSTED_ERROR_CODE)
        await ctx.queue.fail(job, errors.EXHAUSTED_ERROR_CODE)
        logger.info("ingest_job_exhausted job=%s last_code=%s", job.id, code)
        return JobOutcome.FAILED
    delay = retry_delay_seconds(job.attempts, ctx.rng, ctx.config)
    await _set_version(ctx, job, "queued", None)
    await ctx.queue.retry(job, delay_seconds=delay, error_code=code)
    logger.info("ingest_job_requeued job=%s code=%s attempt=%d", job.id, code, job.attempts)
    return outcome


async def _release_for_shutdown(ctx: WorkerContext, job: Job) -> None:
    """SIGTERM: hand the job straight back (due now, attempt refunded) so another worker takes it."""
    try:
        await _set_version(ctx, job, "queued", None)
        await ctx.queue.retry(job, delay_seconds=0.0, error_code=None, refund_attempt=True)
    except errors.JobStopped:
        logger.warning("ingest_job_lost_at_shutdown job=%s", job.id)  # another worker owns it: leave it


# ── The poll loop ────────────────────────────────────────────────────────────


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def run_worker(ctx: WorkerContext, stages: IngestStages, stop: asyncio.Event) -> None:
    """
    Claim and run jobs one at a time until `stop` is set. Idle: sleep WORKER_POLL_SECONDS with jitter
    (the stop interrupts the sleep). On stop while a job runs it gets INGEST_SHUTDOWN_GRACE_SECONDS to
    finish; after that it is cancelled and released (not lost, not half done).
    """
    while not stop.is_set():
        try:
            job = await ctx.queue.claim(ctx.worker_id)
        except Exception as exc:
            logger.error("ingest_claim_failed error=%s", type(exc).__name__)
            job = None
        if job is None:
            await _sleep_or_stop(stop, poll_delay_seconds(ctx.rng, ctx.config))
            continue
        task = asyncio.create_task(ingest_document_version(job, stages, ctx))
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), ctx.config.shutdown_grace_seconds)
            except TimeoutError:
                task.cancel()
            except Exception:
                pass  # reported below
            await asyncio.gather(task, return_exceptions=True)
        if task.cancelled():
            continue
        failure = task.exception()
        if failure is not None:
            logger.error("ingest_job_crashed job=%s error=%s", job.id, type(failure).__name__)
        else:
            logger.info("ingest_job_ended job=%s outcome=%s", job.id, task.result().value)


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM and SIGINT set `stop` (finish or release the current job, then exit)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows (development only)
            signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(stop.set))


# ── The default entry point ──────────────────────────────────────────────────


class PipelineNotAssembled(RuntimeError):
    """The real stages do not exist yet (step 5c)."""


def assemble_default_stages() -> IngestStages:
    """
    The production pipeline. Until step 5c assembles the real parse, chunk, embed and index stages
    this refuses: starting with fake or missing stages would leave every version stuck in
    `processing` (or mark empty results as ready).
    """
    raise PipelineNotAssembled(
        "The ingestion worker is not started: the real pipeline stages (parse, chunk, embed, index) are "
        "assembled in step 5c. Run it with injected stages in tests only."
    )


async def _amain(stages: IngestStages) -> None:
    from app.core.config import settings
    from app.core.slots import default_manager
    from app.services.document_store import get_document_store
    from app.workers.sweeper import Sweeper, SweeperSettings, run_sweeper_loop

    config = IngestSettings.from_settings(settings)
    dbs = WorkerDatabases.from_settings()
    await dbs.verify_roles()
    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    queue = JobQueue(
        dbs.worker_engine,
        max_attempts=config.max_attempts,
        lease_seconds=config.lease_seconds,
        tenant_concurrency=config.tenant_concurrency,
    )
    store = get_document_store()
    ctx = WorkerContext(
        queue=queue,
        tenant_sessions=dbs.tenant_sessions,
        store=store,
        slots=default_manager,
        config=config,
        rng=random.Random(),
        worker_id=worker_id,
    )
    sweeper = Sweeper(
        queue=queue,
        tenant_sessions=dbs.tenant_sessions,
        store=store,
        config=SweeperSettings.from_settings(settings),
    )
    stop = asyncio.Event()
    install_signal_handlers(stop)
    sweeping = asyncio.create_task(run_sweeper_loop(sweeper, stop))
    try:
        await run_worker(ctx, stages, stop)
    finally:
        stop.set()
        await asyncio.gather(sweeping, return_exceptions=True)
        await dbs.dispose()


def main() -> int:
    # The worker's environment holds the database URLs and SECRET_KEY. Make the process non-dumpable
    # (Linux) so a compromised parser child of the same user cannot read /proc/<this pid>/environ.
    sandbox_child.set_non_dumpable()
    try:
        stages = assemble_default_stages()
    except PipelineNotAssembled as exc:
        print(str(exc), file=sys.stderr)
        return 2
    asyncio.run(_amain(stages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
