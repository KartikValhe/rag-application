"""
The ingestion worker (step 5b-2, design A3 "Worker job" and "Failures, retries and the sweeper").

Fake stages (tests/fixtures/worker.FakePipeline) plus the real queue, slots, tenant sessions and
sandbox. Realistic academic-world data; the _test database only.
"""

import asyncio
import os
import random
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slots import RenewResult, SlotBackendUnavailable, SlotLimit, SlotManager
from app.workers import errors, ingest, stages
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.worker import (
    FAST,
    FakeClock,
    FakePipeline,
    ManualTicker,
    chunk_count,
    job_row,
    make_context,
    queued_version,
    version_row,
)
from tests.security.worker_assertions import (
    StatementLog,
    assert_every_transaction_sets_tenant_first,
    assert_job_session_is_scoped_to_its_tenant,
    assert_no_slots_held,
    assert_only_queue_tables,
)
from tests.test_timetable_hardening import _wait_dead


class SimulatedKill(BaseException):  # noqa: N818  (a kill -9: nothing in the worker may catch it)
    pass


async def _run(ctx: ingest.WorkerContext, pipeline: FakePipeline) -> ingest.JobOutcome | None:
    return await ingest.run_one(ctx, pipeline.stages())


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_a_job_runs_all_stages_in_order_and_ends_done(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    pipeline = FakePipeline(chunks=3)
    seen_stage: dict[str, str | None] = {}

    for name in ("validate", "parse", "chunk", "embed", "index"):
        async def snoop(ctx: stages.StageContext, name: str = name) -> None:
            async with ctx.session() as db:
                seen_stage[name] = (await db.execute(text("SELECT status || '/' || coalesce(stage, '-') FROM app.document_versions WHERE id = :v"), {"v": ctx.version.id})).scalar_one()
        pipeline.hooks[name] = snoop

    outcome = await _run(make_context(worker_dbs, tmp_path, ingest_slots), pipeline)
    assert outcome == ingest.JobOutcome.DONE
    assert pipeline.calls == ["validate", "parse", "chunk", "embed", "index"]
    assert seen_stage == {
        "validate": "processing/parsing", "parse": "processing/parsing", "chunk": "processing/chunking",
        "embed": "processing/embedding", "index": "processing/indexing",
    }
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.stage, v.chunk_count, v.error_code) == ("ready", None, 3, None)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["lease_expires_at"]) == ("done", 1, None)
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_nothing_to_claim_returns_none(
    worker_dbs: WorkerDatabases, tmp_path: Path, quiet_queue: None, ingest_slots: SlotManager
) -> None:
    assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), FakePipeline()) is None


# ── Busy slots ───────────────────────────────────────────────────────────────


async def test_a_busy_tenant_slot_requeues_the_job_with_the_attempt_refunded(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    held = await ingest_slots.acquire([SlotLimit(ingest.tenant_slot_key(w.id), 1, 60.0)])  # another worker of this tenant
    try:
        pipeline = FakePipeline()
        clock = FakeClock(datetime.now(UTC))
        outcome = await _run(make_context(worker_dbs, tmp_path, ingest_slots, clock=clock), pipeline)
        assert outcome == ingest.JobOutcome.BUSY
        assert pipeline.calls == []
        row = await job_row(raw_db, made.job.id)
        assert (row["status"], row["attempts"], row["worker_id"], row["lease_expires_at"]) == ("queued", 0, None, None)
        delay = (row["run_after"] - clock.now()).total_seconds()
        assert 15.0 <= delay <= 45.0, delay
        assert (await version_row(raw_db, made.version.id)).status == "queued"  # never shown as processing
        assert await ingest_slots.count(ingest.GLOBAL_SLOT_KEY) == 0  # the global slot was given back too
    finally:
        await ingest_slots.release(held)


async def test_a_full_global_cap_also_sends_the_job_back(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.b, tmp_path)
    held = [await ingest_slots.acquire([SlotLimit(ingest.GLOBAL_SLOT_KEY, FAST.global_concurrency, 60.0)]) for _ in range(FAST.global_concurrency)]
    try:
        assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), FakePipeline()) == ingest.JobOutcome.BUSY
        assert (await job_row(raw_db, made.job.id))["attempts"] == 0
        assert await ingest_slots.count(ingest.tenant_slot_key(academic_world.b.id)) == 0
    finally:
        for lease in held:
            await ingest_slots.release(lease)


