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
cannot be passed in by accident (TypeError).

Tenant guard (review of 5b-1/5b-2, finding 8). The worker process holds both credentials, so RLS alone
cannot stop a stage that sets app.tenant_id itself. Two checks catch that:

  - a before_cursor_execute listener on the tenant engine rejects every statement that mentions
    set_config or app.tenant_id, or starts with SET, RESET, DISCARD or DO, unless it is one of this
    module's own statements. Those are tagged through an execution option that carries a per-process
    token (`trusted_statement`), not recognised by their text;
  - the helper checks current_setting('app.tenant_id') against the job's tenant immediately before
    EVERY commit of its session (the session's before_commit event).

The API's `SessionLocal` is a different engine. `install_worker_guards()` makes any use of it in the
worker process fail, and `tenant_session_for` refuses to fall back to it.

HONEST LIMIT: this catches mistakes, not malice. Code in the worker process can import the token, build
the setting name from pieces, call a SQL function that runs set_config for it (and switch back before the
commit), or open its own connection with the credentials. The database cannot stop that, because the
process holds app_tenant's password; only code review and a worker that runs only reviewed stages do.

The engines are built from explicit URLs (no module-level engine on import), so tests pass their own
_test URLs and nothing here reads .env.
"""

import logging
import re
import secrets
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql.elements import TextClause

from app.workers.errors import TenantContextViolation

if TYPE_CHECKING:
    from app.workers.queue import Job

logger = logging.getLogger(__name__)

# -- The tenant guard ---------------------------------------------------------

GUARD_OPTION = "worker_tenant_helper"
_HELPER_TOKEN = secrets.token_hex(16)  # per process; see the honest limit in the module note

_TENANT_SETTING = re.compile(r"\bset_config\b|\bapp\.tenant_id\b", re.IGNORECASE)
# A statement that starts with SET / RESET / DISCARD / DO (after whitespace and comments), at the start
# of the text or after a semicolon. UPDATE ... SET does not match: SET is not first.
_SESSION_COMMAND = re.compile(
    r"^(?:\s|/\*.*?\*/|--[^\n]*(?:\n|$))*(?:set|reset|discard|do)\b", re.IGNORECASE | re.DOTALL
)
# What SQLAlchemy itself runs when a connection is first used
_DIALECT_PROBES = ("select pg_catalog.version", "select current_schema", "show standard_conforming", "select cast('test")


def trusted_statement(sql: str) -> TextClause:
    """A statement of this module's own (set the tenant, check the tenant). Only these pass the guard."""
    return text(sql).execution_options(**{GUARD_OPTION: _HELPER_TOKEN})


def forbidden_tenant_statement(statement: str) -> str | None:
    """A fixed category word when `statement` touches the tenant setting or the session state, else None."""
    if _TENANT_SETTING.search(statement):
        return "tenant_setting"
    for part in statement.split(";"):
        if _SESSION_COMMAND.match(part):
            return "session_command"
    return None


@dataclass
class GuardHandle:
    """Removes the listeners again (tests install and remove guards on shared engines)."""

    _remove: list[Callable[[], None]]

    def remove(self) -> None:
        for remove in self._remove:
            remove()
        self._remove.clear()


def _is_trusted(context: Any) -> bool:
    options = getattr(context, "execution_options", None) or {}
    return bool(options.get(GUARD_OPTION) == _HELPER_TOKEN)


def assert_tenant_unchanged(sync_session: Any, expected: str) -> None:
    """Right before a commit: the session's tenant must still be the job's. Else nothing is committed."""
    current = sync_session.execute(trusted_statement(_READ_TENANT)).scalar()
    if current != expected:
        logger.error("tenant_context_violation reason=tenant_changed_before_commit")
        raise TenantContextViolation("tenant_changed_before_commit")


def install_tenant_guard(engine: AsyncEngine) -> GuardHandle:
    """Reject tenant-setting and session-state statements on `engine` unless they come from this module."""

    def before_execute(_conn: Any, _cursor: Any, statement: str, _params: Any, context: Any, _many: Any) -> None:
        if _is_trusted(context):
            return
        reason = forbidden_tenant_statement(statement)
        if reason is not None:
            logger.error("tenant_context_violation reason=%s", reason)  # never the statement text
            raise TenantContextViolation(reason)

    sync = engine.sync_engine
    event.listen(sync, "before_cursor_execute", before_execute)
    return GuardHandle([lambda: event.remove(sync, "before_cursor_execute", before_execute)])


def install_forbidden_engine_guard(engine: AsyncEngine) -> GuardHandle:
    """Make EVERY statement on `engine` fail: the worker must not use it (the API's SessionLocal engine)."""

    def before_execute(_conn: Any, _cursor: Any, statement: str, _params: Any, context: Any, _many: Any) -> None:
        if _is_trusted(context) or statement.lower().startswith(_DIALECT_PROBES):
            return
        logger.error("tenant_context_violation reason=api_engine_used_by_worker")
        raise TenantContextViolation("api_engine_used_by_worker")

    sync = engine.sync_engine
    event.listen(sync, "before_cursor_execute", before_execute)
    return GuardHandle([lambda: event.remove(sync, "before_cursor_execute", before_execute)])


def install_worker_guards() -> GuardHandle:
    """Called once by the worker process: the API's own engine (SessionLocal) must never be used here."""
    from app.db.engine import engine as api_engine

    return install_forbidden_engine_guard(api_engine)


_SET_TENANT = "SELECT set_config('app.tenant_id', :tid, true)"
_READ_TENANT = "SELECT current_setting('app.tenant_id', true)"


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
        # Never fall back to the API's SessionLocal: it is another engine, with no tenant guard
        raise RuntimeError("tenant_session_for needs the worker's own sessionmaker (WorkerDatabases)")
    expected = str(job.tenant_id)

    def check_tenant_before_commit(sync_session: Any) -> None:
        assert_tenant_unchanged(sync_session, expected)  # looked up by name on every call (tests switch it off)

    async with sessionmaker() as session, session.begin():
        await session.execute(trusted_statement(_SET_TENANT), {"tid": expected})
        # Runs before EVERY commit of this session, also one a stage asks for itself
        event.listen(session.sync_session, "before_commit", check_tenant_before_commit)
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
    tenant_guard: GuardHandle | None = None  # the statement guard on tenant_engine (tests switch it off)

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
            tenant_guard=install_tenant_guard(tenant_engine),
        )

    @classmethod
    def from_settings(cls) -> "WorkerDatabases":
        from app.core.config import settings

        install_worker_guards()  # the API's SessionLocal must not be used in the worker process
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
