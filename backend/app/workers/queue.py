"""
app/workers/queue.py
────────────────────
The Postgres job queue (design A3): enqueue, claim, heartbeat, complete, retry, fail, sweep.

Everything here runs on the CLAIM connection (role app_worker, engine WorkerDatabases.worker_engine)
and touches `app.ingest_jobs` and `app.tenants (id)` only: app_worker has no grant on any other table.
Statements that change a job are guarded by (tenant_id, id, status = running, worker_id, attempts), so
a worker that lost its job (lease expired and reclaimed, or the document was deleted and the job
frozen by the worker_update policy) changes 0 rows and learns that from the False it gets back.

enqueue is the one exception to "claim connection": app_worker has no INSERT grant (the API inserts a
job in the same transaction as its version), so enqueue runs on a TENANT session given by the caller.
The sweeper uses it to re-create a missing job row.

Time: every statement uses `now()` of the database unless a clock is injected, in which case the
clock's value is passed as :now (COALESCE(CAST(:now AS timestamptz), now())). Tests use a fake clock.
"""

import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.workers.db import TenantRef

NOW = "COALESCE(CAST(:now AS timestamptz), now())"

# The two ways a job is claimable. Each is a separate constant so a test can remove one.
QUEUED_DUE_SQL = f"(status = 'queued' AND run_after <= {NOW})"
RECLAIM_EXPIRED_SQL = f"OR (status = 'running' AND lease_expires_at < {NOW})"

# Design A3. One short transaction: pick one due job, lock it (skipping rows other workers hold), mark
# it running with a new lease, count the attempt.
CLAIM_SQL = f"""
WITH next AS (
  SELECT tenant_id, id FROM app.ingest_jobs
  WHERE ({QUEUED_DUE_SQL}
      {RECLAIM_EXPIRED_SQL})
    AND attempts < :max_attempts
  ORDER BY run_after, created_at LIMIT 1
  FOR UPDATE SKIP LOCKED)
UPDATE app.ingest_jobs j
SET status = 'running', attempts = j.attempts + 1,
    lease_expires_at = {NOW} + make_interval(secs => :lease), worker_id = :worker, updated_at = {NOW}
FROM next WHERE j.tenant_id = next.tenant_id AND j.id = next.id
RETURNING j.id, j.tenant_id, j.version_id, j.attempts
"""

_OWNED = "tenant_id = :tenant_id AND id = :id AND status = 'running' AND worker_id = :worker AND attempts = :attempts"

HEARTBEAT_SQL = f"""
UPDATE app.ingest_jobs SET lease_expires_at = {NOW} + make_interval(secs => :lease), updated_at = {NOW}
WHERE {_OWNED}
"""

COMPLETE_SQL = f"""
UPDATE app.ingest_jobs SET status = 'done', lease_expires_at = NULL, worker_id = NULL, updated_at = {NOW}
WHERE {_OWNED}
"""

FAIL_SQL = f"""
UPDATE app.ingest_jobs SET status = 'failed', last_error_code = :code, lease_expires_at = NULL,
  worker_id = NULL, updated_at = {NOW}
WHERE {_OWNED}
"""

# run_after and updated_at come from the same :now, so run_after - updated_at is exactly the delay.
RETRY_SQL = f"""
UPDATE app.ingest_jobs SET status = 'queued', run_after = {NOW} + make_interval(secs => :delay),
  attempts = CASE WHEN :refund THEN GREATEST(attempts - 1, 0) ELSE attempts END,
  last_error_code = :code, lease_expires_at = NULL, worker_id = NULL, updated_at = {NOW}
WHERE {_OWNED}
"""

# A job that cannot run any more: out of attempts and either waiting or abandoned by a dead worker.
SWEEP_SQL = f"""
UPDATE app.ingest_jobs SET status = 'failed', last_error_code = 'processing_failed',
  lease_expires_at = NULL, worker_id = NULL, updated_at = {NOW}
WHERE attempts >= :max_attempts
  AND (status = 'queued' OR (status = 'running' AND lease_expires_at < {NOW}))
  {{tenant_filter}}
RETURNING id, tenant_id, version_id, attempts
"""

PURGE_FINISHED_SQL = f"""
DELETE FROM app.ingest_jobs
WHERE status IN ('done', 'failed') AND updated_at < {NOW} - make_interval(days => :days)
  {{tenant_filter}}
"""

LIST_TENANTS_SQL = "SELECT id FROM app.tenants {tenant_filter} ORDER BY id"

# Runs on a TENANT session (app_tenant): one job per version, never a duplicate.
ENQUEUE_SQL = """
INSERT INTO app.ingest_jobs (tenant_id, version_id, status)
VALUES (:tenant_id, :version_id, 'queued')
ON CONFLICT ON CONSTRAINT uq_ingest_jobs_version DO NOTHING
RETURNING id
"""


