"""
app/workers/sweeper.py
──────────────────────
The sweeper (design A3, "Failures, retries and the sweeper"): a loop every SWEEPER_INTERVAL_SECONDS
(60 s) in the worker process, with ONE active sweeper at a time (`pg_try_advisory_lock`, a session-level
lock on a dedicated claim connection, so it needs a direct or session-mode connection like the claim).

Per run:
  1. fail jobs that ran out of attempts (queue.sweep) and mark their versions `failed` with
     `processing_failed` (tenant session);
  2. per tenant: re-create the job row of any `queued` version older than 2 minutes that has none
     (defensive; JobQueue.enqueue on a tenant session);
  3. per tenant: purge tombstones (soft-deleted documents) older than DOC_TOMBSTONE_RETENTION_DAYS, after
     putting any file they still have into the storage outbox;
  4. per tenant: drain the storage outbox (`storage_deletions`): delete the object through the private
     store, then the row. A missing file is success. A failure keeps the row, counts the attempt and
     moves `not_before` out with an exponential backoff;
  5. purge finished jobs older than INGEST_FINISHED_JOB_RETENTION_DAYS (queue.purge_finished).

Roles: app_worker (the claim connection) is used for jobs and for `SELECT id FROM app.tenants` only. Every
tenant table (document_versions, documents, chunks, storage_deletions, and the job insert) is touched
through a tenant session built from a TenantRef of the listed tenant id, never from a loose UUID.
Checked against migration 0008: app_worker has NO grant on those tables, and none is needed, so no
grant was widened.

`only_tenants` limits a run to some tenants (tests, and a manual clean-up of one tenant); in production
it is None. The orphan-file reconciliation of backlog B41 is NOT done here (it needs a listing of the
store, which the private store interface does not offer yet).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.services import document_store, document_upload
from app.workers.db import TenantRef, TenantSessions
from app.workers.queue import NOW, Job, JobQueue

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# Arbitrary constant: "ingest sweeper" in hex-ish digits. Advisory locks are per database.
SWEEPER_LOCK_KEY = 0x1_6E57_5EE9_E8


@dataclass(frozen=True)
class SweeperSettings:
    interval_seconds: float
    missing_job_after_seconds: float
    tombstone_retention_days: int
    finished_job_retention_days: int
    outbox_batch: int = 100
    outbox_backoff_base_seconds: float = 60.0
    outbox_backoff_max_seconds: float = 21_600.0

    @classmethod
    def from_settings(cls, s: "Settings") -> "SweeperSettings":
        return cls(
            interval_seconds=float(s.SWEEPER_INTERVAL_SECONDS),
            missing_job_after_seconds=float(s.SWEEPER_MISSING_JOB_AFTER_SECONDS),
            tombstone_retention_days=s.DOC_TOMBSTONE_RETENTION_DAYS,
            finished_job_retention_days=s.INGEST_FINISHED_JOB_RETENTION_DAYS,
        )


@dataclass
class SweepReport:
    ran: bool
    failed_jobs: int = 0
    recreated_jobs: int = 0
    files_deleted: int = 0
    deletions_retried: int = 0
    tombstones_purged: int = 0
    jobs_purged: int = 0


_MISSING_JOBS_SQL = f"""
SELECT v.id FROM app.document_versions v
WHERE v.tenant_id = :t AND v.status = 'queued'
  AND v.updated_at < {NOW} - make_interval(secs => :age)
  AND NOT EXISTS (SELECT 1 FROM app.ingest_jobs j WHERE j.tenant_id = v.tenant_id AND j.version_id = v.id)
ORDER BY v.updated_at LIMIT :batch
"""

_OLD_TOMBSTONES_SQL = f"""
SELECT id FROM app.documents
WHERE tenant_id = :t AND deleted_at IS NOT NULL
  AND deleted_at < {NOW} - make_interval(days => :days)