# ── Permanent failures ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    ["scanned_pdf", "parse_timeout", "too_many_pages", "quota_pages_exceeded", "pdf_active_content", "invalid_encoding",
     "file_type_mismatch", "password_protected_pdf", "no_text_found", "unreadable_text", "text_too_large"],
)
async def test_permanent_errors_fail_the_version_and_the_job_without_retry(
    code: str, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    pipeline = FakePipeline()

    async def boom(_ctx: stages.StageContext) -> None:
        raise errors.PermanentIngestError(code)

    pipeline.hooks["parse"] = boom
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, clock=clock)
    assert await _run(ctx, pipeline) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code, v.stage) == ("failed", code, None)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"], row["attempts"]) == ("failed", code, 1)
    clock.advance(86_400)
    assert await ctx.queue.claim("again") is None  # never retried
    assert pipeline.calls == ["validate", "parse"]
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_an_unknown_permanent_code_is_never_stored_as_such() -> None:
    assert errors.PermanentIngestError("C:\\secret\\path").code == "parse_failed"
    assert errors.TransientIngestError("anything").code == "worker_crash"


# ── Transient failures: backoff with a fake clock ────────────────────────────


@pytest.mark.parametrize(
    "make_exc,code",
    [
        (lambda: OperationalError("SELECT 1", {}, Exception("connection lost")), "db_unavailable"),
        (lambda: SlotBackendUnavailable(), "redis_unavailable"),
        (lambda: OSError("disk gone"), "storage_unavailable"),
    ],
)
async def test_outage_errors_requeue_with_exponential_backoff_and_get_the_full_attempt_budget(
    make_exc: object, code: str, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession,
    tmp_path: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, clock=clock, seed=11)
    twin = random.Random(11)  # the worker's rng, replayed to know the exact jitter
    pipeline = FakePipeline()

    async def boom(_ctx: stages.StageContext) -> None:
        raise make_exc()  # type: ignore[operator]

    pipeline.hooks["chunk"] = boom
    for attempt in (1, 2):
        assert await _run(ctx, pipeline) == ingest.JobOutcome.RETRY
        expected = 30.0 * 2 ** (attempt - 1) + twin.uniform(0, 15)
        row = await job_row(raw_db, made.job.id)
        assert (row["status"], row["attempts"], row["last_error_code"]) == ("queued", attempt, code)
        assert abs((row["run_after"] - clock.now()).total_seconds() - expected) < 0.01
        assert 30.0 * 2 ** (attempt - 1) <= expected <= 30.0 * 2 ** (attempt - 1) + 15
        v = await version_row(raw_db, made.version.id)
        assert (v.status, v.error_code) == ("queued", None)  # waiting, not failed
        assert await ctx.queue.claim("early") is None  # not before run_after
        clock.advance(expected + 0.5)
    assert await _run(ctx, pipeline) == ingest.JobOutcome.FAILED  # third failure: attempts exhausted
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "processing_failed")
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("failed", 3, "processing_failed")
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


@pytest.mark.parametrize(
    "make_exc",
    [lambda: errors.TransientIngestError("worker_crash"), lambda: RuntimeError("a stage bug with CANARY text")],
    ids=["child-crash", "stage-bug"],
)
async def test_a_crash_requeues_once_and_then_fails_with_processing_failed_on_the_smaller_budget(
    make_exc: object, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession,
    tmp_path: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """worker_crash (a dead child, or any stage bug) is transient but counts against
    INGEST_MAX_CRASH_ATTEMPTS = 2, so a file that kills its parser cannot loop through all 3 attempts."""
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, clock=clock, seed=11)
    pipeline = FakePipeline()

    async def boom(_ctx: stages.StageContext) -> None:
        raise make_exc()  # type: ignore[operator]

    pipeline.hooks["chunk"] = boom
    assert await _run(ctx, pipeline) == ingest.JobOutcome.RETRY
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("queued", 1, "worker_crash")
    clock.advance(1000)
    assert await _run(ctx, pipeline) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "processing_failed")
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("failed", 2, "processing_failed")
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


def test_retry_delay_is_30_times_2_to_the_attempt_minus_one_plus_jitter() -> None:
    rng = random.Random(1)
    for attempt, base in ((1, 30), (2, 60), (3, 120)):
        d = ingest.retry_delay_seconds(attempt, rng, FAST)
        assert base <= d <= base + 15
    assert 4.0 <= ingest.poll_delay_seconds(random.Random(2), FAST) <= 6.0


# ── Heartbeat ────────────────────────────────────────────────────────────────


