"""
Prove-it-fails for the protections added by the review of 5b-1/5b-2 (findings 6, 8 and 10).

Each test first runs the scenario with the protection ON (the shared helper passes), then switches the
protection off, runs the SAME scenario again and requires the helper to RAISE. That shows the helper
really checks the protection and does not pass by accident.

  1. fair claim:     the old claim SQL (run_after order, no cap)            -> the small-tenant helper raises.
  2. fair claim:     the running-cap condition removed                       -> the cap helper raises.
  3. fair claim:     the one-job-per-tenant condition removed                -> the per-round helper raises.
  4. tenant guard:   the statement guard removed from the tenant engine      -> the switch helper raises.
  5. tenant guard:   the check right before the commit removed               -> the commit helper raises.
  6. header layer:   the headers not applied                                 -> the headers helper raises (real app 401).
  7. header layer:   any route CSP kept                                      -> the headers helper raises (weak CSP).

TEST database only: the autouse fixture asserts current_database() ends in _test before anything runs.
"""

import os
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core import middleware
from app.workers import db as worker_db
from app.workers import queue as queue_module
from app.workers.db import WorkerDatabases
from app.workers.queue import Job
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.worker import make_queue, queued_version
from tests.security.header_assertions import assert_security_headers
from tests.security.tenant_guard_assertions import (
    assert_commit_refuses_a_changed_tenant,
    assert_tenant_switch_rejected_before_it_takes_effect,
)
from tests.security.worker_assertions import (
    assert_claims_overlapped,
    assert_no_double_claim,
    assert_one_claim_per_tenant_per_round,
    assert_small_tenant_not_stuck_behind_backlog,
    assert_tenant_at_its_cap_is_skipped,
    claim_round_overlapped,
)
from tests.test_security_headers_layer import _call, _responder
from tests.test_worker_fair_claim import _backlog
from tests.test_worker_tenant_guard import _claimed, switch_function  # noqa: F401  (a fixture, used by name)


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    finally:
        await engine.dispose()
    assert name.endswith("_test"), f"prove-it tests must run against the test database, not {name!r}"


# ── Fair claim ───────────────────────────────────────────────────────────────

# The claim of the 5b-2 delivery: oldest run_after first, no cap, no per-tenant rule
OLD_CLAIM_SQL = f"""
WITH next AS (
  SELECT tenant_id, id FROM app.ingest_jobs
  WHERE ({queue_module.QUEUED_DUE_SQL}
      {queue_module.RECLAIM_EXPIRED_SQL})
    AND attempts < :max_attempts
  ORDER BY run_after, created_at LIMIT 1
  FOR UPDATE SKIP LOCKED)
UPDATE app.ingest_jobs j
SET status = 'running', attempts = j.attempts + 1,
    lease_expires_at = {queue_module.NOW} + make_interval(secs => :lease), worker_id = :worker, updated_at = {queue_module.NOW}
FROM next WHERE j.tenant_id = next.tenant_id AND j.id = next.id
RETURNING j.id, j.tenant_id, j.version_id, j.attempts
"""


async def _reset(raw_db: AsyncSession, academic_world: AcademicWorld) -> None:
    """Finish whatever the first run left, so the second run starts from fresh queued jobs."""
    await raw_db.execute(
        text("UPDATE app.ingest_jobs SET status = 'done', lease_expires_at = NULL, worker_id = NULL WHERE tenant_id IN (:a, :b)"),
        {"a": academic_world.a.id, "b": academic_world.b.id},
    )
    await raw_db.commit()


