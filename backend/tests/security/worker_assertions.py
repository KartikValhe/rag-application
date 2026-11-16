"""
tests/security/worker_assertions.py
───────────────────────────────────
Shared assertions for the ingestion worker (design C2 "prove-it-fails"). Every protection is
checked by a helper, so tests/security/test_prove_it_fails_worker.py can switch the protection off
and show that the SAME helper then raises.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.slots import SlotManager
from app.db.models.document import Chunk
from app.workers import errors, ingest, stages
from app.workers import queue as queue_module
from app.workers.db import TenantSessions
from app.workers.queue import Job, JobQueue


def assert_exactly_one_claim(results: list[Job | None]) -> Job:
    """N claimers, one claimable job: exactly one gets it and it is claimed once."""
    got = [r for r in results if r is not None]
    assert len(got) == 1, f"expected exactly one claimer to get the job, {len(got)} did: {got}"
    assert got[0].attempts == 1, f"the job was claimed more than once (attempts={got[0].attempts})"
    return got[0]


async def assert_locked_job_is_skipped(queue: JobQueue, engine: AsyncEngine, *, timeout: float = 3.0) -> tuple[Job, Job]:
    """
    Two claimable jobs. Claimer 1 takes one and keeps its transaction OPEN (row locked). Claimer 2
    must not wait for that lock and must not get the same job: it gets the other one at once.
    Without FOR UPDATE SKIP LOCKED it blocks (timeout) or claims the same job.
    """
    async with engine.connect() as holder:
        row = (await holder.execute(text(queue_module.CLAIM_SQL), queue.claim_params("holder"))).mappings().first()
        assert row is not None, "the first claimer found nothing to claim"
        first = Job(id=row["id"], tenant_id=row["tenant_id"], version_id=row["version_id"], attempts=row["attempts"], worker_id="holder")
        try:
            second = await asyncio.wait_for(queue.claim("second"), timeout)
        except TimeoutError:
            raise AssertionError("the second claimer blocked on a locked row (FOR UPDATE SKIP LOCKED is missing)") from None
        await holder.commit()
    assert second is not None, "the second claimer found no job although another one was claimable"
    assert second.id != first.id, "two workers claimed the same job"
    return first, second


async def assert_expired_lease_reclaimed(queue: JobQueue, job_id: uuid.UUID, previous_attempts: int) -> Job:
    """A running job whose lease has expired is claimable again and its attempts go up by one."""
    job = await queue.claim("reclaimer")
    assert job is not None, "an expired running lease was not reclaimed"
    assert job.id == job_id, "a different job was claimed"
    assert job.attempts == previous_attempts + 1, f"attempts did not increase ({job.attempts})"
    return job


async def assert_job_session_is_scoped_to_its_tenant(sessions: TenantSessions, job: Job | Any) -> None:
    """The session built for a job has the job row's tenant and sees no other tenant's rows."""
    async with sessions.for_job(job) as db:
        current = (await db.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
        assert current == str(job.tenant_id), f"session tenant {current} is not the job row's tenant {job.tenant_id}"
        seen = set((await db.execute(text("SELECT DISTINCT tenant_id FROM app.documents"))).scalars())
        assert seen <= {job.tenant_id}, f"the session saw another tenant's rows: {seen - {job.tenant_id}}"


async def assert_no_slots_held(slots: SlotManager, tenant_id: uuid.UUID) -> None:
    """After a job ended (any way) both of its slots are free."""
    held_global = await slots.count(ingest.GLOBAL_SLOT_KEY)
    held_tenant = await slots.count(ingest.tenant_slot_key(tenant_id))
    assert held_global == 0, f"{held_global} global ingest slot(s) were not released"
    assert held_tenant == 0, f"{held_tenant} tenant ingest slot(s) were not released"


@dataclass
class StatementLog:
    """Records, per transaction, the statements an engine executes (to see what runs first)."""

    engine: AsyncEngine
    transactions: list[list[str]] = field(default_factory=list)
    _open: dict[int, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sync = self.engine.sync_engine
        event.listen(sync, "begin", self._on_begin)
        event.listen(sync, "before_cursor_execute", self._on_execute)

    def close(self) -> None:
        sync = self.engine.sync_engine
        event.remove(sync, "begin", self._on_begin)
        event.remove(sync, "before_cursor_execute", self._on_execute)

    def _on_begin(self, conn: Any) -> None:
        group: list[str] = []
        self._open[id(conn)] = group
        self.transactions.append(group)

    def _on_execute(self, conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        self._open.setdefault(id(conn), self._new_group()).append(statement)

    def _new_group(self) -> list[str]:
        group: list[str] = []
        self.transactions.append(group)
        return group

    @property
    def statements(self) -> list[str]:
        return [s for group in self.transactions for s in group]


_DIALECT_PROBES = ("select pg_catalog.version", "select current_schema", "show standard_conforming", "select cast('test")


def assert_every_transaction_sets_tenant_first(log: StatementLog, *, at_least: int) -> None:
    """Every transaction of a tenant session sets app.tenant_id before any other statement."""
    groups = [g for g in log.transactions if g and not all(s.lower().startswith(_DIALECT_PROBES) for s in g)]
    assert len(groups) >= at_least, f"expected at least {at_least} transactions, saw {len(groups)}"
    for group in groups:
        first = group[0].lower()
        assert "set_config('app.tenant_id'" in first, f"a transaction ran a query before setting the tenant: {group[0][:120]}"


def assert_only_queue_tables(log: StatementLog) -> None:
    """The claim connection (app_worker) only ever touches the queue and the tenant id list."""
    import re

    allowed = {"ingest_jobs", "tenants"}
    for statement in log.statements:
        for table in re.findall(r"\bapp\.([a-z_]+)", statement.lower()):
            assert table in allowed, f"the worker connection touched {table}: {statement[:120]}"


async def _version_snapshot(db: AsyncSession, version_id: uuid.UUID) -> tuple[Any, ...]:
    row = (
        await db.execute(
            text("SELECT status, stage, error_code, chunk_count FROM app.document_versions WHERE id = :v"),
            {"v": version_id},
        )
    ).one()
    chunk_ids = sorted(
        (await db.execute(text("SELECT id FROM app.chunks WHERE document_version_id = :v"), {"v": version_id})).scalars()
    )
    return (*row, tuple(chunk_ids))


async def assert_stale_worker_cannot_write(
    ctx: ingest.WorkerContext, stale: Job, raw_db: AsyncSession, version_id: uuid.UUID
) -> None:
    """
    `stale` is a job that another worker reclaimed (its lease expired). Every way the stale worker can
    write version, chunk or job state must raise JobStopped("job_lost") and change nothing: not the
    chunks, not the version's status, stage or error code. Without the ownership check the first
    write succeeds and this helper raises AssertionError.
    """
    row = (
        await raw_db.execute(
            text(
                "SELECT v.document_id, d.subject_id FROM app.document_versions v "
                "JOIN app.documents d ON d.tenant_id = v.tenant_id AND d.id = v.document_id WHERE v.id = :v"
            ),
            {"v": version_id},
        )
    ).one()
    document_id, subject_id = row

    async def write_chunks() -> None:
        async with stages.active_version_session(ctx.tenant_sessions, stale) as db:
            db.add(
                Chunk(
                    tenant_id=stale.tenant_id, document_version_id=version_id, document_id=document_id,
                    subject_id=subject_id, ordinal=99, page_start=1, page_end=1, heading_path="stale",
                    content="written by a stale worker", content_sha256="0" * 64, token_count=3, script="latn",
                )
            )

    async def mark_ready() -> None:
        async with stages.active_version_session(ctx.tenant_sessions, stale) as db:
            await db.execute(
                text("UPDATE app.document_versions SET status = 'ready', stage = NULL WHERE tenant_id = :t AND id = :v"),
                {"t": stale.tenant_id, "v": version_id},
            )

    before = await _version_snapshot(raw_db, version_id)
    attempts = {
        "write chunks": write_chunks,
        "mark the version ready": mark_ready,
        "enter a stage": lambda: ingest._enter_stage(ctx, stale, "chunk"),
        "set the version failed": lambda: ingest._set_version(ctx, stale, "failed", "scanned_pdf"),
        "put the version back to queued": lambda: ingest._set_version(ctx, stale, "queued", None),
    }
    for label, attempt in attempts.items():
        try:
            await attempt()
        except errors.JobStopped as stop:
            assert stop.reason == "job_lost", f"stale worker ({label}) was stopped for {stop.reason!r}, not job_lost"
        else:
            raise AssertionError(f"a stale worker could {label} after its job was reclaimed")
    after = await _version_snapshot(raw_db, version_id)
    assert after == before, f"a stale worker changed the new owner's rows: {before} -> {after}"
