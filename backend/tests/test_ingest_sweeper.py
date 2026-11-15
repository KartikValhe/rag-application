"""
The sweeper (step 5b-2, design A3 "Failures, retries and the sweeper"): one loop, one active at a
time, every tenant table touched through a tenant session only (app_worker has no grant on them).

Realistic world data; `only_tenants` keeps each sweep to the test's own tenants (the _test database
holds thousands of tenants left by other tests).
"""

import asyncio
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import document_store
from app.workers import sweeper
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import make_document
from tests.fixtures.worker import (
    FakeClock,
    chunk_count,
    job_row,
    make_queue,
    queued_version,
    version_row,
)
from tests.security.worker_assertions import StatementLog, assert_only_queue_tables

CONFIG = sweeper.SweeperSettings(
    interval_seconds=60,
    missing_job_after_seconds=120,
    tombstone_retention_days=30,
    finished_job_retention_days=7,
    outbox_batch=50,
    outbox_backoff_base_seconds=60.0,
    outbox_backoff_max_seconds=21_600.0,
)


def make_sweeper(
    dbs: WorkerDatabases,
    store_root: Path | document_store.PrivateObjectStore,
    *,
    only_tenants: Collection[uuid.UUID] | None = None,
    clock: FakeClock | None = None,
    config: sweeper.SweeperSettings = CONFIG,
) -> sweeper.Sweeper:
    store = store_root if not isinstance(store_root, Path) else document_store.LocalPrivateStore(store_root)
    return sweeper.Sweeper(
        queue=make_queue(dbs, clock=clock),
        tenant_sessions=dbs.tenant_sessions,
        store=store,
        config=config,
        clock=clock,
        only_tenants=only_tenants,
    )


async def _sql(db: AsyncSession, sql: str, **params: object) -> None:
    await db.execute(text(sql), params)
    await db.commit()


async def _scalar(db: AsyncSession, sql: str, **params: object) -> object:
    return (await db.execute(text(sql), params)).scalar_one_or_none()


# ── Stuck jobs ───────────────────────────────────────────────────────────────


