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
  5. purge finished jobs older than INGEST_FINISHED_JOB_RETENTION_DAYS (queue.purge_finished);
  6. per tenant, only when a ChatMaintenance object is given (step 6a-12b, plan 9.4; CHAT_MAINT_DATABASE_URL is set in
     this worker): mark stale `pending` answers `interrupted`, delete conversations older than the tenant's retention
     and old usage events, blank the citations of purged documents from the outbox queue, and write the audit rows.
     Everything that touches chat rows goes through the app_chat_maint connection of chat_maintenance.py; the retention
     value and the audit rows go through a tenant session (that role has no grant on tenant_settings or audit_log).
     A tenant whose retention is NULL (the default) loses nothing: no conversation and no usage event is deleted.

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

from app.services import chat_maintenance, chat_settings, document_store, document_upload
from app.services.audit import record_audit_event
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
    stale_marked: int = 0
    conversations_deleted: int = 0
    messages_deleted: int = 0
    usage_events_deleted: int = 0
    blanked_documents: int = 0
    blanked_citations: int = 0


def maintenance_from_settings(s: "Settings") -> chat_maintenance.ChatMaintenance | None:
    """The chat maintenance object of this worker, or None when CHAT_MAINT_DATABASE_URL is unset (no chat maintenance
    here). Raises MaintenanceNotConfigured for a user other than app_chat_maint (the worker's start refuses it first)."""
    return chat_maintenance.ChatMaintenance.from_settings(s)


def maintenance_problems(s: "Settings") -> list[str]:
    """What stops the worker from starting because of the chat maintenance URL (empty list when it may start). An unset
    URL is not a problem: that worker simply does no chat maintenance. Never echoes the URL or a password."""
    url = s.CHAT_MAINT_DATABASE_URL.get_secret_value().strip()
    if not url:
        return []
    try:
        chat_maintenance.check_url_user(url)
    except chat_maintenance.MaintenanceNotConfigured as exc:
        return [str(exc)]
    return []


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
        maintenance: chat_maintenance.ChatMaintenance | None = None,
    ) -> None:
        self.maintenance = maintenance
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
            if self.maintenance is not None:
                try:  # after the document tasks, so the queue rows of a purge made in this run are drained in it
                    await self._chat_maintenance(ref, self.maintenance, report)
                except Exception as exc:
                    logger.error("sweeper_chat_maintenance_failed error=%s", type(exc).__name__)

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
            # One blank-queue row per purged document, in this transaction (plan 9.4, C21)
            await document_upload.schedule_citation_blanking(db, ref.tenant_id, list(ids))
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

    # ── Chat maintenance (step 6a-12b) ───────────────────────────────────────

    async def _read_retention(self, ref: TenantRef) -> int | None:
        """The tenant's chat retention through a tenant session: None (no row, or NULL) means keep everything."""
        async with self.tenant_sessions.for_job(ref) as db:
            return (await chat_settings.read_effective(db, ref.tenant_id)).retention_days

    async def _chat_maintenance(
        self, ref: TenantRef, maint: chat_maintenance.ChatMaintenance, report: SweepReport
    ) -> None:
        retention_days = await self._read_retention(ref)
        stale = 0
        deleted = chat_maintenance.RetentionCounts()
        blanked: list[chat_maintenance.BlankedDocument] = []
        try:
            stale = await maint.mark_stale_pending(ref)
            try:
                deleted = await maint.delete_expired(ref, retention_days)
            except chat_maintenance.RetentionInterrupted as exc:
                deleted = exc.counts  # the batches that did commit are still counted and audited
                raise
            blanked = await maint.drain_blank_queue(ref)
        finally:
            report.stale_marked += stale
            report.conversations_deleted += deleted.conversations
            report.messages_deleted += deleted.messages
            report.usage_events_deleted += deleted.usage_events
            report.blanked_documents += len(blanked)
            report.blanked_citations += sum(b.citations for b in blanked)
            await self._audit_chat_maintenance(ref, retention_days, stale, deleted, blanked)

    async def _audit_chat_maintenance(
        self, ref: TenantRef, retention_days: int | None, stale: int, deleted: chat_maintenance.RetentionCounts,
        blanked: list[chat_maintenance.BlankedDocument],
    ) -> None:
        """Counts only, never text (plan 9.4, 9.6). Written in a separate tenant session: app_chat_maint has no grant on
        audit_log. Nothing is written for a run that did nothing."""
        if not (stale or deleted.any or blanked):
            return
        async with self.tenant_sessions.for_job(ref) as db:
            if stale or deleted.any:
                await record_audit_event(
                    tenant_id=ref.tenant_id, action="chat:retention_purge", target_type="tenant",
                    target_id=str(ref.tenant_id),
                    details={
                        "retention_days": retention_days, "conversations": deleted.conversations,
                        "messages": deleted.messages, "usage_events": deleted.usage_events,
                        "stale_pending_marked": stale,
                    },
                    actor_id=None, db=db,
                )
            for item in blanked:
                await record_audit_event(
                    tenant_id=ref.tenant_id, action="chat:citation_blank", target_type="document",
                    target_id=str(item.document_id), details={"citations": item.citations}, actor_id=None, db=db,
                )

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
                    "sweeper_run failed_jobs=%d recreated_jobs=%d files_deleted=%d tombstones=%d jobs_purged=%d "
                    "stale_marked=%d conversations_deleted=%d messages_deleted=%d usage_deleted=%d blanked_documents=%d",
                    report.failed_jobs, report.recreated_jobs, report.files_deleted,
                    report.tombstones_purged, report.jobs_purged, report.stale_marked, report.conversations_deleted,
                    report.messages_deleted, report.usage_events_deleted, report.blanked_documents,
                )
        except Exception as exc:
            logger.error("sweeper_run_failed error=%s", type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), sweeper.config.interval_seconds)