async def test_the_heartbeat_keeps_the_job_lease_and_both_slots_alive(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No wall-clock time: a fake clock drives the job lease, a manual tick drives the heartbeat, and the slot
    lease is long. The clock moves past the ORIGINAL job lease; only the heartbeat's renewals keep the job.
    """
    w = academic_world.a
    made = await queued_version(raw_db, w, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ticker = ManualTicker()
    config = replace(FAST, lease_seconds=300.0)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=config, clock=clock, sleep=ticker.sleep)
    pipeline = FakePipeline()
    probes: dict[str, object] = {}
    renewals: list[RenewResult] = []
    real_renew = ingest_slots.renew_lease

    async def spy(*args: object, **kwargs: object) -> RenewResult:
        result = await real_renew(*args, **kwargs)  # type: ignore[arg-type]
        renewals.append(result)
        return result

    monkeypatch.setattr(ingest_slots, "renew_lease", spy)

    async def long_stage(_ctx: stages.StageContext) -> None:
        probes["first_lease"] = (await job_row(raw_db, made.job.id))["lease_expires_at"]
        for _ in range(4):
            clock.advance(100.0)  # 400 s in all: past the original 300 s lease
            await ticker.beat()
        probes["claimable"] = await ctx.queue.claim("thief")
        probes["global"] = await ingest_slots.count(ingest.GLOBAL_SLOT_KEY)
        probes["tenant"] = await ingest_slots.count(ingest.tenant_slot_key(w.id))
        probes["last_lease"] = (await job_row(raw_db, made.job.id))["lease_expires_at"]

    pipeline.hooks["parse"] = long_stage
    assert await asyncio.wait_for(_run(ctx, pipeline), 60) == ingest.JobOutcome.DONE
    assert (probes["claimable"], probes["global"], probes["tenant"]) == (None, 1, 1)
    assert renewals == [RenewResult.RENEWED] * 4
    assert probes["last_lease"] > probes["first_lease"]  # type: ignore[operator]
    assert probes["last_lease"] == clock.now() + timedelta(seconds=300.0)  # renewed by the last beat
    assert (await job_row(raw_db, made.job.id))["attempts"] == 1


async def test_a_lost_slot_lease_stops_the_job_kills_the_child_and_writes_nothing(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ticker = ManualTicker()
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, clock=FakeClock(datetime.now(UTC)), sleep=ticker.sleep)
    pid_file = tmp_path / "pid.txt"
    pipeline = FakePipeline()

    async def hang_in_the_sandbox(sctx: stages.StageContext) -> None:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=sctx.limits, parse=FAST.parse, child_module="tests._sandbox_children",
            child_args=("sleep",), extra_params={"pid_file": str(pid_file)},
        )

    async def lease_expired(*_a: object, **_k: object) -> RenewResult:
        return RenewResult.LEASE_LOST  # the slot lease expired or was released

    async def tick_once_the_child_runs() -> None:
        while not pid_file.exists():  # waits for the real child process to start, nothing else
            await asyncio.sleep(0.05)
        await ticker.beat()

    pipeline.hooks["parse"] = hang_in_the_sandbox
    monkeypatch.setattr(ingest_slots, "renew_lease", lease_expired)
    kick = asyncio.create_task(tick_once_the_child_runs())
    try:
        outcome = await asyncio.wait_for(_run(ctx, pipeline), 30)
    finally:
        kick.cancel()
    assert outcome == ingest.JobOutcome.STOPPED
    assert "chunk" not in pipeline.calls and "index" not in pipeline.calls
    assert await _wait_dead(int(pid_file.read_text())), "the hung child kept running after the job was stopped"
    assert await chunk_count(raw_db, made.version.id) == 0
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"], row["lease_expires_at"]) == ("queued", "slot_lease_lost", None)
    assert (await version_row(raw_db, made.version.id)).status == "queued"
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_redis_going_away_during_the_heartbeat_stops_the_job_after_three_failed_renewals(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ticker = ManualTicker()
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, sleep=ticker.sleep)
    pipeline = FakePipeline()

    async def hang(_c: stages.StageContext) -> None:
        for _ in range(10):  # the third failed renewal stops the job and cancels this stage
            await ticker.beat()

    async def broken_renew(*_a: object, **_k: object) -> RenewResult:
        raise SlotBackendUnavailable()

    pipeline.hooks["parse"] = hang
    monkeypatch.setattr(ingest_slots, "renew_lease", broken_renew)
    assert await asyncio.wait_for(_run(ctx, pipeline), 30) == ingest.JobOutcome.STOPPED
    assert ticker.beats == ingest.HEARTBEAT_MAX_BACKEND_FAILURES - 1  # two failures were tolerated
    assert "chunk" not in pipeline.calls
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"]) == ("queued", "redis_unavailable")
    assert await chunk_count(raw_db, made.version.id) == 0


async def test_a_job_cancelled_by_a_document_delete_stops_and_writes_no_results(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """Backlog B40: DELETE /documents marks the job failed (frozen) and archives the version."""
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ticker = ManualTicker()
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, sleep=ticker.sleep)
    pipeline = FakePipeline()

    async def delete_then_wait(_c: stages.StageContext) -> None:
        await raw_db.execute(text("UPDATE app.ingest_jobs SET status = 'failed', last_error_code = 'document_deleted' WHERE id = :j"), {"j": made.job.id})
        await raw_db.execute(text("UPDATE app.document_versions SET status = 'archived' WHERE id = :v"), {"v": made.version.id})
        await raw_db.commit()
        await ticker.beat()  # the heartbeat finds its job row frozen and stops the stage

    pipeline.hooks["parse"] = delete_then_wait
    assert await asyncio.wait_for(_run(ctx, pipeline), 30) == ingest.JobOutcome.CANCELLED
    assert await chunk_count(raw_db, made.version.id) == 0
    assert (await job_row(raw_db, made.job.id))["last_error_code"] == "document_deleted"
    assert (await version_row(raw_db, made.version.id)).status == "archived"
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_a_version_that_is_no_longer_queued_is_cancelled_before_any_stage_runs(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    await raw_db.execute(text("UPDATE app.document_versions SET status = 'archived' WHERE id = :v"), {"v": made.version.id})
    await raw_db.commit()
    pipeline = FakePipeline()
    assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), pipeline) == ingest.JobOutcome.CANCELLED
    assert pipeline.calls == []
    assert (await job_row(raw_db, made.job.id))["status"] == "failed"
    assert (await version_row(raw_db, made.version.id)).status == "archived"


# ── A hung or crashing child, slots released ─────────────────────────────────


async def test_a_hung_child_is_killed_the_job_fails_permanently_and_slots_are_released(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    parse = stages.ParseSettings(timeout_seconds=1.5, max_output_bytes=1024, memory_limit_mb=1024, cpu_seconds=60)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=replace(FAST, parse=parse))
    pid_file = tmp_path / "pid.txt"
    pipeline = FakePipeline()

    async def hang(sctx: stages.StageContext) -> None:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=sctx.limits, parse=sctx.config.parse, child_module="tests._sandbox_children",
            child_args=("sleep",), extra_params={"pid_file": str(pid_file)},
        )

    pipeline.hooks["parse"] = hang
    assert await _run(ctx, pipeline) == ingest.JobOutcome.FAILED
    assert (await version_row(raw_db, made.version.id)).error_code == "parse_timeout"
    assert await _wait_dead(int(pid_file.read_text()))
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_a_crashing_child_is_a_transient_worker_crash(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots)
    pipeline = FakePipeline()

    async def crash(sctx: stages.StageContext) -> None:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=sctx.limits, parse=sctx.config.parse, child_module="tests._sandbox_children",
            child_args=("boom",),
        )

    pipeline.hooks["parse"] = crash
    assert await _run(ctx, pipeline) == ingest.JobOutcome.RETRY
    assert (await job_row(raw_db, made.job.id))["last_error_code"] == "worker_crash"
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_the_real_validate_stage_reads_the_stored_file_and_runs_it_in_the_sandbox(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    good = await queued_version(raw_db, w, tmp_path, subject="Math", data=b"Unit 1: Algebra\n")
    ctx = make_context(worker_dbs, tmp_path, ingest_slots)
    pipeline = FakePipeline()
    real = stages.make_validate_stage(ctx.store, ctx.config.parse)

    async def validate(sctx: stages.StageContext) -> None:
        await real(sctx)
        assert sctx.results["validation"] == stages.SandboxValidation(file_kind="txt", page_count=None)

    pipeline.hooks.clear()
    st = pipeline.stages()
    assert await ingest.run_one(ctx, replace(st, validate=validate)) == ingest.JobOutcome.DONE
    assert (await job_row(raw_db, good.job.id))["status"] == "done"

    # A file that is not what its version says is a permanent validation failure
    bad = await queued_version(raw_db, w, tmp_path, subject="Physics", data=b"MZ\x90 this is an executable")
    assert await ingest.run_one(ctx, replace(st, validate=real)) == ingest.JobOutcome.FAILED
    assert (await version_row(raw_db, bad.version.id)).error_code == "disallowed_file_type"


# ── A killed worker: recovery with no duplicate rows ─────────────────────────


@pytest.mark.parametrize("kill_in", ["parse", "embed"])  # after stage 2 (no rows yet) and after the chunks were written
async def test_a_worker_killed_mid_job_leaves_a_state_the_sweeper_and_next_worker_recover(
    kill_in: str, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    from tests.test_ingest_sweeper import make_sweeper

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots)

    async def kill(_c: stages.StageContext) -> None:
        raise SimulatedKill()

    victim = FakePipeline(chunks=4)
    victim.hooks[kill_in] = kill
    with pytest.raises(SimulatedKill):
        await _run(ctx, victim)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("running", 1)  # nobody released it: the worker is dead
    assert (await version_row(raw_db, made.version.id)).status == "processing"
    partial = await chunk_count(raw_db, made.version.id)
    assert partial == (4 if kill_in == "embed" else 0)

    # Its lease expires; the sweeper finds nothing to fail (attempts left); a second worker takes over
    await raw_db.execute(text("UPDATE app.ingest_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": made.job.id})
    await raw_db.commit()
    sweeper = make_sweeper(worker_dbs, tmp_path, only_tenants={academic_world.a.id})
    assert (await sweeper.run_once()).failed_jobs == 0
    survivor = FakePipeline(chunks=4)
    assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), survivor) == ingest.JobOutcome.DONE
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert await chunk_count(raw_db, made.version.id) == 4
    dupes = (await raw_db.execute(text("SELECT count(*) FROM (SELECT ordinal FROM app.chunks WHERE document_version_id = :v GROUP BY ordinal HAVING count(*) > 1) d"), {"v": made.version.id})).scalar_one()
    assert dupes == 0
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.chunk_count) == ("ready", 4)


# ── The poll loop and shutdown ───────────────────────────────────────────────


async def test_the_poll_loop_sleeps_between_empty_polls_and_stops_at_once_when_asked(
    worker_dbs: WorkerDatabases, tmp_path: Path, quiet_queue: None, ingest_slots: SlotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real progress, not just 'still running': one claim, one idle sleep of about 5 s, then a prompt stop."""
    ctx = make_context(worker_dbs, tmp_path, ingest_slots)
    claims: list[str] = []
    sleeps: list[float] = []
    parked = asyncio.Event()

    async def empty_claim(worker_id: str) -> None:
        claims.append(worker_id)

    async def recording_sleep(stop: asyncio.Event, seconds: float) -> None:
        sleeps.append(seconds)
        parked.set()
        await stop.wait()  # an idle sleep that only the stop interrupts

    ctx.queue.claim = empty_claim  # type: ignore[method-assign]
    monkeypatch.setattr(ingest, "_sleep_or_stop", recording_sleep)
    stop = asyncio.Event()
    task = asyncio.create_task(ingest.run_worker(ctx, FakePipeline().stages(), stop))
    await asyncio.wait_for(parked.wait(), 10)
    for _ in range(200):  # a loop that spins would claim again on every turn
        await asyncio.sleep(0)
    assert len(claims) == 1 and len(sleeps) == 1, (claims, sleeps)
    assert 4.0 <= sleeps[0] <= 6.0  # WORKER_POLL_SECONDS (5) with +-20% jitter
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 5.0)  # the stop ends the loop at once
    assert len(claims) == 1  # and it did not claim once more on its way out