async def _small_tenant_scenario(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    await _backlog(raw_db, a, tmp_path, n=8)
    small = await queued_version(raw_db, b, tmp_path)
    queue = make_queue(worker_dbs)
    claims = [await queue.claim(f"w{i}") for i in range(3)]
    assert_small_tenant_not_stuck_behind_backlog(claims, small_job_id=small.job.id, big_tenant=a.id, within=2)


async def test_the_old_claim_is_caught_by_the_small_tenant_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _small_tenant_scenario(worker_dbs, academic_world, raw_db, tmp_path)  # protection ON: passes

    await _reset(raw_db, academic_world)
    monkeypatch.setattr(queue_module, "CLAIM_SQL", OLD_CLAIM_SQL)
    with pytest.raises(AssertionError, match="small tenant's job was claimed at position"):
        await _small_tenant_scenario(worker_dbs, academic_world, raw_db, tmp_path)


async def _cap_scenario(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    await _backlog(raw_db, academic_world.a, tmp_path, n=4)
    queue = make_queue(worker_dbs)
    assert_tenant_at_its_cap_is_skipped([await queue.claim(f"w{i}") for i in range(3)])


async def test_removing_the_running_cap_is_caught_by_the_cap_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _cap_scenario(worker_dbs, academic_world, raw_db, tmp_path)  # protection ON: passes

    assert "AND live.running < :tenant_cap" in queue_module.CLAIM_SQL
    await _reset(raw_db, academic_world)
    monkeypatch.setattr(queue_module, "CLAIM_SQL", queue_module.CLAIM_SQL.replace("AND live.running < :tenant_cap", ""))
    with pytest.raises(AssertionError, match="already has a running job was claimed again"):
        await _cap_scenario(worker_dbs, academic_world, raw_db, tmp_path)


async def _round_scenario(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> list[Job | None]:
    await _backlog(raw_db, academic_world.a, tmp_path, n=8)
    await queued_version(raw_db, academic_world.b, tmp_path)
    # Six claim statements that all run before the first commit (proven inside the helper), so the
    # running-cap cannot hide the rule: the result does not depend on timing.
    results = await claim_round_overlapped(make_queue(worker_dbs), 6)
    assert_no_double_claim(results)
    assert_one_claim_per_tenant_per_round(results)
    return results


async def test_removing_the_one_job_per_tenant_rule_is_caught_by_the_round_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = await _round_scenario(worker_dbs, academic_world, raw_db, tmp_path)  # protection ON: passes
    # exactly one claim for each of the two tenants, the other four claimers find nothing
    assert sorted(str(j.tenant_id) for j in results if j is not None) == sorted([str(academic_world.a.id), str(academic_world.b.id)])
    assert sum(1 for j in results if j is None) == 4

    sql = queue_module.CLAIM_SQL
    start = sql.index("    AND j.id = (SELECT q.id")
    end = sql.index("  ORDER BY live.running")
    await _reset(raw_db, academic_world)
    monkeypatch.setattr(queue_module, "CLAIM_SQL", sql[:start] + sql[end:])
    with pytest.raises(AssertionError, match="several claims of the same round"):
        await _round_scenario(worker_dbs, academic_world, raw_db, tmp_path)


def test_the_overlap_check_accepts_only_claims_that_all_ran_before_the_first_commit() -> None:
    good = [("claim", 1), ("claim", 2), ("claim", 3), ("commit", 1), ("commit", 2), ("commit", 3)]
    assert_claims_overlapped(good, claimers=3)  # passes

    commit_between = [("claim", 1), ("commit", 1), ("claim", 2), ("claim", 3), ("commit", 2), ("commit", 3)]
    with pytest.raises(AssertionError, match="the round did not overlap"):
        assert_claims_overlapped(commit_between, claimers=3)
    serialized = [("claim", 1), ("commit", 1), ("claim", 2), ("commit", 2), ("claim", 3), ("commit", 3)]
    with pytest.raises(AssertionError, match="the round did not overlap"):
        assert_claims_overlapped(serialized, claimers=3)
    with pytest.raises(AssertionError, match="expected 3 claim statements, saw 2"):
        assert_claims_overlapped([("claim", 1), ("claim", 2), ("commit", 1), ("commit", 2)], claimers=3)
    with pytest.raises(AssertionError, match="did not run on 3 separate connections"):
        assert_claims_overlapped([("claim", 1), ("claim", 1), ("claim", 2), ("commit", 1)], claimers=3)
    with pytest.raises(AssertionError, match="nothing was committed"):
        assert_claims_overlapped([("claim", 1), ("claim", 2), ("claim", 3)], claimers=3)


async def test_the_overlapped_round_gives_every_connection_back_when_a_statement_fails(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _backlog(raw_db, academic_world.a, tmp_path, n=3)
    queue = make_queue(worker_dbs)
    real_params = queue.claim_params

    def failing_params(worker_id: str) -> dict[str, object]:
        if worker_id == "w2":
            raise RuntimeError("the third claimer fails")
        return real_params(worker_id)

    monkeypatch.setattr(queue, "claim_params", failing_params)
    with pytest.raises(RuntimeError, match="the third claimer fails"):
        await claim_round_overlapped(queue, 6)

    assert worker_dbs.worker_engine.pool.checkedout() == 0  # type: ignore[attr-defined]  # every connection came back
    running = (
        await raw_db.execute(
            text("SELECT count(*) FROM app.ingest_jobs WHERE tenant_id = :t AND status = 'running'"), {"t": academic_world.a.id}
        )
    ).scalar_one()
    assert running == 0  # the claims of the first two statements were rolled back, not committed


# ── Tenant guard ─────────────────────────────────────────────────────────────


async def test_removing_the_statement_guard_is_caught_by_the_switch_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    from tests.fixtures.documents import make_document

    a, b = academic_world.a, academic_world.b
    await make_document(raw_db, b, subject="Physics", classes=("10-B",), uploader=b.t2, status="published", title="CANARY-B-PROVE")
    _, job = await _claimed(worker_dbs, raw_db, tmp_path, a)

    await assert_tenant_switch_rejected_before_it_takes_effect(worker_dbs.tenant_sessions, job, b.id, "CANARY-B-PROVE")  # ON

    assert worker_dbs.tenant_guard is not None
    worker_dbs.tenant_guard.remove()
    with pytest.raises(AssertionError, match="read another tenant's rows|switched to another tenant"):
        await assert_tenant_switch_rejected_before_it_takes_effect(worker_dbs.tenant_sessions, job, b.id, "CANARY-B-PROVE")


@pytest.mark.usefixtures("switch_function")
async def test_removing_the_pre_commit_check_is_caught_by_the_commit_helper(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made, job = await _claimed(worker_dbs, raw_db, tmp_path, academic_world.a)
    b = academic_world.b.id

    await assert_commit_refuses_a_changed_tenant(worker_dbs.tenant_sessions, job, b, raw_db, made.version.id)  # ON

    monkeypatch.setattr(worker_db, "assert_tenant_unchanged", lambda *_a, **_k: None)
    with pytest.raises(AssertionError, match="was committed"):
        await assert_commit_refuses_a_changed_tenant(worker_dbs.tenant_sessions, job, b, raw_db, made.version.id)


# ── Header layer ─────────────────────────────────────────────────────────────


async def test_not_applying_the_headers_is_caught_by_the_headers_helper_on_the_real_app(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"file": ("a.txt", b"x", "text/plain")}
    resp = await client.post("/api/v1/documents", files=files)  # the body guard's 401
    assert resp.status_code == 401
    assert_security_headers(resp.headers, label="401")  # protection ON: passes

    monkeypatch.setattr(middleware.SecurityHeadersMiddleware, "_apply", staticmethod(lambda *_a, **_k: None))
    resp = await client.post("/api/v1/documents", files=files)
    assert resp.status_code == 401
    with pytest.raises(AssertionError, match="no X-Content-Type-Options"):
        assert_security_headers(resp.headers, label="401")


async def test_keeping_any_route_csp_is_caught_by_the_headers_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    weak = "default-src *"
    assert_security_headers(await _call(_responder(200, weak)), label="weak route CSP")  # protection ON: replaced

    monkeypatch.setattr(middleware, "_route_csp_is_allowed", lambda _value: True)
    with pytest.raises(AssertionError, match="weak or unexpected Content-Security-Policy"):
        assert_security_headers(await _call(_responder(200, weak)), label="weak route CSP")
