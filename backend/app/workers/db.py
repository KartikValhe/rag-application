"""
app/workers/db.py
─────────────────
The worker's two kinds of database access (design A3, "How this interacts with RLS"):

  - the CLAIM connection: engine `worker_engine`, role app_worker (WORKER_DATABASE_URL). It sees job
    metadata (app.ingest_jobs) and tenant ids only and is used by workers/queue.py and nothing else.
    It must NEVER be used for a tenant table: app_worker has no grant on them.
  - TENANT sessions: engine `tenant_engine`, role app_tenant (DATABASE_URL), opened by
    `tenant_session_for(job)`, which sets app.tenant_id from the tenant of the CLAIMED ROW with
    set_config(..., true) (transaction-local) before the first query. Every transaction of a job opens
    its own session through this helper, so the tenant is set again each time.

`tenant_session_for` takes the `Job` object (or a `TenantRef`, which the sweeper builds from the tenant
id list), never a raw UUID or a string: a tenant id that arrived in a message payload or an argument
cannot be passed in by accident (TypeError). Residual risk, as in the design: the worker process holds
both credentials, so a stage that deliberately sets app.tenant_id to another value inside its
transaction is not prevented by the database; tests cover what RLS does contain.

The engines are built from explicit URLs (no module-level engine on import), so tests pass their own
_test URLs and nothing here reads .env.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from app.workers.queue import Job


@dataclass(frozen=True)
class TenantRef:
    """
    A tenant taken from the tenant id list (SELECT id FROM app.tenants as app_worker). The sweeper
    uses it where there is no job; it is built by JobQueue.list_tenants, not from loose ids.
    """

    tenant_id: uuid.UUID


def _is_scoped(value: object) -> bool:
    from app.workers.queue import Job

    return isinstance(value, Job | TenantRef)


@asynccontextmanager
async def tenant_session_for(
    job: "Job | TenantRef", *, sessionmaker: "async_sessionmaker[AsyncSession] | None" = None
) -> AsyncGenerator[AsyncSession, None]:
    """
    Open an app_tenant session in ONE transaction whose app.tenant_id is the tenant of `job`.
    The tenant is read from the Job (a claimed row) or TenantRef, never from an argument.
    """
    if not _is_scoped(job):
        raise TypeError("tenant_session_for needs the claimed Job (or a TenantRef), not a bare tenant id")
    if sessionmaker is None:
        from app.db.engine import SessionLocal

        sessionmaker = SessionLocal
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(job.tenant_id)}
        )
        yield session


@dataclass(frozen=True)
class TenantSessions:
    """Opens a tenant session for a job. The lookup of tenant_session_for is dynamic (tests patch it)."""

    sessionmaker: "async_sessionmaker[AsyncSession]"

    def for_job(self, job: "Job | TenantRef") -> AbstractAsyncContextManager[AsyncSession]:
        return tenant_session_for(job, sessionmaker=self.sessionmaker)


def _engine(url: str, pool_size: int) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        # Session-mode or direct connections only: the sweeper's advisory lock is session-level
        connect_args={"statement_cache_size": 0, "command_timeout": 30},
        query_cache_size=0,
    )


@dataclass
class WorkerDatabases:
    worker_engine: AsyncEngine  # app_worker: the queue
    tenant_engine: AsyncEngine  # app_tenant: tenant sessions
    tenant_sessions: TenantSessions

    @classmethod
    def from_urls(cls, *, tenant_url: str, worker_url: str) -> "WorkerDatabases":
        if not worker_url:
            raise RuntimeError("WORKER_DATABASE_URL is not set (the ingestion worker connects as app_worker)")
        tenant_engine = _engine(tenant_url, 4)
        return cls(
            worker_engine=_engine(worker_url, 4),
            tenant_engine=tenant_engine,
            tenant_sessions=TenantSessions(
                async_sessionmaker(tenant_engine, expire_on_commit=False, autoflush=False)
            ),
        )

    @classmethod
    def from_settings(cls) -> "WorkerDatabases":
        from app.core.config import settings

        return cls.from_urls(tenant_url=settings.DATABASE_URL, worker_url=settings.WORKER_DATABASE_URL)

    async def verify_roles(self) -> None:
        """Fail at startup when a URL points at the wrong role (the worker must be app_worker)."""
        for engine, expected in ((self.worker_engine, "app_worker"), (self.tenant_engine, "app_tenant")):
            async with engine.connect() as conn:
                who = (await conn.execute(text("SELECT current_user"))).scalar_one()
            if who != expected:
                raise RuntimeError(f"a worker database URL connects as {who!r}, expected {expected!r}")

    async def dispose(self) -> None:
        await self.worker_engine.dispose()
        await self.tenant_engine.dispose()
