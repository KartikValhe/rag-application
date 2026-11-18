"""
Prove-it-fails for the ingestion worker protections of step 5b-2 (design C2).

Each test first runs the scenario with the protection ON (the shared helper passes), then switches the
protection off with monkeypatch, runs the SAME scenario again and requires the helper to RAISE. That
shows the helper really checks the protection and does not pass by accident.

  1. FOR UPDATE SKIP LOCKED removed       -> the double-claim helper raises.
  2. the tenant guard switched off        -> a stage that sets a payload tenant on its own session is seen.
  3. expired-lease reclaim removed        -> the reclaim helper raises.
  4. slot release skipped on failure      -> the slot helper raises.
  5. a stage opens a session without the helper -> the per-transaction helper raises (whole job log).
  6. the job ownership check removed       -> the stale-worker helper raises.
  7. the lease forgets its backend         -> the Redis-lease helper raises.
  8. the fresh-session file check removed  -> the lost-commit scenario raises.

TEST database only: the autouse fixture asserts current_database() ends in _test before anything runs.
"""

import dataclasses
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import documents as documents_api
from app.core import redis_client
from app.core.config import settings
from app.core.slots import SlotLimit, SlotManager
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
from tests.security.slot_assertions import assert_redis_lease_is_not_judged_from_memory
from tests.security.tenant_guard_assertions import assert_own_session_cannot_read_a_payload_tenant
from tests.security.worker_assertions import (
    StatementLog,
    assert_every_transaction_sets_tenant_first,
    assert_expired_lease_reclaimed,
    assert_locked_job_is_skipped,
    assert_no_slots_held,
    assert_stale_worker_cannot_write,
)


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
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
    async def two_claimable_jobs() -> None:
        # A tenant offers one job per claim round, so the two claimable jobs belong to two tenants.
        # Whatever the first run left running is finished first (a tenant at its cap offers nothing).
        await raw_db.execute(text("UPDATE app.ingest_jobs SET status = 'done' WHERE status = 'running' AND tenant_id IN (:a, :b)"),
                             {"a": academic_world.a.id, "b": academic_world.b.id})
        await raw_db.commit()
        await queued_version(raw_db, academic_world.a, tmp_path, subject="Math")
        await queued_version(raw_db, academic_world.b, tmp_path, subject="Physics")

    queue = make_queue(worker_dbs)
    await two_claimable_jobs()
    await assert_locked_job_is_skipped(queue, worker_dbs.worker_engine)  # protection ON: passes

    assert "FOR UPDATE OF j SKIP LOCKED" in queue_module.CLAIM_SQL
    monkeypatch.setattr(queue_module, "CLAIM_SQL", queue_module.CLAIM_SQL.replace("FOR UPDATE OF j SKIP LOCKED", ""))
    await two_claimable_jobs()
    with pytest.raises(AssertionError, match="blocked on a locked row|claimed the same job"):
        await assert_locked_job_is_skipped(queue, worker_dbs.worker_engine, timeout=2.0)


# ── 2. A stage cannot take the tenant from somewhere else ────────────────────
# Rewritten (review of 5b-1/5b-2): the old test patched tenant_session_for itself, so it only proved that
# the helper notices a broken helper. Now the stage is a code path that opens a session WITHOUT the
# helper and sets the tenant it got from a message payload. The engine guard is the protection; with it
# switched off the same helper must raise.


async def test_a_stage_that_sets_the_tenant_from_a_payload_is_caught_when_the_guard_is_off(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None,
) -> None:
    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, a, subject="Physics", classes=("10-A",), uploader=a.t1, status="published", title="CANARY-A")
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B")

    await assert_own_session_cannot_read_a_payload_tenant(worker_dbs.tenant_sessions, b.id, "CANARY-B")  # guard ON

    assert worker_dbs.tenant_guard is not None
    worker_dbs.tenant_guard.remove()  # the protection OFF
    with pytest.raises(AssertionError, match="read another tenant's rows"):
        await assert_own_session_cannot_read_a_payload_tenant(worker_dbs.tenant_sessions, b.id, "CANARY-B")


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


