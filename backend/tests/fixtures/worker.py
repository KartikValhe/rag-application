"""
tests/fixtures/worker.py
────────────────────────
Fixtures for the ingestion worker tests (step 5b-2):

  - `worker_dbs`:   the worker's two engines (app_worker for the queue, app_tenant for tenant
                    sessions), built from the explicit _test URLs in the environment (conftest
                    rewrites them), never from .env; the roles are verified.
  - `quiet_queue`:  parks every claimable job left behind by other tests (run_after and leases
                    moved 100 years ahead), so the real, unfiltered claim SQL can be tested.
  - `ingest_slots`: a SlotManager with the global ingest slot key emptied before and after.
  - `FakePipeline`: injectable fake stages (validate, parse, chunk, embed, index) that record their
                    calls, write real chunk rows idempotently and can fail on demand.
  - `queued_version(...)`: a queued version with its job and a stored file (realistic world data).
"""

import asyncio
import hashlib
import os
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.core.slots import DEFAULT_UNAVAILABLE_MESSAGE, SlotLease, SlotLimit, SlotManager
from app.db.models.document import Chunk, DocumentVersion
from app.services import document_store
from app.workers import ingest, stages
from app.workers.db import TenantRef, TenantSessions, WorkerDatabases
from app.workers.queue import Job, JobQueue
from tests.fixtures.academic_world import WorldTenant
from tests.fixtures.documents import MadeDocument, make_document

PARSE = stages.ParseSettings(timeout_seconds=20.0, max_output_bytes=1024 * 1024, memory_limit_mb=1024, cpu_seconds=60)

# Fast but otherwise production-shaped settings: short heartbeat and lease so tests run in seconds.
FAST = ingest.IngestSettings(
    global_concurrency=2,
    tenant_concurrency=1,
    lease_seconds=300.0,
    heartbeat_seconds=3600.0,  # tests that are about the heartbeat inject a manual tick (ManualTicker)
    max_job_seconds=60.0,
    max_attempts=3,
    retry_base_seconds=30.0,
    retry_jitter_seconds=15.0,
    busy_min_seconds=15.0,
    busy_max_seconds=45.0,
    poll_seconds=5.0,
    shutdown_grace_seconds=0.5,
    parse=PARSE,
)