async def test_sigterm_lets_a_job_that_finishes_within_the_grace_period_complete(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=replace(FAST, shutdown_grace_seconds=5.0))
    stop = asyncio.Event()
    pipeline = FakePipeline()

    async def stop_during_parse(_c: stages.StageContext) -> None:
        stop.set()
        await asyncio.sleep(0.3)

    pipeline.hooks["parse"] = stop_during_parse
    await asyncio.wait_for(ingest.run_worker(ctx, pipeline.stages(), stop), 20)
    assert (await job_row(raw_db, made.job.id))["status"] == "done"


async def test_sigterm_releases_a_job_that_does_not_finish_in_time(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=replace(FAST, shutdown_grace_seconds=0.5))
    stop = asyncio.Event()
    pipeline = FakePipeline()

    async def hang(_c: stages.StageContext) -> None:
        stop.set()
        await asyncio.sleep(60)

    pipeline.hooks["parse"] = hang
    await asyncio.wait_for(ingest.run_worker(ctx, pipeline.stages(), stop), 20)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["worker_id"], row["lease_expires_at"]) == ("queued", 0, None, None)
    assert (await version_row(raw_db, made.version.id)).status == "queued"
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX signals")
def test_install_signal_handlers_sets_the_stop_event_on_sigterm() -> None:
    async def scenario() -> bool:
        import signal

        stop = asyncio.Event()
        ingest.install_signal_handlers(stop)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 2)
        return stop.is_set()

    assert asyncio.run(scenario())