# ── 5. The tenant is set in every transaction of a job ───────────────────────
# Rewritten (review of 5b-1/5b-2): the old test patched tenant_session_for. Now a whole job runs and its
# statement log (every transaction, every statement) goes to the helper. The bad code path is a STAGE that
# opens a session some other way (straight from the sessionmaker) and queries without setting the tenant.


async def _logged_job(
    dbs: WorkerDatabases, w: object, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager, *, sneaky: bool
) -> StatementLog:
    await queued_version(raw_db, w, tmp_path)  # type: ignore[arg-type]
    pipeline = FakePipeline()

    async def own_session(sctx: stages.StageContext) -> None:
        async with sctx.tenant_sessions.sessionmaker() as session, session.begin():  # not the helper
            await session.execute(text("SELECT count(*) FROM app.documents"))

    if sneaky:
        pipeline.hooks["parse"] = own_session
    log = StatementLog(dbs.tenant_engine)
    try:
        assert await ingest.run_one(make_context(dbs, tmp_path, slots), pipeline.stages()) == ingest.JobOutcome.DONE
    finally:
        log.close()
    return log


async def test_a_stage_that_opens_a_session_some_other_way_is_caught_by_the_transaction_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    honest = await _logged_job(worker_dbs, w, raw_db, tmp_path, ingest_slots, sneaky=False)
    assert_every_transaction_sets_tenant_first(honest, at_least=5)  # every transaction of a normal job: passes

    sneaky = await _logged_job(worker_dbs, w, raw_db, tmp_path, ingest_slots, sneaky=True)
    with pytest.raises(AssertionError, match="before setting the tenant"):
        assert_every_transaction_sets_tenant_first(sneaky, at_least=5)


# ── 6. Job ownership ─────────────────────────────────────────────────────────


async def test_removing_the_ownership_check_is_caught_by_the_stale_writer_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_worker_review_fixes import _new_owner_writes_chunks, _reclaimed_job

    made, old, stale, new, fresh = await _reclaimed_job(worker_dbs, raw_db, tmp_path, ingest_slots, academic_world.a)
    await _new_owner_writes_chunks(new, fresh, 3)

    await assert_stale_worker_cannot_write(old, stale, raw_db, made.version.id)  # protection ON: passes

    async def no_check(_db: object, _job: object) -> None:
        return None

    monkeypatch.setattr(stages, "lock_job_ownership", no_check)
    with pytest.raises(AssertionError, match="a stale worker could"):
        await assert_stale_worker_cannot_write(old, stale, raw_db, made.version.id)


# ── 7. The lease records its backend ─────────────────────────────────────────


async def test_a_lease_that_forgets_its_backend_is_caught_by_the_redis_lease_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    try:
        real = await redis_client.get_redis()
        await real.ping()
    except Exception:
        pytest.skip("Redis not reachable")

    async def down() -> Any:
        raise ConnectionError("redis down")

    async def back() -> Any:
        return real

    def break_redis() -> None:
        monkeypatch.setattr(redis_client, "get_redis", down)

    def restore_redis() -> None:
        monkeypatch.setattr(redis_client, "get_redis", back)

    manager = SlotManager()
    lease = await manager.acquire([SlotLimit("test:prove-it:lease", 1, 30.0)])
    await assert_redis_lease_is_not_judged_from_memory(manager, lease, break_redis, restore_redis)  # protection ON
    await manager.release(lease)

    lease2 = await manager.acquire([SlotLimit("test:prove-it:lease", 1, 30.0)])
    forgetful = dataclasses.replace(lease2, backend="memory")  # the lease no longer says Redis holds it
    with pytest.raises(AssertionError, match="renewed without Redis|lost although"):
        await assert_redis_lease_is_not_judged_from_memory(manager, forgetful, break_redis, restore_redis)
    await manager.release(lease2)


# ── 8. The fresh-session file check ──────────────────────────────────────────


async def test_removing_the_fresh_session_file_check_is_caught_by_the_lost_commit_scenario(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_upload_cleanup import run_lost_commit_scenario

    await run_lost_commit_scenario(monkeypatch, academic_world.a, raw_db, document_store_root)  # protection ON

    monkeypatch.setattr(documents_api, "_discard_unless_committed", documents_api._discard_file)  # the old behaviour
    with pytest.raises(AssertionError, match="missing files"):
        await run_lost_commit_scenario(monkeypatch, academic_world.b, raw_db, document_store_root)
