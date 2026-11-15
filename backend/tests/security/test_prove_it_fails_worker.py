"""
Prove-it-fails for the ingestion worker protections of step 5b-2 (design C2).

Each test first runs the scenario with the protection ON (the shared helper passes), then switches the
protection off with monkeypatch, runs the SAME scenario again and requires the helper to RAISE. That
shows the helper really checks the protection and does not pass by accident.

  1. FOR UPDATE SKIP LOCKED removed       -> the double-claim helper raises.
  2. tenant taken from an argument        -> the session-scope helper raises.
  3. expired-lease reclaim removed        -> the reclaim helper raises.
  4. slot release skipped on failure      -> the slot helper raises.
  5. the tenant is not set per transaction-> the per-transaction helper raises.

TEST database only: the autouse fixture asserts current_database() ends in _test before anything runs.
"""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.slots import SlotManager
from app.workers import db as worker_db
from app.workers import errors, ingest, stages
from app.workers import queue as queue_module
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import make_document
from tests.fixtures.worker import (
    FakePipeline,
    make_context,
    make_queue,
    queued_version,
)
from tests.security.worker_assertions import (
    StatementLog,
    assert_every_transaction_sets_tenant_first,
    assert_expired_lease_reclaimed,
    assert_job_session_is_scoped_to_its_tenant,
    assert_locked_job_is_skipped,
    assert_no_slots_held,
)


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"], connect_args={"statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    finally:
        await engine.dispose()
    assert name.endswith("_test"), f"prove-it tests must run against the test database, not {name!r}"


# ── 1. FOR UPDATE SKIP LOCKED ────────────────────────────────────────────────


async def test_removing_skip_locked_is_caught_by_the_double_claim_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for subject in ("Math", "Physics", "English", "Chemistry"):
        await queued_version(raw_db, academic_world.a, tmp_path, subject=subject, classes=("11-A",) if subject == "Chemistry" else ("10-A",))
    queue = make_queue(worker_dbs)

    await assert_locked_job_is_skipped(queue, worker_dbs.worker_engine)  # protection ON: passes

    assert "FOR UPDATE SKIP LOCKED" in queue_module.CLAIM_SQL
    monkeypatch.setattr(queue_module, "CLAIM_SQL", queue_module.CLAIM_SQL.replace("FOR UPDATE SKIP LOCKED", ""))
    with pytest.raises(AssertionError, match="blocked on a locked row|claimed the same job"):
        await assert_locked_job_is_skipped(queue, worker_dbs.worker_engine, timeout=2.0)


# ── 2. The tenant comes from the claimed row ─────────────────────────────────


async def test_taking_the_tenant_from_an_argument_is_caught_by_the_scope_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, a, subject="Physics", classes=("10-A",), uploader=a.t1, status="published", title="CANARY-A")
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B")
    await queued_version(raw_db, a, tmp_path)
    job = await make_queue(worker_dbs).claim("w")
    assert job is not None and job.tenant_id == a.id

    await assert_job_session_is_scoped_to_its_tenant(worker_dbs.tenant_sessions, job)  # protection ON: passes

    payload_tenant = b.id  # e.g. a tenant id that arrived in a message payload

    @asynccontextmanager
    async def from_argument(_job: object, *, sessionmaker: object = None):  # type: ignore[no-untyped-def]
        async with sessionmaker() as session, session.begin():  # type: ignore[operator]
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(payload_tenant)})
            yield session

    monkeypatch.setattr(worker_db, "tenant_session_for", from_argument)
    with pytest.raises(AssertionError, match="is not the job row's tenant|another tenant's rows"):
        await assert_job_session_is_scoped_to_its_tenant(worker_dbs.tenant_sessions, job)


# ── 3. Lease-expiry reclaim ──────────────────────────────────────────────────


async def test_skipping_the_lease_expiry_reclaim_is_caught_by_the_reclaim_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    queue = make_queue(worker_dbs)

    async def kill_the_worker() -> None:
        await raw_db.execute(
            text("UPDATE app.ingest_jobs SET status = 'running', attempts = 1, worker_id = 'dead', lease_expires_at = :t WHERE id = :j"),
            {"t": datetime.now(UTC) - timedelta(seconds=5), "j": made.job.id},
        )
        await raw_db.commit()

    await kill_the_worker()
    await assert_expired_lease_reclaimed(queue, made.job.id, previous_attempts=1)  # protection ON: passes

    assert queue_module.RECLAIM_EXPIRED_SQL in queue_module.CLAIM_SQL
    monkeypatch.setattr(queue_module, "CLAIM_SQL", queue_module.CLAIM_SQL.replace(queue_module.RECLAIM_EXPIRED_SQL, ""))
    await kill_the_worker()
    with pytest.raises(AssertionError, match="expired running lease was not reclaimed"):
        await assert_expired_lease_reclaimed(queue, made.job.id, previous_attempts=1)


# ── 4. Slot release on failure ───────────────────────────────────────────────


async def _failing_job(
    dbs: WorkerDatabases, w: object, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager
) -> None:
    await queued_version(raw_db, w, tmp_path)  # type: ignore[arg-type]
    pipeline = FakePipeline()

    async def boom(_c: stages.StageContext) -> None:
        raise errors.PermanentIngestError("scanned_pdf")

    pipeline.hooks["parse"] = boom
    assert await ingest.run_one(make_context(dbs, tmp_path, slots), pipeline.stages()) == ingest.JobOutcome.FAILED


async def test_skipping_slot_release_on_failure_is_caught_by_the_slot_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    await _failing_job(worker_dbs, w, raw_db, tmp_path, ingest_slots)
    await assert_no_slots_held(ingest_slots, w.id)  # protection ON: passes

    async def never_release(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(ingest_slots, "release", never_release)
    await _failing_job(worker_dbs, w, raw_db, tmp_path, ingest_slots)
    with pytest.raises(AssertionError, match="slot\\(s\\) were not released"):
        await assert_no_slots_held(ingest_slots, w.id)


# ── 5. The tenant is set in every transaction ────────────────────────────────


async def test_not_setting_the_tenant_per_transaction_is_caught_by_the_transaction_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    job = await make_queue(worker_dbs).claim("w")
    assert job is not None

    async def three_transactions() -> StatementLog:
        log = StatementLog(worker_dbs.tenant_engine)
        try:
            for _ in range(3):
                async with worker_dbs.tenant_sessions.for_job(job) as db:
                    await db.execute(text("SELECT count(*) FROM app.documents"))
        finally:
            log.close()
        return log

    assert_every_transaction_sets_tenant_first(await three_transactions(), at_least=3)  # protection ON: passes

    @asynccontextmanager
    async def sets_once_then_forgets(_job: object, *, sessionmaker: object = None):  # type: ignore[no-untyped-def]
        async with sessionmaker() as session, session.begin():  # type: ignore[operator]
            yield session  # no set_config at all

    monkeypatch.setattr(worker_db, "tenant_session_for", sets_once_then_forgets)
    with pytest.raises(AssertionError, match="before setting the tenant"):
        assert_every_transaction_sets_tenant_first(await three_transactions(), at_least=3)