ORDER BY deleted_at LIMIT :batch FOR UPDATE SKIP LOCKED
"""

_DUE_DELETIONS_SQL = f"""
SELECT id, storage_key, attempts FROM app.storage_deletions
WHERE tenant_id = :t AND not_before <= {NOW}
ORDER BY not_before LIMIT :batch FOR UPDATE SKIP LOCKED
"""

_RETRY_DELETION_SQL = f"""
UPDATE app.storage_deletions
SET attempts = attempts + 1, not_before = {NOW} + make_interval(secs => :backoff)
WHERE tenant_id = :t AND id = :id
"""


def _uuid_array(name: str) -> "object":
    return bindparam(name, type_=ARRAY(PG_UUID(as_uuid=True)))


def outbox_backoff_seconds(attempts_so_far: int, config: SweeperSettings) -> float:
    """60 s, 120 s, 240 s ... capped at 6 hours."""
    return float(
        min(config.outbox_backoff_base_seconds * 2 ** max(attempts_so_far, 0), config.outbox_backoff_max_seconds)
    )


class Sweeper:
    def __init__(
        self,
        *,
        queue: JobQueue,
        tenant_sessions: TenantSessions,
        store: document_store.PrivateObjectStore,
        config: SweeperSettings,
        clock: Callable[[], datetime] | None = None,
        only_tenants: Collection[uuid.UUID] | None = None,
    ) -> None:
        self.queue = queue
        self.tenant_sessions = tenant_sessions
        self.store = store
        self.config = config
        self.clock = clock
        self.only_tenants = only_tenants

    def _now(self) -> datetime | None:
        return self.clock() if self.clock is not None else None

    # ── One run, guarded by the advisory lock ────────────────────────────────

    async def run_once(self) -> SweepReport:
        """One sweep, or SweepReport(ran=False) when another sweeper holds the lock."""
        async with self.queue.engine.connect() as lock_conn:
            got = (
                await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SWEEPER_LOCK_KEY})
            ).scalar_one()
            await lock_conn.commit()  # the lock is session-level: do not sit idle in a transaction
            if not got:
                return SweepReport(ran=False)
            try:
                return await self._sweep()
            finally:
                try:
                    await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SWEEPER_LOCK_KEY})
                    await lock_conn.commit()
                except BaseException as exc:
                    # Never hand a connection that may still hold the lock back to the pool. BaseException,
                    # not Exception: a cancellation that hits the unlock must discard the connection too
                    # (closing it ends the session, which releases the lock). A failed unlock does not fail
                    # the run; a cancellation is re-raised after the connection is gone.
                    await lock_conn.invalidate()
                    if not isinstance(exc, Exception):
                        raise

    async def _sweep(self) -> SweepReport:
        report = SweepReport(ran=True)
        failed = await self.queue.sweep(only_tenants=self.only_tenants)
        for job in failed:
            await self._fail_version(job)
        report.failed_jobs = len(failed)

        for ref in await self.queue.list_tenants(only_tenants=self.only_tenants):
            try:
                report.recreated_jobs += await self._recreate_jobs(ref)
                report.tombstones_purged += await self._purge_tombstones(ref)  # before the drain: files go this run
                deleted, retried = await self._drain_outbox(ref)
                report.files_deleted += deleted
                report.deletions_retried += retried
            except Exception as exc:
                # One tenant's trouble must not stop the others
                logger.error("sweeper_tenant_failed error=%s", type(exc).__name__)

        report.jobs_purged = await self.queue.purge_finished(
            days=self.config.finished_job_retention_days, only_tenants=self.only_tenants
        )
        return report

    # ── The tasks (tenant sessions only) ─────────────────────────────────────

    async def _fail_version(self, job: Job) -> None:
        async with self.tenant_sessions.for_job(job) as db:
            await db.execute(
                text(
                    "UPDATE app.document_versions SET status = 'failed', stage = NULL, "
                    "error_code = 'processing_failed', updated_at = now() "
                    "WHERE tenant_id = :t AND id = :v AND status IN ('queued', 'processing')"
                ),
                {"t": job.tenant_id, "v": job.version_id},
            )

    async def _recreate_jobs(self, ref: TenantRef) -> int:
        created = 0
        async with self.tenant_sessions.for_job(ref) as db:
            ids = (
                await db.execute(
                    text(_MISSING_JOBS_SQL),
                    {
                        "t": ref.tenant_id,
                        "age": self.config.missing_job_after_seconds,
                        "batch": self.config.outbox_batch,
                        "now": self._now(),
                    },
                )
            ).scalars().all()
            for version_id in ids:
                if await JobQueue.enqueue(db, ref, version_id):
                    created += 1
        return created

    async def _purge_tombstones(self, ref: TenantRef) -> int:
        async with self.tenant_sessions.for_job(ref) as db:
            ids = (
                await db.execute(
                    text(_OLD_TOMBSTONES_SQL),
                    {
                        "t": ref.tenant_id,
                        "days": self.config.tombstone_retention_days,
                        "batch": self.config.outbox_batch,
                        "now": self._now(),
                    },
                )
            ).scalars().all()
            if not ids:
                return 0
            keys = (
                await db.execute(
                    text("SELECT storage_key FROM app.document_versions WHERE tenant_id = :t AND document_id = ANY(:ids)").bindparams(
                        _uuid_array("ids")  # type: ignore[arg-type]
                    ),
                    {"t": ref.tenant_id, "ids": list(ids)},
                )
            ).scalars().all()
            # Any file the delete route did not already queue goes to the outbox in this transaction
            await document_upload.schedule_storage_deletions(db, ref.tenant_id, list(keys), "tombstone_purged")
            await db.execute(
                text("DELETE FROM app.documents WHERE tenant_id = :t AND id = ANY(:ids)").bindparams(
                    _uuid_array("ids")  # type: ignore[arg-type]
                ),
                {"t": ref.tenant_id, "ids": list(ids)},
            )
        return len(ids)

    async def _drain_outbox(self, ref: TenantRef) -> tuple[int, int]:
        deleted = retried = 0
        async with self.tenant_sessions.for_job(ref) as db:
            rows = (
                await db.execute(
                    text(_DUE_DELETIONS_SQL),
                    {"t": ref.tenant_id, "batch": self.config.outbox_batch, "now": self._now()},
                )
            ).all()
            for row_id, key, attempts in rows:
                try:
                    await self.store.delete(ref.tenant_id, key)
                except document_store.InvalidStorageKey:
                    # A key that can never be valid: retrying forever would help nobody
                    logger.error("sweeper_invalid_storage_key outbox_row=%s", row_id)
                    await db.execute(
                        text("DELETE FROM app.storage_deletions WHERE tenant_id = :t AND id = :id"),
                        {"t": ref.tenant_id, "id": row_id},
                    )
                except Exception as exc:
                    logger.warning("sweeper_delete_failed outbox_row=%s error=%s", row_id, type(exc).__name__)
                    await db.execute(
                        text(_RETRY_DELETION_SQL),
                        {
                            "t": ref.tenant_id,
                            "id": row_id,
                            "backoff": outbox_backoff_seconds(attempts, self.config),
                            "now": self._now(),
                        },
                    )
                    retried += 1
                else:
                    await db.execute(
                        text("DELETE FROM app.storage_deletions WHERE tenant_id = :t AND id = :id"),
                        {"t": ref.tenant_id, "id": row_id},
                    )
                    deleted += 1
        return deleted, retried


async def run_sweeper_loop(sweeper: Sweeper, stop: asyncio.Event) -> None:
    """Sweep every interval until `stop` is set. A failed run is logged (error type only) and retried."""
    while not stop.is_set():
        try:
            report = await sweeper.run_once()
            if report.ran:
                logger.info(
                    "sweeper_run failed_jobs=%d recreated_jobs=%d files_deleted=%d tombstones=%d jobs_purged=%d",
                    report.failed_jobs, report.recreated_jobs, report.files_deleted,
                    report.tombstones_purged, report.jobs_purged,
                )
        except Exception as exc:
            logger.error("sweeper_run_failed error=%s", type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), sweeper.config.interval_seconds)
