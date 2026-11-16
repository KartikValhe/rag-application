"""
The worker's tenant-context guard (finding 8 of the review of 5b-1/5b-2; app/workers/db.py).

A stage that calls set_config / SET / RESET / DISCARD / DO itself used to pass RLS (a known residual
risk). The tenant engine now rejects such statements unless they come from the helper itself (tagged
through execution options), and the helper checks `current_setting('app.tenant_id')` right before every
commit. Honest limit, also stated in app/workers/db.py and docs/phase5-design.md: this catches mistakes,
NOT a deliberate call through a SQL function that switches and switches back.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slots import SlotManager
from app.db.engine import SessionLocal
from app.workers import db as worker_db
from app.workers import errors, ingest, stages
from app.workers.db import WorkerDatabases
from app.workers.queue import Job
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument, make_document
from tests.fixtures.worker import FakePipeline, job_row, make_context, make_queue, queued_version
from tests.security.tenant_guard_assertions import (
    SWITCH_STATEMENTS,
    assert_commit_refuses_a_changed_tenant,
    assert_stage_statement_rejected,
    assert_tenant_switch_rejected_before_it_takes_effect,
)
from tests.security.worker_assertions import StatementLog


@pytest_asyncio.fixture
async def switch_function(raw_db: AsyncSession) -> AsyncIterator[None]:
    """A SQL function that sets the tenant: the statement that calls it has no set_config in its text."""
    await raw_db.execute(
        text(
            "CREATE OR REPLACE FUNCTION public.test_switch_tenant(t text) RETURNS text "
            "LANGUAGE sql AS $$ SELECT set_config('app.tenant_id', t, true) $$"
        )
    )
    await raw_db.execute(text("GRANT EXECUTE ON FUNCTION public.test_switch_tenant(text) TO app_tenant"))
    await raw_db.commit()
    yield
    await raw_db.execute(text("DROP FUNCTION IF EXISTS public.test_switch_tenant(text)"))
    await raw_db.commit()


async def _claimed(
    worker_dbs: WorkerDatabases, raw_db: AsyncSession, tmp_path: Path, w: WorldTenant
) -> tuple[MadeDocument, Job]:
    made = await queued_version(raw_db, w, tmp_path)
    job = await make_queue(worker_dbs).claim("w")
    assert job is not None
    return made, job


@pytest.mark.parametrize("label", list(SWITCH_STATEMENTS))
async def test_a_stage_that_touches_the_tenant_setting_is_rejected(
    label: str, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None,
) -> None:
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, academic_world.a)
    await assert_stage_statement_rejected(
        worker_dbs.tenant_sessions, job, label, SWITCH_STATEMENTS[label], academic_world.b.id
    )


async def test_a_stage_that_switches_to_tenant_b_never_sees_b(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-SWITCH")
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, a)
    await assert_tenant_switch_rejected_before_it_takes_effect(worker_dbs.tenant_sessions, job, b.id, "CANARY-B-SWITCH")


async def test_switching_to_b_and_back_before_the_commit_is_rejected(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, a)
    with pytest.raises(errors.TenantContextViolation):
        async with worker_dbs.tenant_sessions.for_job(job) as db:
            await db.execute(text("SELECT set_config('app.tenant_id', :b, true)"), {"b": str(b.id)})
            await db.execute(text("SELECT set_config('app.tenant_id', :a, true)"), {"a": str(a.id)})


async def test_a_stage_in_a_real_job_that_sets_the_tenant_fails_the_job_permanently(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-JOB")
    made = await queued_version(raw_db, a, tmp_path)
    pipeline = FakePipeline()
    seen: set[str] = set()

    async def sneaky(sctx: stages.StageContext) -> None:
        async with sctx.session() as db:
            await db.execute(text("SELECT set_config('app.tenant_id', :b, true)"), {"b": str(b.id)})
            seen.update((await db.execute(text("SELECT title FROM app.documents"))).scalars())

    pipeline.hooks["parse"] = sneaky
    outcome = await ingest.run_one(make_context(worker_dbs, tmp_path, ingest_slots), pipeline.stages())
    assert outcome == ingest.JobOutcome.FAILED
    assert "CANARY-B-JOB" not in seen
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["last_error_code"]) == ("failed", "processing_failed")
    assert pipeline.calls == ["validate", "parse"]  # nothing after the offending stage ran


async def test_the_helpers_own_statements_work_and_the_check_runs_right_before_the_commit(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, academic_world.a)
    log = StatementLog(worker_dbs.tenant_engine)
    try:
        async with worker_dbs.tenant_sessions.for_job(job) as db:
            assert (await db.execute(text("SELECT 41 + 1"))).scalar_one() == 42
    finally:
        log.close()
    probes = ("select pg_catalog", "select current_schema", "show ", "select cast")
    statements = [s.lower() for s in log.statements if not s.lower().startswith(probes)]
    assert "set_config('app.tenant_id'" in statements[0]
    assert "select 41" in statements[1]
    assert "current_setting('app.tenant_id'" in statements[-1], "no tenant check right before the commit"


async def test_a_changed_tenant_context_is_caught_right_before_the_commit(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None,
    switch_function: None,
) -> None:
    made, job = await _claimed(worker_dbs, raw_db, tmp_path, academic_world.a)
    await assert_commit_refuses_a_changed_tenant(worker_dbs.tenant_sessions, job, academic_world.b.id, raw_db, made.version.id)


async def test_known_limit_a_sql_function_that_switches_and_switches_back_is_not_caught(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None,
    switch_function: None,
) -> None:
    """
    DOCUMENTED LIMIT, not a protection: the worker holds the credentials of app_tenant and a SQL function
    can set the tenant without the word set_config in the statement. The guard catches mistakes only.
    """
    a, b = academic_world.a, academic_world.b
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, a)
    async with worker_dbs.tenant_sessions.for_job(job) as db:
        await db.execute(text("SELECT public.test_switch_tenant(:b)"), {"b": str(b.id)})
        await db.execute(text("SELECT public.test_switch_tenant(:a)"), {"a": str(a.id)})


async def test_the_guard_is_on_the_engine_not_only_on_the_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld
) -> None:
    async with worker_dbs.tenant_engine.connect() as conn:
        with pytest.raises(errors.TenantContextViolation):
            await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(academic_world.b.id)})


async def test_a_stage_that_opens_the_api_session_directly_is_detected(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld
) -> None:
    """The API's SessionLocal is a different engine. The worker guards it too and nothing may use it."""
    handle: Any = worker_db.install_worker_guards()
    try:
        async with SessionLocal() as session:
            with pytest.raises(errors.TenantContextViolation):
                await session.execute(text("SELECT count(*) FROM app.documents"))
    finally:
        handle.remove()
    async with SessionLocal() as session:  # the guard is gone again: the rest of the suite is unaffected
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_tenant_session_for_refuses_to_fall_back_to_the_api_sessionmaker(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, academic_world.a)
    with pytest.raises(RuntimeError):
        async with worker_db.tenant_session_for(job):
            pass


# ── The every-statement check of the transaction helper (weak test of 5b-2, repaired) ───────────────────


def _log_with(worker_dbs: WorkerDatabases, *transactions: list[str]) -> StatementLog:
    log = StatementLog(worker_dbs.tenant_engine)
    log.close()  # only the container is needed: the statements are filled in by hand
    log.transactions.extend(transactions)
    return log


SET_A = "SELECT set_config('app.tenant_id', $1, true)"
CHECK = "SELECT current_setting('app.tenant_id', true)"


async def test_the_transaction_helper_looks_at_every_statement_not_only_the_first(worker_dbs: WorkerDatabases) -> None:
    from tests.security.worker_assertions import assert_every_transaction_sets_tenant_first as check

    check(_log_with(worker_dbs, [SET_A, "SELECT 1", CHECK]), at_least=1)  # the normal shape passes
    for bad in (
        [SET_A, "SELECT 1", "SELECT set_config('app.tenant_id', $1, true)", CHECK],  # a later set_config
        [SET_A, "SELECT 1 /* app.tenant_id */"],  # a later mention of the setting
        ["SELECT 1", SET_A],  # a query before the tenant is set
    ):
        with pytest.raises(AssertionError):
            check(_log_with(worker_dbs, bad), at_least=1)
