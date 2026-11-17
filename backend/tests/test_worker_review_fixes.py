"""
Review of 5b-1/5b-2, findings 1, 3, 4 and 5 (worker side): job ownership, a slow Redis call, outages
that must not use up attempts, and the maximum job lifetime.

Fake stages plus the real queue, slots, tenant sessions and sandbox; the _test database only. The heartbeat
is driven by a manual tick (tests/fixtures/worker.ManualTicker), never by wall-clock time, and every job
here runs with a heartbeat that would only fire after an hour unless the test is about the heartbeat.
"""

import asyncio
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.core import slots as slots_module
from app.core.config import settings
from app.core.slots import SlotManager
from app.workers import errors, ingest, stages
from app.workers.db import WorkerDatabases
from app.workers.queue import Job
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument
from tests.fixtures.worker import (
    FAST,
    FakeClock,
    FakePipeline,
    FlakyTenantSessions,
    ManualTicker,
    OutageSlots,
    chunk_count,
    job_row,
    make_context,
    queued_version,
    version_row,
)
from tests.security.worker_assertions import assert_no_slots_held, assert_stale_worker_cannot_write

# Nothing here depends on the heartbeat firing by itself
QUIET = replace(FAST, heartbeat_seconds=3600.0, lease_seconds=300.0)
BACKEND_DOWN = "backend_down"  # JobOutcome.BACKEND_DOWN.value


async def _run(ctx: ingest.WorkerContext, pipeline: FakePipeline) -> ingest.JobOutcome | None:
    return await ingest.run_one(ctx, pipeline.stages())


async def _expire_lease(raw_db: AsyncSession, job_id: Any) -> None:
    await raw_db.execute(
        text("UPDATE app.ingest_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": job_id}
    )
    await raw_db.commit()


async def _reclaimed_job(
    worker_dbs: WorkerDatabases, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager, w: WorldTenant
) -> tuple[MadeDocument, ingest.WorkerContext, Job, ingest.WorkerContext, Job]:
    """Worker 1 claims the job and stalls; its lease expires; worker 2 reclaims it (attempts 2)."""
    made = await queued_version(raw_db, w, tmp_path)
    old = make_context(worker_dbs, tmp_path, slots, config=QUIET, seed=1)
    stale = await old.queue.claim(old.worker_id)
    assert stale is not None and stale.attempts == 1
    await _expire_lease(raw_db, made.job.id)
    new = make_context(worker_dbs, tmp_path, slots, config=QUIET, seed=2)
    fresh = await new.queue.claim(new.worker_id)
    assert fresh is not None and fresh.id == stale.id and fresh.attempts == 2
    assert fresh.worker_id != stale.worker_id
    return made, old, stale, new, fresh


async def _new_owner_writes_chunks(ctx: ingest.WorkerContext, job: Job, chunks: int) -> None:
    info, limits = await ingest._begin(ctx, job)
    sctx = stages.StageContext(
        job=job, version=info, limits=limits, config=ctx.config, store=ctx.store, tenant_sessions=ctx.tenant_sessions,
        _session=lambda: stages.active_version_session(ctx.tenant_sessions, job),
    )
    sctx.results["pages"] = [f"page {i}" for i in range(chunks)]
    await FakePipeline(chunks=chunks).chunk(sctx)


# ── Finding 1: job ownership ─────────────────────────────────────────────────


