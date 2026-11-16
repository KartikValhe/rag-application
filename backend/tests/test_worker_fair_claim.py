"""
A fair claim (finding 6 of the review of 5b-1/5b-2; app/workers/queue.py).

Before the fix the claim ordered by run_after only, so one tenant's backlog was claimed (and bounced as
busy) ahead of every other tenant while only INGEST_GLOBAL_CONCURRENCY jobs ran platform-wide. Now the
single atomic claim statement skips tenants that are at their running cap, prefers the tenant with the
fewest running jobs and then the oldest queued job, and offers each tenant only its oldest due job per
round (so even simultaneous claims spread over tenants).
"""

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.db import WorkerDatabases
from app.workers.queue import Job
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.worker import FAST, make_queue, queued_version
from tests.security.worker_assertions import (
    assert_no_double_claim,
    assert_small_tenant_not_stuck_behind_backlog,
)

BACKLOG = 50


async def _backlog(raw_db: AsyncSession, w: WorldTenant, tmp_path: Path, n: int = BACKLOG) -> list[uuid.UUID]:
    """`n` queued jobs of one tenant, all OLDER than anything created afterwards. Returns their job ids."""
    first = await queued_version(raw_db, w, tmp_path)
    # One in-flight version per document is enforced by the database, so the backlog is n documents
    await raw_db.execute(
        text(
            "WITH docs AS (INSERT INTO app.documents (tenant_id, id, subject_id, title, created_by) "
            "  SELECT d.tenant_id, gen_random_uuid(), d.subject_id, 'Backlog ' || g, d.created_by "
            "  FROM app.documents d, generate_series(1, :n) g WHERE d.tenant_id = :t AND d.id = :d "
            "  RETURNING tenant_id, id), "
            "vers AS (INSERT INTO app.document_versions (tenant_id, id, document_id, version_no, status, file_kind, "
            "  original_filename, size_bytes, sha256, storage_key, uploaded_by) "
            "  SELECT nd.tenant_id, gen_random_uuid(), nd.id, 1, 'queued', v.file_kind, v.original_filename, "
            "  v.size_bytes, md5(random()::text) || md5(nd.id::text), 't/' || nd.tenant_id || '/d/' || md5(random()::text), "
            "  v.uploaded_by FROM docs nd, app.document_versions v WHERE v.tenant_id = :t AND v.id = :v "
            "  RETURNING tenant_id, id) "
            "INSERT INTO app.ingest_jobs (tenant_id, version_id, status) SELECT tenant_id, id, 'queued' FROM vers"
        ),
        {"n": n - 1, "t": w.id, "d": first.document.id, "v": first.version.id},
    )
    await raw_db.execute(
        text("UPDATE app.ingest_jobs SET run_after = now() - interval '2 hours' WHERE tenant_id = :t AND status = 'queued'"),
        {"t": w.id},
    )
    await raw_db.commit()
    ids = (
        await raw_db.execute(text("SELECT id FROM app.ingest_jobs WHERE tenant_id = :t AND status = 'queued'"), {"t": w.id})
    ).scalars().all()
    assert len(ids) == n
    return list(ids)


async def test_a_small_tenants_job_is_not_stuck_behind_another_tenants_backlog(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await _backlog(raw_db, a, tmp_path)
    small = await queued_version(raw_db, b, tmp_path)  # newer than every job of tenant A
    queue = make_queue(worker_dbs)

    claims: list[Job | None] = [await queue.claim(f"w{i}") for i in range(4)]

    assert_small_tenant_not_stuck_behind_backlog(claims, small_job_id=small.job.id, big_tenant=a.id, within=2)
    assert claims[0] is not None and claims[0].tenant_id == a.id  # the oldest queued job still goes first
    assert claims[2] is None and claims[3] is None  # both tenants are at their running cap: nothing else is claimed


async def test_a_tenant_at_its_running_cap_is_skipped_and_comes_back_when_a_job_ends(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a = academic_world.a
    await _backlog(raw_db, a, tmp_path, n=5)
    queue = make_queue(worker_dbs)
    first = await queue.claim("w1")
    assert first is not None
    assert await queue.claim("w2") is None  # tenant A has one running job (INGEST_TENANT_CONCURRENCY = 1)
    assert await queue.complete(first)
    second = await queue.claim("w2")
    assert second is not None and second.id != first.id


async def test_with_a_higher_cap_the_tenant_with_fewer_running_jobs_goes_first(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await _backlog(raw_db, a, tmp_path, n=10)
    small = await queued_version(raw_db, b, tmp_path)
    queue = make_queue(worker_dbs, replace(FAST, tenant_concurrency=2))
    claims = [await queue.claim(f"w{i}") for i in range(4)]
    assert_small_tenant_not_stuck_behind_backlog(claims, small_job_id=small.job.id, big_tenant=a.id, within=2)
    assert claims[2] is not None and claims[2].tenant_id == a.id  # A is under its cap of 2
    assert claims[3] is None  # A is at 2, B at 1 with nothing left to claim


async def test_simultaneous_claims_never_double_claim_and_spread_over_tenants(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await _backlog(raw_db, a, tmp_path)
    small = await queued_version(raw_db, b, tmp_path)
    queues = [make_queue(worker_dbs) for _ in range(6)]
    results = await asyncio.gather(*(q.claim(f"w{i}") for i, q in enumerate(queues)))
    got = assert_no_double_claim(list(results))
    assert small.job.id in {j.id for j in got}, "tenant B's job was not claimed in the very first round"
    assert sum(1 for j in got if j.tenant_id == a.id) == 1, "one tenant took several claims of the same round"