async def test_jobs_out_of_attempts_fail_and_their_versions_become_processing_failed(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    dead = await queued_version(raw_db, w, tmp_path, subject="Math")
    retryable = await queued_version(raw_db, w, tmp_path, subject="Physics")
    past = datetime.now(UTC) - timedelta(minutes=5)
    await _sql(raw_db, "UPDATE app.document_versions SET status = 'processing', stage = 'embedding' WHERE id = ANY(:v)", v=[dead.version.id, retryable.version.id])
    await _sql(raw_db, "UPDATE app.ingest_jobs SET status = 'running', attempts = 3, lease_expires_at = :t WHERE id = :j", t=past, j=dead.job.id)
    await _sql(raw_db, "UPDATE app.ingest_jobs SET status = 'running', attempts = 1, lease_expires_at = :t WHERE id = :j", t=past, j=retryable.job.id)
    # The dead worker had written half of its chunks
    await _sql(raw_db, "INSERT INTO app.chunks (tenant_id, document_version_id, document_id, subject_id, ordinal, content, content_sha256, token_count, script) "
               "SELECT v.tenant_id, v.id, v.document_id, d.subject_id, 0, 'half', md5('half') || md5('half'), 1, 'latn' FROM app.document_versions v JOIN app.documents d ON d.id = v.document_id WHERE v.id = :v", v=dead.version.id)

    report = await make_sweeper(worker_dbs, tmp_path, only_tenants={w.id}).run_once()
    assert report.ran and report.failed_jobs == 1
    assert (await job_row(raw_db, dead.job.id))["status"] == "failed"
    v = await version_row(raw_db, dead.version.id)
    assert (v.status, v.error_code, v.stage) == ("failed", "processing_failed", None)
    assert (await job_row(raw_db, retryable.job.id))["status"] == "running"  # still has attempts: reclaimable
    assert (await version_row(raw_db, retryable.version.id)).status == "processing"
    assert await make_queue(worker_dbs).claim("w") is not None  # the retryable one is claimable, the dead one is not
    assert await chunk_count(raw_db, dead.version.id) == 1  # left for the retry path (the API retry re-runs the worker, which deletes first)


async def test_a_version_that_was_already_finished_is_not_touched_by_a_failed_job(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    await _sql(raw_db, "UPDATE app.document_versions SET status = 'archived' WHERE id = :v", v=made.version.id)
    await _sql(raw_db, "UPDATE app.ingest_jobs SET status = 'running', attempts = 3, lease_expires_at = now() - interval '1 minute' WHERE id = :j", j=made.job.id)
    await make_sweeper(worker_dbs, tmp_path, only_tenants={w.id}).run_once()
    assert (await version_row(raw_db, made.version.id)).status == "archived"


# ── Missing job rows ─────────────────────────────────────────────────────────


async def test_a_queued_version_older_than_two_minutes_without_a_job_gets_one(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    old = await queued_version(raw_db, w, tmp_path, subject="Math")
    fresh = await queued_version(raw_db, w, tmp_path, subject="Physics")
    has_job = await queued_version(raw_db, w, tmp_path, subject="English")
    await _sql(raw_db, "DELETE FROM app.ingest_jobs WHERE id = ANY(:j)", j=[old.job.id, fresh.job.id])
    await _sql(raw_db, "UPDATE app.document_versions SET updated_at = now() - interval '3 minutes' WHERE id = ANY(:v)", v=[old.version.id, has_job.version.id])

    sw = make_sweeper(worker_dbs, tmp_path, only_tenants={w.id})
    report = await sw.run_once()
    assert report.recreated_jobs == 1
    created = await _scalar(raw_db, "SELECT status || '/' || attempts FROM app.ingest_jobs WHERE version_id = :v", v=old.version.id)
    assert created == "queued/0"
    assert await _scalar(raw_db, "SELECT count(*) FROM app.ingest_jobs WHERE version_id = :v", v=fresh.version.id) == 0  # too young
    assert await _scalar(raw_db, "SELECT count(*) FROM app.ingest_jobs WHERE version_id = :v", v=has_job.version.id) == 1  # no duplicate
    assert (await sw.run_once()).recreated_jobs == 0  # idempotent
    queue = make_queue(worker_dbs)
    claimed = {j.version_id for j in (await queue.claim("w1"), await queue.claim("w2")) if j is not None}
    assert claimed == {old.version.id, has_job.version.id}  # the re-created job is a normal queued job


# ── Storage outbox ───────────────────────────────────────────────────────────


class FlakyStore:
    """A store whose delete fails for chosen keys (an outage) and can block on an event."""

    def __init__(self, inner: document_store.LocalPrivateStore) -> None:
        self.inner = inner
        self.failing: set[str] = set()
        self.deleted: list[str] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def put(self, tenant_id: uuid.UUID, key: str, data: bytes) -> str:
        return await self.inner.put(tenant_id, key, data)

    async def get(self, tenant_id: uuid.UUID, key: str) -> bytes | None:
        return await self.inner.get(tenant_id, key)

    async def stream(self, tenant_id: uuid.UUID, key: str, chunk_size: int = 65536):  # type: ignore[no-untyped-def]
        return await self.inner.stream(tenant_id, key, chunk_size)

    async def delete(self, tenant_id: uuid.UUID, key: str) -> None:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if key in self.failing:
            raise OSError("storage outage")
        self.deleted.append(key)
        await self.inner.delete(tenant_id, key)


async def _outbox(db: AsyncSession, w_id: uuid.UUID, key: str, *, not_before: datetime | None = None) -> None:
    await _sql(db, "INSERT INTO app.storage_deletions (tenant_id, storage_key, reason, not_before) VALUES (:t, :k, 'test', COALESCE(:n, now() - interval '1 minute'))", t=w_id, k=key, n=not_before)


async def test_the_outbox_deletes_files_treats_missing_files_as_success_and_retries_failures_with_backoff(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    store = FlakyStore(document_store.LocalPrivateStore(tmp_path))
    present, missing, outage, later = (document_store.new_document_key(w.id) for _ in range(4))
    for key in (present, outage, later):
        await store.put(w.id, key, b"bytes")
    store.failing.add(outage)
    clock = FakeClock(datetime.now(UTC))
    await _outbox(raw_db, w.id, present)
    await _outbox(raw_db, w.id, missing)  # the file is already gone: still a success
    await _outbox(raw_db, w.id, outage)
    await _outbox(raw_db, w.id, later, not_before=clock.now() + timedelta(days=30))  # kept for its retention period

    sw = make_sweeper(worker_dbs, store, only_tenants={w.id}, clock=clock)
    report = await sw.run_once()
    assert (report.files_deleted, report.deletions_retried) == (2, 1)
    assert await store.get(w.id, present) is None and await store.get(w.id, later) == b"bytes"
    remaining = {r[0]: r for r in (await raw_db.execute(text("SELECT storage_key, attempts, not_before FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()}
    assert set(remaining) == {outage, later}
    assert remaining[outage][1] == 1
    assert abs((remaining[outage][2] - (clock.now() + timedelta(seconds=60))).total_seconds()) < 0.01  # base * 2^0

    assert (await sw.run_once()).deletions_retried == 0  # not due yet: backoff respected
    clock.advance(61)
    assert (await sw.run_once()).deletions_retried == 1
    assert (await _scalar(raw_db, "SELECT attempts FROM app.storage_deletions WHERE storage_key = :k", k=outage)) == 2
    store.failing.clear()
    clock.advance(121)
    assert (await sw.run_once()).files_deleted == 1
    assert await store.get(w.id, outage) is None
    assert {r[0] for r in (await raw_db.execute(text("SELECT storage_key FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()} == {later}


async def test_an_outbox_row_with_an_unusable_key_is_dropped_not_retried_forever(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    await _outbox(raw_db, w.id, f"t/{w.id}/d/not-a-valid-key")
    report = await make_sweeper(worker_dbs, tmp_path, only_tenants={w.id}).run_once()
    assert report.files_deleted == 0
    assert await _scalar(raw_db, "SELECT count(*) FROM app.storage_deletions WHERE tenant_id = :t", t=w.id) == 0


# ── Tombstones and finished jobs ─────────────────────────────────────────────


async def test_old_tombstones_are_purged_with_their_files_and_young_ones_stay(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    old = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="archived", error_code=None, data=b"old", store_root=tmp_path)
    young = await make_document(raw_db, w, subject="Physics", classes=("10-A",), uploader=w.t1, status="archived", error_code=None, data=b"young", store_root=tmp_path)
    live = await make_document(raw_db, w, subject="English", classes=("10-A",), uploader=w.t1, status="published", data=b"live", store_root=tmp_path)
    await _sql(raw_db, "UPDATE app.documents SET deleted_at = now() - interval '31 days' WHERE id = :d", d=old.document.id)
    await _sql(raw_db, "UPDATE app.documents SET deleted_at = now() - interval '29 days' WHERE id = :d", d=young.document.id)
    await _sql(raw_db, "INSERT INTO app.chunks (tenant_id, document_version_id, document_id, subject_id, ordinal, content, content_sha256, token_count, script) "
               "SELECT v.tenant_id, v.id, v.document_id, d.subject_id, 0, 'c', md5('c') || md5('c'), 1, 'latn' FROM app.document_versions v JOIN app.documents d ON d.id = v.document_id WHERE v.id = :v", v=old.version.id)

    store = document_store.LocalPrivateStore(tmp_path)
    report = await make_sweeper(worker_dbs, store, only_tenants={w.id}).run_once()
    assert report.tombstones_purged == 1
    assert await _scalar(raw_db, "SELECT count(*) FROM app.documents WHERE id = :d", d=old.document.id) == 0
    assert await _scalar(raw_db, "SELECT count(*) FROM app.document_versions WHERE id = :v", v=old.version.id) == 0
    assert await chunk_count(raw_db, old.version.id) == 0
    assert await store.get(w.id, old.version.storage_key) is None  # its file went through the outbox in the same run
    assert await store.get(w.id, young.version.storage_key) == b"young"
    assert await store.get(w.id, live.version.storage_key) == b"live"
    assert await _scalar(raw_db, "SELECT count(*) FROM app.documents WHERE id = ANY(:d)", d=[young.document.id, live.document.id]) == 2


async def test_finished_jobs_older_than_seven_days_are_purged_and_nothing_else(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    jobs: dict[str, uuid.UUID] = {}
    for status, subject in (("done", "Math"), ("failed", "Physics"), ("queued", "English"), ("running", "Chemistry")):
        made = await queued_version(raw_db, w, tmp_path, subject=subject, classes=("11-A",) if subject == "Chemistry" else ("10-A",))
        jobs[status] = made.job.id
        await _sql(raw_db, "UPDATE app.ingest_jobs SET status = :s, lease_expires_at = now() + interval '1 hour', updated_at = now() - interval '8 days' WHERE id = :j", s=status, j=made.job.id)
    fresh = await queued_version(raw_db, w, tmp_path, subject="Math", classes=("10-B",))
    await _sql(raw_db, "UPDATE app.ingest_jobs SET status = 'done' WHERE id = :j", j=fresh.job.id)

    report = await make_sweeper(worker_dbs, tmp_path, only_tenants={w.id}).run_once()
    assert report.jobs_purged == 2
    assert await _scalar(raw_db, "SELECT count(*) FROM app.ingest_jobs WHERE id = ANY(:j)", j=[jobs["done"], jobs["failed"]]) == 0
    assert await _scalar(raw_db, "SELECT count(*) FROM app.ingest_jobs WHERE id = ANY(:j)", j=[jobs["queued"], jobs["running"], fresh.job.id]) == 3


# ── Only one sweeper runs; narrow role only ──────────────────────────────────


async def test_only_one_sweeper_runs_at_a_time(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    store = FlakyStore(document_store.LocalPrivateStore(tmp_path))
    key = document_store.new_document_key(w.id)
    await store.put(w.id, key, b"x")
    await _outbox(raw_db, w.id, key)
    store.gate = asyncio.Event()

    first = asyncio.create_task(make_sweeper(worker_dbs, store, only_tenants={w.id}).run_once())
    await asyncio.wait_for(store.entered.wait(), 10)  # the first sweeper is inside its run, holding the lock
    second = await make_sweeper(worker_dbs, store, only_tenants={w.id}).run_once()
    assert second.ran is False
    store.gate.set()
    assert (await first).ran is True
    assert (await make_sweeper(worker_dbs, store, only_tenants={w.id}).run_once()).ran is True  # the lock was released


async def test_the_sweeper_works_through_tenant_sessions_and_the_narrow_role_stays_narrow(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    w = academic_world.a
    old = await queued_version(raw_db, w, tmp_path)
    await _sql(raw_db, "DELETE FROM app.ingest_jobs WHERE id = :j", j=old.job.id)
    await _sql(raw_db, "UPDATE app.document_versions SET updated_at = now() - interval '5 minutes' WHERE id = :v", v=old.version.id)
    await _outbox(raw_db, w.id, document_store.new_document_key(w.id))
    worker_log = StatementLog(worker_dbs.worker_engine)
    tenant_log = StatementLog(worker_dbs.tenant_engine)
    try:
        await make_sweeper(worker_dbs, tmp_path, only_tenants={w.id}).run_once()
    finally:
        worker_log.close()
        tenant_log.close()
    assert_only_queue_tables(worker_log)  # app_worker: ingest_jobs and tenants(id) only
    touched = " ".join(tenant_log.statements).lower()
    assert "app.storage_deletions" in touched and "app.ingest_jobs" in touched  # tenant tables went through app_tenant
    from tests.security.worker_assertions import assert_every_transaction_sets_tenant_first

    assert_every_transaction_sets_tenant_first(tenant_log, at_least=2)