# ── Tenant context inside a job ──────────────────────────────────────────────


async def test_the_worker_sets_the_tenant_in_every_transaction_and_the_claim_connection_stays_narrow(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    from tests.fixtures.documents import make_document

    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-FOREIGN")
    made = await queued_version(raw_db, a, tmp_path)
    tenant_log = StatementLog(worker_dbs.tenant_engine)
    worker_log = StatementLog(worker_dbs.worker_engine)
    pipeline = FakePipeline()
    seen: dict[str, object] = {}

    async def snoop(sctx: stages.StageContext) -> None:
        await assert_job_session_is_scoped_to_its_tenant(sctx.tenant_sessions, sctx.job)
        async with sctx.session() as db:
            seen["titles"] = set((await db.execute(text("SELECT title FROM app.documents"))).scalars())

    pipeline.hooks["parse"] = snoop
    try:
        assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), pipeline) == ingest.JobOutcome.DONE
    finally:
        tenant_log.close()
        worker_log.close()
    assert "CANARY-B-FOREIGN" not in seen["titles"]  # type: ignore[operator]
    assert_every_transaction_sets_tenant_first(tenant_log, at_least=8)
    assert_only_queue_tables(worker_log)  # app_worker never touched a tenant table
    assert (await job_row(raw_db, made.job.id))["status"] == "done"


