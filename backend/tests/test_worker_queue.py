"""
The Postgres job queue and the tenant session helper (step 5b-2, design A3 and C2 "Job queue and RLS").

Real database (the _test one), the real app_worker and app_tenant roles, the realistic academic world.
`quiet_queue` parks every claimable job other tests left behind, so the REAL, unfiltered claim SQL runs.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers import queue as queue_module
from app.workers.db import TenantRef, WorkerDatabases, tenant_session_for
from app.workers.queue import Job, JobQueue
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import make_document
from tests.fixtures.worker import FakeClock, chunk_count, job_row, make_queue, queued_version
from tests.security.worker_assertions import (
    StatementLog,
    assert_exactly_one_claim,
    assert_expired_lease_reclaimed,
    assert_job_session_is_scoped_to_its_tenant,
    assert_locked_job_is_skipped,
    assert_only_queue_tables,
)


async def _set(db: AsyncSession, job_id: uuid.UUID, **cols: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    await db.execute(text(f"UPDATE app.ingest_jobs SET {sets} WHERE id = :id"), {"id": job_id, **cols})
    await db.commit()


# ── enqueue ──────────────────────────────────────────────────────────────────


async def test_enqueue_runs_on_a_tenant_session_and_creates_one_job_per_version(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    await raw_db.execute(text("DELETE FROM app.ingest_jobs WHERE id = :id"), {"id": made.job.id})
    await raw_db.commit()

    ref = TenantRef(w.id)
    async with worker_dbs.tenant_sessions.for_job(ref) as db:
        assert await JobQueue.enqueue(db, ref, made.version.id) is True
    async with worker_dbs.tenant_sessions.for_job(ref) as db:
        assert await JobQueue.enqueue(db, ref, made.version.id) is False  # idempotent: one job per version
    count = (await raw_db.execute(text("SELECT count(*) FROM app.ingest_jobs WHERE version_id = :v"), {"v": made.version.id})).scalar_one()
    assert count == 1


# ── claim ────────────────────────────────────────────────────────────────────


async def test_claim_returns_a_typed_job_taken_from_the_row(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)
    job = await queue.claim("worker-1")
    assert job == Job(
        id=made.job.id, tenant_id=academic_world.a.id, version_id=made.version.id, attempts=1, worker_id="worker-1"
    )
    row = await job_row(raw_db, made.job.id)
    assert row["status"] == "running" and row["attempts"] == 1 and row["worker_id"] == "worker-1"
    left = (row["lease_expires_at"] - datetime.now(UTC)).total_seconds()
    assert 0 < left <= queue.lease_seconds + 1
    assert await queue.claim("worker-2") is None  # nothing else is claimable


async def test_claim_waits_for_run_after_and_respects_the_attempt_cap(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    later = await queued_version(raw_db, academic_world.a, tmp_path, subject="Math")
    capped = await queued_version(raw_db, academic_world.a, tmp_path, subject="Physics")
    await _set(raw_db, later.job.id, run_after=datetime.now(UTC) + timedelta(hours=1))
    await _set(raw_db, capped.job.id, attempts=3)
    queue = make_queue(worker_dbs)
    assert await queue.claim("w") is None
    # The same job becomes claimable the moment the (fake) clock passes run_after
    clock = FakeClock(datetime.now(UTC) + timedelta(hours=2))
    job = await make_queue(worker_dbs, clock=clock).claim("w")
    assert job is not None and job.id == later.job.id


async def test_two_workers_claim_concurrently_and_exactly_one_gets_the_job(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    queues = [make_queue(worker_dbs) for _ in range(6)]
    results = await asyncio.gather(*(q.claim(f"w{i}") for i, q in enumerate(queues)))
    assert_exactly_one_claim(list(results))


async def test_a_locked_job_is_skipped_not_waited_for(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path, subject="Math")
    await queued_version(raw_db, academic_world.b, tmp_path, subject="Physics")
    await assert_locked_job_is_skipped(make_queue(worker_dbs), worker_dbs.worker_engine)


async def test_expired_running_lease_is_reclaimed_and_attempts_increase(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)
    first = await queue.claim("dead-worker")
    assert first is not None and first.attempts == 1
    assert await queue.claim("other") is None  # the lease is still alive
    await _set(raw_db, made.job.id, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    job = await assert_expired_lease_reclaimed(queue, made.job.id, previous_attempts=1)
    assert job.worker_id == "reclaimer"


async def test_a_job_at_the_attempt_cap_is_not_reclaimed(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    await _set(raw_db, made.job.id, status="running", attempts=3, lease_expires_at=datetime.now(UTC) - timedelta(minutes=5))
    assert await make_queue(worker_dbs).claim("w") is None


# ── heartbeat, complete, retry, fail ─────────────────────────────────────────


async def test_heartbeat_extends_only_the_owners_lease(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)
    job = await queue.claim("owner")
    assert job is not None
    await _set(raw_db, made.job.id, lease_expires_at=datetime.now(UTC) + timedelta(seconds=1))
    assert await queue.heartbeat(job) is True
    assert (await job_row(raw_db, made.job.id))["lease_expires_at"] > datetime.now(UTC) + timedelta(seconds=2)
    impostor = Job(id=job.id, tenant_id=job.tenant_id, version_id=job.version_id, attempts=job.attempts, worker_id="impostor")
    assert await queue.heartbeat(impostor) is False
    stale = Job(id=job.id, tenant_id=job.tenant_id, version_id=job.version_id, attempts=job.attempts - 1, worker_id="owner")
    assert await queue.heartbeat(stale) is False  # an older attempt of the same worker no longer owns it


async def test_complete_retry_and_fail_are_guarded_and_finished_jobs_are_frozen(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)
    job = await queue.claim("w")
    assert job is not None
    assert await queue.complete(job) is True
    row = await job_row(raw_db, made.job.id)
    assert row["status"] == "done" and row["lease_expires_at"] is None
    # Finished jobs are frozen by the worker_update policy: every later write affects 0 rows
    assert await queue.complete(job) is False
    assert await queue.fail(job, "parse_failed") is False
    assert await queue.retry(job, delay_seconds=1.0, error_code="worker_crash") is False
    assert (await job_row(raw_db, made.job.id))["status"] == "done"


async def test_retry_requeues_with_a_delay_and_an_optional_attempt_refund(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    queue = make_queue(worker_dbs, clock=clock)
    job = await queue.claim("w")
    assert job is not None
    assert await queue.retry(job, delay_seconds=42.0, error_code="worker_crash") is True
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"], row["worker_id"], row["lease_expires_at"]) == (
        "queued", 1, "worker_crash", None, None,
    )
    assert abs((row["run_after"] - (clock.now() + timedelta(seconds=42))).total_seconds()) < 0.01
    assert await queue.claim("w") is None  # not due yet
    clock.advance(43)
    job2 = await queue.claim("w")
    assert job2 is not None and job2.attempts == 2
    assert await queue.retry(job2, delay_seconds=1.0, error_code=None, refund_attempt=True) is True
    assert (await job_row(raw_db, made.job.id))["attempts"] == 1
    clock.advance(5)
    job3 = await queue.claim("w")
    assert job3 is not None
    assert await queue.retry(job3, delay_seconds=0.0, error_code=None, refund_attempt=True) is True
    assert await queue.retry(job3, delay_seconds=0.0, error_code=None, refund_attempt=True) is False  # not running any more


async def test_fail_marks_the_job_failed_with_a_code(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)
    job = await queue.claim("w")
    assert job is not None
    assert await queue.fail(job, "scanned_pdf") is True
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"], row["lease_expires_at"]) == ("failed", "scanned_pdf", None)


async def test_sweep_fails_jobs_that_ran_out_of_attempts_and_only_those(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    dead = await queued_version(raw_db, w, tmp_path, subject="Math")
    live = await queued_version(raw_db, w, tmp_path, subject="Physics")
    retry = await queued_version(raw_db, w, tmp_path, subject="English")
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _set(raw_db, dead.job.id, status="running", attempts=3, lease_expires_at=past)
    await _set(raw_db, live.job.id, status="running", attempts=3, lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    await _set(raw_db, retry.job.id, status="running", attempts=2, lease_expires_at=past)
    failed = await make_queue(worker_dbs).sweep(only_tenants={w.id})
    assert [(j.id, j.version_id) for j in failed] == [(dead.job.id, dead.version.id)]
    assert (await job_row(raw_db, dead.job.id))["last_error_code"] == "processing_failed"
    assert (await job_row(raw_db, live.job.id))["status"] == "running"  # its worker may still finish
    assert (await job_row(raw_db, retry.job.id))["status"] == "running"  # still claimable


# ── The claim connection never touches tenant tables ─────────────────────────


def test_queue_sql_references_only_the_queue_and_the_tenant_id_list() -> None:
    source = Path(queue_module.__file__).read_text(encoding="utf-8")
    tables = set(re.findall(r"(?:FROM|UPDATE|INTO|JOIN)\s+app\.([a-z_]+)", source))
    assert tables == {"ingest_jobs", "tenants"}, tables
    assert not re.search(r"SELECT[^;]*\bFROM app\.tenants[^;]*\b(slug|db_dsn|doc_limits)", source, re.S)


async def test_list_tenants_returns_refs_built_from_ids_only(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld
) -> None:
    refs = await make_queue(worker_dbs).list_tenants()
    assert all(isinstance(r, TenantRef) for r in refs)
    assert {academic_world.a.id, academic_world.b.id} <= {r.tenant_id for r in refs}


async def test_app_worker_still_cannot_touch_tenant_tables_directly(worker_dbs: WorkerDatabases) -> None:
    for sql in (
        "SELECT 1 FROM app.documents LIMIT 1",
        "SELECT 1 FROM app.document_versions LIMIT 1",
        "SELECT 1 FROM app.chunks LIMIT 1",
        "SELECT 1 FROM app.chunk_embeddings LIMIT 1",
        "SELECT 1 FROM app.storage_deletions LIMIT 1",
        "SELECT 1 FROM app.users LIMIT 1",
        "UPDATE app.document_versions SET status = 'failed' WHERE false",
        "DELETE FROM app.storage_deletions WHERE false",
        "INSERT INTO app.ingest_jobs (tenant_id, version_id) VALUES (gen_random_uuid(), gen_random_uuid())",
    ):
        async with worker_dbs.worker_engine.connect() as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conn.execute(text(sql))
            await conn.rollback()


# ── tenant_session_for ───────────────────────────────────────────────────────


async def test_tenant_session_for_accepts_a_job_object_never_a_loose_id(worker_dbs: WorkerDatabases) -> None:
    for loose in (uuid.uuid4(), str(uuid.uuid4()), {"tenant_id": str(uuid.uuid4())}, None):
        with pytest.raises(TypeError):
            async with tenant_session_for(loose):  # type: ignore[arg-type]
                pass


async def test_the_session_tenant_comes_from_the_claimed_row_and_sees_only_that_tenant(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await queued_version(raw_db, a, tmp_path, subject="Math")
    await queued_version(raw_db, b, tmp_path, subject="Math")
    # Canary documents in both tenants
    await make_document(raw_db, a, subject="Physics", classes=("10-A",), uploader=a.t1, status="published", title="CANARY-A-DOC")
    await make_document(raw_db, b, subject="Physics", classes=("10-A",), uploader=b.t1, status="published", title="CANARY-B-DOC")
    queue = make_queue(worker_dbs)
    claimed = [await queue.claim("w1"), await queue.claim("w2")]
    assert {j.tenant_id for j in claimed if j} == {a.id, b.id}
    for job in claimed:
        assert job is not None
        await assert_job_session_is_scoped_to_its_tenant(worker_dbs.tenant_sessions, job)
        async with worker_dbs.tenant_sessions.for_job(job) as db:
            titles = set((await db.execute(text("SELECT title FROM app.documents"))).scalars())
        own, foreign = ("CANARY-A-DOC", "CANARY-B-DOC") if job.tenant_id == a.id else ("CANARY-B-DOC", "CANARY-A-DOC")
        assert own in titles and foreign not in titles


async def test_a_stage_cannot_read_or_write_another_tenants_rows(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await queued_version(raw_db, a, tmp_path)
    canary = await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-SECRET")
    job = await make_queue(worker_dbs).claim("w")
    assert job is not None and job.tenant_id == a.id
    async with worker_dbs.tenant_sessions.for_job(job) as db:
        # Asking for the other tenant explicitly, by id, by join and by canary text, returns nothing
        for sql in (
            "SELECT count(*) FROM app.documents WHERE tenant_id = :b",
            "SELECT count(*) FROM app.document_versions WHERE tenant_id = :b OR document_id = :d",
            "SELECT count(*) FROM app.documents d JOIN app.document_versions v ON v.document_id = d.id WHERE d.title = 'CANARY-B-SECRET'",
        ):
            assert (await db.execute(text(sql), {"b": b.id, "d": canary.document.id})).scalar_one() == 0, sql
    # Writing a row for the other tenant is refused by the RLS WITH CHECK
    with pytest.raises(DBAPIError, match="row-level security"):
        async with worker_dbs.tenant_sessions.for_job(job) as db:
            await db.execute(
                text("UPDATE app.document_versions SET status = 'failed' WHERE tenant_id = :b"), {"b": b.id}
            )
            await db.execute(
                text(
                    "INSERT INTO app.storage_deletions (tenant_id, storage_key, reason) "
                    "VALUES (CAST(:b AS uuid), 't/' || CAST(CAST(:b AS uuid) AS text) || '/d/' || md5('x'), 'canary')"
                ),
                {"b": b.id},
            )
    assert (await chunk_count(raw_db, canary.version.id)) == 0
    assert (await raw_db.execute(text("SELECT status FROM app.document_versions WHERE id = :v"), {"v": canary.version.id})).scalar_one() == "published"


async def test_every_job_transaction_sets_the_tenant_before_its_first_query(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    job = await make_queue(worker_dbs).claim("w")
    assert job is not None
    log = StatementLog(worker_dbs.tenant_engine)
    try:
        for _ in range(3):  # three separate transactions of the same job
            async with worker_dbs.tenant_sessions.for_job(job) as db:
                await db.execute(text("SELECT count(*) FROM app.documents"))
    finally:
        log.close()
    from tests.security.worker_assertions import assert_every_transaction_sets_tenant_first

    assert_every_transaction_sets_tenant_first(log, at_least=3)


async def test_queue_connection_statements_touch_only_queue_tables(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    log = StatementLog(worker_dbs.worker_engine)
    try:
        queue = make_queue(worker_dbs)
        job = await queue.claim("w")
        assert job is not None
        await queue.heartbeat(job)
        await queue.list_tenants()
        await queue.sweep(only_tenants={academic_world.a.id})
        await queue.complete(job)
    finally:
        log.close()
    assert log.statements
    assert_only_queue_tables(log)