class FakeClock:
    """A clock the test moves by hand. The queue, the worker and the sweeper pass it to SQL as :now."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    __call__ = now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def worker_dbs() -> AsyncIterator[WorkerDatabases]:
    tenant_url = os.environ["DATABASE_URL"]
    worker_url = os.environ["WORKER_DATABASE_URL"]
    for url in (tenant_url, worker_url):
        assert (make_url(url).database or "").endswith("_test"), "worker tests must use the test database"
    dbs = WorkerDatabases.from_urls(tenant_url=tenant_url, worker_url=worker_url)
    await dbs.verify_roles()
    yield dbs
    await dbs.dispose()


@pytest_asyncio.fixture
async def quiet_queue(raw_db: AsyncSession) -> None:
    await raw_db.execute(
        text(
            "UPDATE app.ingest_jobs SET run_after = now() + interval '100 years' "
            "WHERE status = 'queued' AND run_after < now() + interval '50 years'"
        )
    )
    await raw_db.execute(
        text(
            "UPDATE app.ingest_jobs SET lease_expires_at = now() + interval '100 years' "
            "WHERE status = 'running' AND lease_expires_at < now() + interval '50 years'"
        )
    )
    await raw_db.commit()


@pytest_asyncio.fixture
async def ingest_slots() -> AsyncIterator[SlotManager]:
    manager = SlotManager()
    try:
        redis = await redis_client.get_redis()
        await redis.delete(ingest.GLOBAL_SLOT_KEY)
    except Exception:  # development fallback: the per-process in-memory slots of this manager
        redis = None
    yield manager
    if redis is not None:
        await redis.delete(ingest.GLOBAL_SLOT_KEY)


def make_queue(
    dbs: WorkerDatabases, config: ingest.IngestSettings = FAST, *, clock: Callable[[], datetime] | None = None
) -> JobQueue:
    return JobQueue(
        dbs.worker_engine,
        max_attempts=config.max_attempts,
        lease_seconds=config.lease_seconds,
        tenant_concurrency=config.tenant_concurrency,
        clock=clock,
    )


def make_context(
    dbs: WorkerDatabases,
    store_root: Path,
    slots: SlotManager,
    *,
    config: ingest.IngestSettings = FAST,
    seed: int = 7,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ingest.WorkerContext:
    extra: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    return ingest.WorkerContext(
        queue=make_queue(dbs, config, clock=clock),
        tenant_sessions=dbs.tenant_sessions,
        store=document_store.LocalPrivateStore(store_root),
        slots=slots,
        config=config,
        rng=random.Random(seed),
        worker_id=f"test-worker-{uuid.uuid4().hex[:8]}",
        clock=clock,
        **extra,
    )


async def queued_version(
    db: AsyncSession, w: WorldTenant, store_root: Path, *, subject: str = "Math", classes: tuple[str, ...] = ("10-A",),
    data: bytes | None = None,
) -> MadeDocument:
    body = data if data is not None else f"Unit 1: {subject}\n\nSyllabus text {uuid.uuid4().hex}\n".encode()
    return await make_document(
        db, w, subject=subject, classes=classes, uploader=w.t1, status="queued", data=body, store_root=store_root
    )


async def version_row(db: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
    # populate_existing refreshes the identity-mapped row without expiring (and so lazy-loading) others
    result = await db.execute(
        select(DocumentVersion).where(DocumentVersion.id == version_id).execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def job_row(db: AsyncSession, job_id: uuid.UUID) -> dict[str, Any]:
    row = (await db.execute(text("SELECT * FROM app.ingest_jobs WHERE id = :id"), {"id": job_id})).mappings().one_or_none()
    return dict(row) if row is not None else {}


async def chunk_count(db: AsyncSession, version_id: uuid.UUID) -> int:
    return int(
        (await db.execute(select(func.count()).select_from(Chunk).where(Chunk.document_version_id == version_id))).scalar_one()
    )


# ── Fake stages ──────────────────────────────────────────────────────────────

StageHook = Callable[[stages.StageContext], Awaitable[None]]


@dataclass
class FakePipeline:
    """Fake stages. Each records its name; `hooks[name]` runs first (to fail, hang or snoop)."""

    chunks: int = 3
    calls: list[str] = field(default_factory=list)
    hooks: dict[str, StageHook] = field(default_factory=dict)

    async def _hook(self, name: str, ctx: stages.StageContext) -> None:
        self.calls.append(name)
        if name in self.hooks:
            await self.hooks[name](ctx)

    async def validate(self, ctx: stages.StageContext) -> None:
        await self._hook("validate", ctx)

    async def parse(self, ctx: stages.StageContext) -> None:
        await self._hook("parse", ctx)
        ctx.results["pages"] = [f"page {i}" for i in range(self.chunks)]

    async def chunk(self, ctx: stages.StageContext) -> None:
        """Writes real chunk rows, idempotently (delete then insert), like the 5c chunker will."""
        await self._hook("chunk", ctx)
        async with ctx.session() as db:
            await db.execute(
                delete(Chunk).where(Chunk.tenant_id == ctx.job.tenant_id, Chunk.document_version_id == ctx.version.id)
            )
            for i, page in enumerate(ctx.results["pages"]):
                content = f"{page} of version {ctx.version.id}"
                db.add(
                    Chunk(
                        tenant_id=ctx.job.tenant_id,
                        document_version_id=ctx.version.id,
                        document_id=ctx.version.document_id,
                        subject_id=ctx.version.subject_id,
                        ordinal=i,
                        page_start=i + 1,
                        page_end=i + 1,
                        heading_path="Unit 1",
                        content=content,
                        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                        token_count=5,
                        script="latn",
                    )
                )

    async def embed(self, ctx: stages.StageContext) -> None:
        await self._hook("embed", ctx)

    async def index(self, ctx: stages.StageContext) -> None:
        """The index stage finishes the version (5c: the same transaction also publishes it)."""
        await self._hook("index", ctx)
        async with ctx.session() as db:
            count = (
                await db.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.document_version_id == ctx.version.id)
                )
            ).scalar_one()
            await db.execute(
                update(DocumentVersion)
                .where(DocumentVersion.tenant_id == ctx.job.tenant_id, DocumentVersion.id == ctx.version.id)
                .values(status="ready", stage=None, chunk_count=count, flagged_chunk_count=0)
            )

    def stages(self) -> stages.IngestStages:
        return stages.IngestStages(
            validate=self.validate, parse=self.parse, chunk=self.chunk, embed=self.embed, index=self.index
        )


def raise_once(exc_factory: Callable[[], BaseException]) -> StageHook:
    """A hook that raises on its first call only."""
    state = {"n": 0}

    async def hook(_ctx: stages.StageContext) -> None:
        state["n"] += 1
        if state["n"] == 1:
            raise exc_factory()

    return hook


# ── Deterministic time and outages ───────────────────────────────────────────


class ManualTicker:
    """
    Replaces the heartbeat's wait between two renewals (`WorkerContext.sleep`): the heartbeat parks
    until the test calls `beat()`, which lets exactly one renewal round run and returns when the
    heartbeat is parked again. No wall-clock time is involved.
    """

    def __init__(self) -> None:
        self._parked = asyncio.Event()
        self._go = asyncio.Event()
        self.beats = 0

    async def sleep(self, _seconds: float) -> None:
        self._go.clear()
        self._parked.set()
        await self._go.wait()

    async def beat(self, *, timeout: float = 30.0) -> None:
        """
        One renewal round. When the round ends the job (the heartbeat stops it), the stage that is
        waiting in beat() is cancelled by the worker, which is how a test sees the stop. The timeout
        is only a safety net against a hung test.
        """
        await asyncio.wait_for(self._parked.wait(), timeout)
        self._parked.clear()
        self._go.set()
        await asyncio.wait_for(self._parked.wait(), timeout)
        self.beats += 1


class OutageSlots(SlotManager):
    """A SlotManager whose acquire fails closed (503, as outside development) for the first `failures` calls."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.refused = 0

    async def acquire(
        self, limits: "list[SlotLimit] | tuple[SlotLimit, ...]", *, unavailable_message: str = DEFAULT_UNAVAILABLE_MESSAGE
    ) -> SlotLease:
        if self.failures > 0:
            self.failures -= 1
            self.refused += 1
            raise HTTPException(
                status_code=503, detail={"code": "service_unavailable", "message": unavailable_message}
            )
        return await super().acquire(limits, unavailable_message=unavailable_message)


class FlakyTenantSessions:
    """Tenant sessions that fail like a database outage for the first `failures` opens."""

    def __init__(self, inner: TenantSessions, failures: int) -> None:
        self.inner = inner
        self.failures = failures
        self.refused = 0

    def for_job(self, job: Job | TenantRef) -> Any:
        if self.failures > 0:
            self.failures -= 1
            self.refused += 1
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return self.inner.for_job(job)