async def test_a_stale_worker_cannot_write_after_its_job_was_reclaimed(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made, old, stale, new, fresh = await _reclaimed_job(worker_dbs, raw_db, tmp_path, ingest_slots, academic_world.a)
    await _new_owner_writes_chunks(new, fresh, 3)  # the new owner has already produced rows
    assert await chunk_count(raw_db, made.version.id) == 3

    await assert_stale_worker_cannot_write(old, stale, raw_db, made.version.id)

    assert await chunk_count(raw_db, made.version.id) == 3
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("processing", None)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", fresh.worker_id, 2)


async def test_the_ownership_check_holds_the_job_row_until_the_write_commits(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """No check-then-write gap: while the owner is between its check and its write, nobody can reclaim the job."""
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    old = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, seed=1)
    job = await old.queue.claim(old.worker_id)
    assert job is not None
    await _expire_lease(raw_db, made.job.id)  # reclaimable by anyone who can take the row
    rival = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, seed=2)

    async with stages.active_version_session(old.tenant_sessions, job):  # the check passed: the write would follow
        stolen = await asyncio.wait_for(rival.queue.claim(rival.worker_id), 10)
        assert stolen is None, "the job was reclaimed between the ownership check and the write"
    reclaimed = await rival.queue.claim(rival.worker_id)  # the write committed: now it can be taken
    assert reclaimed is not None and reclaimed.attempts == 2


@pytest.mark.parametrize(
    "make_exc",
    [lambda: errors.PermanentIngestError("scanned_pdf"), lambda: errors.TransientIngestError("worker_crash")],
    ids=["permanent_error", "transient_error"],
)
async def test_the_error_path_of_a_stale_worker_does_not_fail_or_requeue_the_new_owners_job(
    make_exc: Any, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    old = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, seed=1)
    new = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, seed=2)
    pipeline = FakePipeline()
    owner: dict[str, Job] = {}

    async def lose_the_job_then_fail(_c: stages.StageContext) -> None:
        await _expire_lease(raw_db, made.job.id)
        fresh = await new.queue.claim(new.worker_id)
        assert fresh is not None
        await ingest._begin(new, fresh)  # the new owner is working on the version
        owner["job"] = fresh
        raise make_exc()

    pipeline.hooks["parse"] = lose_the_job_then_fail
    assert await _run(old, pipeline) == ingest.JobOutcome.CANCELLED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.stage, v.error_code) == ("processing", "parsing", None)  # still the new owner's
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["worker_id"], row["attempts"], row["last_error_code"]) == (
        "running", owner["job"].worker_id, 2, None,
    )
    await assert_no_slots_held(ingest_slots, w.id)


async def test_two_workers_racing_on_one_job_end_with_exactly_one_set_of_results(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """The stalled worker wakes up after the new owner already wrote chunks: it must stop at its next write."""
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    old = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, seed=1)
    # tenant_concurrency 2 so the second worker can hold its slot while the stalled one still holds its own
    new = make_context(worker_dbs, tmp_path, ingest_slots, config=replace(QUIET, tenant_concurrency=2), seed=2)
    slow = FakePipeline(chunks=5)  # the stalled worker would write 5 chunks, the real owner 3
    real = FakePipeline(chunks=3)
    b_chunks_written = asyncio.Event()
    release_b = asyncio.Event()

    async def hold_after_chunks(_c: stages.StageContext) -> None:
        b_chunks_written.set()
        await release_b.wait()

    real.hooks["embed"] = hold_after_chunks
    b_task: dict[str, asyncio.Task[ingest.JobOutcome]] = {}

    async def stall(_c: stages.StageContext) -> None:
        await _expire_lease(raw_db, made.job.id)
        fresh = await new.queue.claim(new.worker_id)
        assert fresh is not None
        b_task["t"] = asyncio.create_task(ingest.ingest_document_version(fresh, real.stages(), new))
        await asyncio.wait_for(b_chunks_written.wait(), 30)  # B has written its rows; A wakes up now

    slow.hooks["parse"] = stall
    outcome_a = await _run(old, slow)
    release_b.set()
    outcome_b = await asyncio.wait_for(b_task["t"], 30)

    assert outcome_a == ingest.JobOutcome.CANCELLED
    assert slow.calls == ["validate", "parse"]  # it never got to write
    assert outcome_b == ingest.JobOutcome.DONE
    assert await chunk_count(raw_db, made.version.id) == 3
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.chunk_count, v.error_code) == ("ready", 3, None)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("done", 2)
    await assert_no_slots_held(ingest_slots, w.id)