@dataclass(frozen=True)
class Job:
    """A claimed job. tenant_id comes from the claimed ROW and is the only source of the tenant."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    version_id: uuid.UUID
    attempts: int
    worker_id: str


def _tenant_filter(only_tenants: Collection[uuid.UUID] | None, column: str) -> str:
    return "" if only_tenants is None else f"AND {column} = ANY(:only_tenants)"


def _only_param() -> Any:
    return bindparam("only_tenants", type_=ARRAY(PG_UUID(as_uuid=True)))


class JobQueue:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_attempts: int,
        lease_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.clock = clock

    def _now(self) -> datetime | None:
        return self.clock() if self.clock is not None else None

    def claim_params(self, worker_id: str) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "lease": float(self.lease_seconds),
            "worker": worker_id,
            "now": self._now(),
        }

    def _owned(self, job: Job) -> dict[str, Any]:
        return {
            "tenant_id": job.tenant_id,
            "id": job.id,
            "worker": job.worker_id,
            "attempts": job.attempts,
            "now": self._now(),
        }

    async def _execute(self, sql: str, params: dict[str, Any], *, only: Collection[uuid.UUID] | None = None) -> Any:
        """One statement, one transaction on the claim connection. Returns rows or the row count."""
        stmt = text(sql)
        if only is not None:
            stmt = stmt.bindparams(_only_param())
            params = {**params, "only_tenants": list(only)}
        async with self.engine.begin() as conn:
            result = await conn.execute(stmt, params)
            return list(result.fetchall()) if result.returns_rows else result.rowcount

    # ── The interface (design A3) ────────────────────────────────────────────

    @staticmethod
    async def enqueue(db: AsyncSession, tenant: Job | TenantRef, version_id: uuid.UUID) -> bool:
        """
        Create the job of a version on a TENANT session (app_worker cannot insert). False when the
        version already has one. The tenant comes from the Job or TenantRef, like the session's own.
        """
        row = (
            await db.execute(text(ENQUEUE_SQL), {"tenant_id": tenant.tenant_id, "version_id": version_id})
        ).first()
        return row is not None

    async def claim(self, worker_id: str) -> Job | None:
        rows = await self._execute(CLAIM_SQL, self.claim_params(worker_id))
        if not rows:
            return None
        row = rows[0]
        return Job(id=row[0], tenant_id=row[1], version_id=row[2], attempts=row[3], worker_id=worker_id)

    async def heartbeat(self, job: Job) -> bool:
        """Extend the lease. False when the job is no longer this worker's (stop working on it)."""
        return bool(await self._execute(HEARTBEAT_SQL, {**self._owned(job), "lease": float(self.lease_seconds)}))

    async def complete(self, job: Job) -> bool:
        return bool(await self._execute(COMPLETE_SQL, self._owned(job)))

    async def fail(self, job: Job, error_code: str) -> bool:
        return bool(await self._execute(FAIL_SQL, {**self._owned(job), "code": error_code}))

    async def retry(
        self, job: Job, *, delay_seconds: float, error_code: str | None, refund_attempt: bool = False
    ) -> bool:
        """Back to queued, due in `delay_seconds`. `refund_attempt` hands the attempt back (busy slot, shutdown)."""
        return bool(
            await self._execute(
                RETRY_SQL,
                {**self._owned(job), "delay": float(delay_seconds), "refund": refund_attempt, "code": error_code},
            )
        )

    async def sweep(self, *, only_tenants: Collection[uuid.UUID] | None = None) -> list[Job]:
        """Fail jobs that ran out of attempts. Returns them so their versions can be failed too."""
        rows = await self._execute(
            SWEEP_SQL.format(tenant_filter=_tenant_filter(only_tenants, "tenant_id")),
            {"max_attempts": self.max_attempts, "now": self._now()},
            only=only_tenants,
        )
        return [Job(id=r[0], tenant_id=r[1], version_id=r[2], attempts=r[3], worker_id="") for r in rows]

    # ── Sweeper helpers on the same connection ───────────────────────────────

    async def purge_finished(self, *, days: int, only_tenants: Collection[uuid.UUID] | None = None) -> int:
        return int(
            await self._execute(
                PURGE_FINISHED_SQL.format(tenant_filter=_tenant_filter(only_tenants, "tenant_id")),
                {"days": days, "now": self._now()},
                only=only_tenants,
            )
        )

    async def list_tenants(self, *, only_tenants: Collection[uuid.UUID] | None = None) -> list[TenantRef]:
        """Tenant ids only (the column grant). Each becomes a TenantRef, never a loose UUID."""
        sql = LIST_TENANTS_SQL.format(tenant_filter="" if only_tenants is None else "WHERE id = ANY(:only_tenants)")
        rows = await self._execute(sql, {}, only=only_tenants)
        return [TenantRef(r[0]) for r in rows]