async def test_a_stage_that_asks_for_another_tenants_rows_gets_nothing(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    from tests.fixtures.documents import make_document

    a, b = academic_world.a, academic_world.b
    foreign = await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-STAGE")
    made = await queued_version(raw_db, a, tmp_path)
    pipeline = FakePipeline()
    found: dict[str, int] = {}

    async def nosy(sctx: stages.StageContext) -> None:
        async with sctx.session() as db:
            found["docs"] = (await db.execute(text("SELECT count(*) FROM app.documents WHERE tenant_id = :b OR id = :d"), {"b": b.id, "d": foreign.document.id})).scalar_one()
            found["versions"] = (await db.execute(text("SELECT count(*) FROM app.document_versions WHERE id = :v"), {"v": foreign.version.id})).scalar_one()

    pipeline.hooks["parse"] = nosy
    assert await _run(make_context(worker_dbs, tmp_path, ingest_slots), pipeline) == ingest.JobOutcome.DONE
    assert found == {"docs": 0, "versions": 0}
    assert (await job_row(raw_db, made.job.id))["tenant_id"] == a.id


# ── The default entry point ──────────────────────────────────────────────────


def test_the_default_entry_point_refuses_to_start_without_the_embedding_model() -> None:
    """Step 5c-2: the real stages exist; the entry point starts only when the model files verify and the rest is
    configured. Here no model directory is configured, so it exits with code 2 and says why."""
    backend = Path(__file__).resolve().parents[1]
    # explicit _test URLs from the test environment, no .env (the working directory has none); no model directory
    env = {**os.environ, "PYTHONPATH": str(backend), "EMBEDDING_MODEL_DIR": ""}
    result = subprocess.run(
        [sys.executable, "-P", "-m", "app.workers.ingest"],
        cwd=str(Path(os.environ.get("TEMP", "/tmp"))), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "not started" in result.stderr and "EMBEDDING_MODEL_DIR" in result.stderr
    assert "Traceback" not in result.stderr  # a clear message, not a crash


def test_preparing_the_embedder_is_refused_when_the_configuration_is_incomplete() -> None:
    from app.core.config import settings

    class Incomplete:
        def __getattr__(self, name: str) -> object:
            return "" if name == "EMBEDDING_MODEL_DIR" else getattr(settings, name)

    with pytest.raises(ingest.WorkerNotConfigured, match="EMBEDDING_MODEL_DIR"):
        ingest.prepare_embedder(Incomplete())  # type: ignore[arg-type]