# ── Finding 3: a slow Redis call must not stop the job ───────────────────────


class _RedisProxy:
    """The real Redis client, except that the first renewal script call fails the way a call slower than 1 s does."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.timeouts_raised = 0
        self.renewals = 0

    async def eval(self, script: str, *args: Any) -> Any:
        if script == slots_module._RENEW_LUA:
            if self.timeouts_raised == 0:
                self.timeouts_raised += 1
                raise TimeoutError("a Redis call slower than REDIS_OP_TIMEOUT")
            self.renewals += 1
        return await self._real.eval(script, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_a_slow_redis_call_in_the_heartbeat_does_not_stop_the_job(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Development mode, Redis answers too slowly once: the token is in Redis, so no fallback may say 'lease lost'."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    try:
        real = await redis_client.get_redis()
        await real.ping()
    except Exception:
        pytest.skip("Redis not reachable")
    proxy = _RedisProxy(real)

    async def get_proxy() -> _RedisProxy:
        return proxy

    ticker = ManualTicker()
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=replace(QUIET, lease_seconds=60.0), sleep=ticker.sleep)
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    pipeline = FakePipeline()

    async def three_beats(_c: stages.StageContext) -> None:
        for _ in range(3):
            await ticker.beat()

    pipeline.hooks["parse"] = three_beats
    monkeypatch.setattr(redis_client, "get_redis", get_proxy)
    outcome = await asyncio.wait_for(_run(ctx, pipeline), 60)
    assert outcome == ingest.JobOutcome.DONE
    assert proxy.timeouts_raised == 1 and proxy.renewals >= 2  # the slow call happened, later renewals worked
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("done", 1, None)
    assert pipeline.calls == ["validate", "parse", "chunk", "embed", "index"]


# ── Finding 4: an outage is not the job's fault ──────────────────────────────


async def test_a_redis_outage_when_taking_the_slots_does_not_use_up_attempts(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None,
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    polls = QUIET.max_attempts + 2  # the outage lasts longer than INGEST_MAX_ATTEMPTS polls
    slots = OutageSlots(failures=polls)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, slots, config=QUIET, clock=clock)
    pipeline = FakePipeline()

    for _ in range(polls):
        assert (await _run(ctx, pipeline)).value == BACKEND_DOWN  # type: ignore[union-attr]
        row = await job_row(raw_db, made.job.id)
        assert (row["status"], row["attempts"], row["last_error_code"]) == ("queued", 0, "redis_unavailable")
        assert 15.0 <= (row["run_after"] - clock.now()).total_seconds() <= 45.0  # backs off like a busy slot
        v = await version_row(raw_db, made.version.id)
        assert (v.status, v.error_code) == ("queued", None)  # never failed, never shown as processing
        assert await ctx.queue.claim("early") is None  # not before run_after
        clock.advance(46.0)
    assert slots.refused == polls and pipeline.calls == []

    assert await _run(ctx, pipeline) == ingest.JobOutcome.DONE  # Redis is back
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.chunk_count, v.error_code) == ("ready", 3, None)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("done", 1)  # (last_error_code keeps the last outage, as for any retry)


async def test_a_database_outage_when_the_job_starts_does_not_use_up_attempts(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    polls = QUIET.max_attempts + 2
    clock = FakeClock(datetime.now(UTC))
    healthy = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, clock=clock)
    flaky = FlakyTenantSessions(worker_dbs.tenant_sessions, failures=polls)
    ctx = replace(healthy, tenant_sessions=flaky)  # type: ignore[arg-type]
    pipeline = FakePipeline()

    for _ in range(polls):
        assert (await _run(ctx, pipeline)).value == BACKEND_DOWN  # type: ignore[union-attr]
        row = await job_row(raw_db, made.job.id)
        assert (row["status"], row["attempts"], row["last_error_code"]) == ("queued", 0, "db_unavailable")
        assert 15.0 <= (row["run_after"] - clock.now()).total_seconds() <= 45.0
        v = await version_row(raw_db, made.version.id)
        assert (v.status, v.error_code) == ("queued", None)
        clock.advance(46.0)
        await assert_no_slots_held(ingest_slots, w.id)
    assert flaky.refused == polls

    assert await _run(healthy, pipeline) == ingest.JobOutcome.DONE  # the database is back
    assert (await version_row(raw_db, made.version.id)).status == "ready"
    assert (await job_row(raw_db, made.job.id))["attempts"] == 1


async def test_a_real_stage_failure_still_counts_its_attempt(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """The refund is only for an unavailable backend at the start: a crashing stage still uses attempts. A
    crash (worker_crash) has the smaller budget since the review of 5b-3 (INGEST_MAX_CRASH_ATTEMPTS = 2)."""
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, clock=clock)
    pipeline = FakePipeline()

    async def crash(_c: stages.StageContext) -> None:
        raise RuntimeError("a stage bug")

    pipeline.hooks["chunk"] = crash
    for expected in (ingest.JobOutcome.RETRY, ingest.JobOutcome.FAILED):
        assert await _run(ctx, pipeline) == expected
        clock.advance(500.0)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("failed", 2)
    assert (await version_row(raw_db, made.version.id)).error_code == "processing_failed"


# ── Finding 5: the maximum job lifetime is final ─────────────────────────────


async def test_reaching_the_maximum_job_lifetime_fails_the_job_for_good(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ticker = ManualTicker()
    config = replace(QUIET, max_job_seconds=0.0)  # the first renewal is already past the cap
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=config, clock=clock, sleep=ticker.sleep)
    pipeline = FakePipeline()

    async def run_until_the_cap(_c: stages.StageContext) -> None:
        await ticker.beat()
        await asyncio.sleep(60)  # never reached: the stop cancels this stage

    pipeline.hooks["parse"] = run_until_the_cap
    assert await asyncio.wait_for(_run(ctx, pipeline), 30) == ingest.JobOutcome.FAILED
    assert "chunk" not in pipeline.calls and await chunk_count(raw_db, made.version.id) == 0
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "job_time_limit_exceeded")
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"], row["attempts"]) == ("failed", "job_time_limit_exceeded", 1)
    clock.advance(86_400.0)
    assert await ctx.queue.claim("again") is None  # never retried
    await assert_no_slots_held(ingest_slots, w.id)


def test_the_lifetime_code_is_permanent_and_documented() -> None:
    assert "job_time_limit_exceeded" in errors.PERMANENT_ERROR_CODES
    assert errors.PermanentIngestError("job_time_limit_exceeded").code == "job_time_limit_exceeded"
    docs = Path(__file__).resolve().parents[2] / "docs"
    assert "job_time_limit_exceeded" in (docs / "api" / "phase5.md").read_text(encoding="utf-8")
    assert "job_time_limit_exceeded" in (docs / "phase5-design.md").read_text(encoding="utf-8")


async def test_a_lost_lease_is_still_retried_not_failed(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease lost for any other reason than the cap keeps the old behaviour: requeue, attempt counted."""
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ticker = ManualTicker()
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=QUIET, sleep=ticker.sleep)
    pipeline = FakePipeline()

    async def lose_the_slots(_c: stages.StageContext) -> None:
        # Another process released the token (an operator cleared the key): the lease is gone
        with contextlib.suppress(Exception):  # development fallback: the lease lives in this manager's memory
            await (await redis_client.get_redis()).delete(ingest.GLOBAL_SLOT_KEY)
        ctx.slots._mem.clear()
        await ticker.beat()
        await asyncio.sleep(60)

    pipeline.hooks["parse"] = lose_the_slots
    assert await asyncio.wait_for(_run(ctx, pipeline), 30) == ingest.JobOutcome.STOPPED
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"]) == ("queued", "slot_lease_lost")
    assert (await version_row(raw_db, made.version.id)).status == "queued"
