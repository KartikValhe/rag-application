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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.slots import SlotManager
from app.db.models.document import Chunk
from app.workers import errors, ingest, stages
from app.workers import queue as queue_module
from app.workers.db import TenantSessions, trusted_statement
from app.workers.queue import Job, JobQueue


def assert_exactly_one_claim(results: list[Job | None]) -> Job:
    """N claimers, one claimable job: exactly one gets it and it is claimed once."""
    got = [r for r in results if r is not None]
    assert len(got) == 1, f"expected exactly one claimer to get the job, {len(got)} did: {got}"
    assert got[0].attempts == 1, f"the job was claimed more than once (attempts={got[0].attempts})"
    return got[0]


def assert_no_double_claim(results: list[Job | None]) -> list[Job]:
    """Several simultaneous claimers: every job is claimed at most once, and only once per attempt."""
    got = [r for r in results if r is not None]
    ids = [j.id for j in got]
    assert len(ids) == len(set(ids)), f"a job was claimed by two workers: {ids}"
    assert all(j.attempts == 1 for j in got), "a job was claimed more than once"
    return got


def assert_small_tenant_not_stuck_behind_backlog(
    claims: list[Job | None], *, small_job_id: uuid.UUID, big_tenant: uuid.UUID, within: int
) -> None:
    """
    One tenant has a big, OLDER backlog; another tenant has a single newer job. The small tenant's job
    must be among the first `within` claims, not behind the whole backlog.
    """
    ids = [c.id if c is not None else None for c in claims]
    assert small_job_id in ids[:within], (
        f"the small tenant's job was claimed at position {ids.index(small_job_id) + 1 if small_job_id in ids else 'never'},"
        f" behind {sum(1 for c in claims[:within] if c is not None and c.tenant_id == big_tenant)} job(s) of the big tenant's backlog"
    )


def assert_tenant_at_its_cap_is_skipped(claims: list[Job | None]) -> None:
    """One tenant with a backlog and a running job: the next claim must find nothing (INGEST_TENANT_CONCURRENCY = 1)."""
    assert claims[0] is not None, "the first claim found nothing"
    assert all(c is None for c in claims[1:]), "a tenant that already has a running job was claimed again"


def assert_one_claim_per_tenant_per_round(results: list[Job | None]) -> None:
    """Simultaneous claimers: a tenant's backlog may not take several claims of the same round."""
    got = [r for r in results if r is not None]
    per_tenant: dict[uuid.UUID, int] = {}
    for job in got:
        per_tenant[job.tenant_id] = per_tenant.get(job.tenant_id, 0) + 1
    assert all(n == 1 for n in per_tenant.values()), f"one tenant took several claims of the same round: {per_tenant}"


@dataclass
class ClaimEventLog:
    """
    Records, in order and at the driver level, every claim statement and every commit of an engine.
    It is how a round test PROVES that its claim statements really overlapped (see
    assert_claims_overlapped). Use it as a context manager around the round.
    """

    engine: AsyncEngine
    events: list[tuple[str, int]] = field(default_factory=list)  # ("claim" | "commit", id of the connection)

    def __enter__(self) -> "ClaimEventLog":
        sync = self.engine.sync_engine
        event.listen(sync, "after_cursor_execute", self._on_execute)
        event.listen(sync, "commit", self._on_commit)
        return self

    def __exit__(self, *_exc: object) -> None:
        sync = self.engine.sync_engine
        event.remove(sync, "after_cursor_execute", self._on_execute)
        event.remove(sync, "commit", self._on_commit)

    def _on_execute(self, conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        # Match the claim statement itself (the sabotaged variants of CLAIM_SQL keep this shape).
        if statement.lstrip().startswith("WITH next AS") and "app.ingest_jobs" in statement:
            self.events.append(("claim", id(conn)))

    def _on_commit(self, conn: Any) -> None:
        self.events.append(("commit", id(conn)))


def assert_claims_overlapped(events: list[tuple[str, int]], *, claimers: int) -> None:
    """
    A round test is only deterministic when every claim statement ran, on its own connection, BEFORE the
    first commit: then no statement could see another one's claim (READ COMMITTED). If any commit comes
    between two statements, a later statement sees the earlier claim and the running-cap hides the very
    rule the round test is about, so the test would depend on timing again.
    """
    claims = [i for i, (kind, _) in enumerate(events) if kind == "claim"]
    commits = [i for i, (kind, _) in enumerate(events) if kind == "commit"]
    assert len(claims) == claimers, f"expected {claimers} claim statements, saw {len(claims)}: {events}"
    assert len({conn for kind, conn in events if kind == "claim"}) == claimers, (
        f"the claim statements did not run on {claimers} separate connections: {events}"
    )
    assert commits, f"nothing was committed: {events}"
    assert min(commits) > max(claims), f"a commit came before the last claim statement ran, the round did not overlap: {events}"


async def claim_round_overlapped(queue: JobQueue, claimers: int) -> list[Job | None]:
    """
    One claim round that is DETERMINISTICALLY overlapped: `claimers` connections each run the real claim
    statement (queue_module.CLAIM_SQL, read at call time so a test can sabotage it) one after another and
    NONE of them commits until all have executed. Under READ COMMITTED no statement can see another one's
    uncommitted claim, which is exactly the "two statements take their snapshots before the first commit"
    case. asyncio.gather over pooled connections does not guarantee that overlap, so a round test built on it
    depended on timing. The result is built from the rows exactly like JobQueue.claim does.

    Every connection is closed in the end (an open transaction is rolled back), also when a statement or an
    assertion fails. The overlap itself is proven from the driver events before the results are returned.
    """
    results: list[Job | None] = []
    connections: list[AsyncConnection] = []
    with ClaimEventLog(queue.engine) as log:
        try:
            for i in range(claimers):
                worker = f"w{i}"
                conn = await queue.engine.connect()
                connections.append(conn)
                rows = (await conn.execute(text(queue_module.CLAIM_SQL), queue.claim_params(worker))).fetchall()
                results.append(
                    Job(id=rows[0][0], tenant_id=rows[0][1], version_id=rows[0][2], attempts=rows[0][3], worker_id=worker)
                    if rows
                    else None
                )
            for conn in connections:  # only now, after every statement has run
                await conn.commit()
        finally:
            for conn in connections:
                await conn.close()  # rolls back whatever is still open
    assert_claims_overlapped(log.events, claimers=claimers)
    return results


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
        # The tenant guard rejects an untrusted statement that mentions app.tenant_id: use the module's own
        current = (await db.execute(trusted_statement("SELECT current_setting('app.tenant_id', true)"))).scalar_one()
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
    """
    Every transaction of a tenant session sets app.tenant_id exactly once, as its FIRST statement, and no
    other statement of it touches the setting. The only other mention allowed is the helper's own check
    right before the commit (a SELECT of current_setting). EVERY statement is checked, not just the first:
    a set_config in the middle of a transaction (a stage that switches tenant) must fail here too.
    """
    groups = [g for g in log.transactions if g and not all(s.lower().startswith(_DIALECT_PROBES) for s in g)]
    assert len(groups) >= at_least, f"expected at least {at_least} transactions, saw {len(groups)}"
    for group in groups:
        first = group[0].lower()
        assert "set_config('app.tenant_id'" in first, f"a transaction ran a query before setting the tenant: {group[0][:120]}"
        for later in group[1:]:
            low = later.lower()
            if "set_config" in low or "app.tenant_id" in low:
                assert low.lstrip().startswith("select current_setting('app.tenant_id'"), (
                    f"a later statement of a transaction touched the tenant setting: {later[:120]}"
                )


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
