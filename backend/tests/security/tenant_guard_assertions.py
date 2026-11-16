"""
tests/security/tenant_guard_assertions.py
─────────────────────────────────────────
Shared assertions for the worker's tenant-context guard (finding 8 of the review of 5b-1/5b-2; see
app/workers/db.py). Each protection is a helper, so the prove-it-fails meta-tests can switch the
protection off and show that the SAME helper then raises.
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers import errors
from app.workers.db import TenantSessions
from app.workers.queue import Job

# label -> SQL. "{b}" is replaced by the other tenant's id.
SWITCH_STATEMENTS = {
    "set_config": "SELECT set_config('app.tenant_id', '{b}', true)",
    "SET LOCAL": "SET LOCAL app.tenant_id = '{b}'",
    "SET": "SET app.tenant_id = '{b}'",
    "RESET": "RESET app.tenant_id",
    "DISCARD": "DISCARD ALL",
    "DO block": "DO $$ BEGIN PERFORM set_config('app.tenant_id', '{b}', true); END $$",
    "comment first": "/* harmless */ SET LOCAL app.tenant_id = '{b}'",
    "second statement": "SELECT 1; SET LOCAL app.tenant_id = '{b}'",
    "reading it": "SELECT current_setting('app.tenant_id', true)",
}


async def assert_stage_statement_rejected(
    sessions: TenantSessions, job: Job, label: str, sql: str, other_tenant: uuid.UUID
) -> None:
    """A stage that runs `sql` on its session gets TenantContextViolation."""
    statement = sql.replace("{b}", str(other_tenant))
    try:
        async with sessions.for_job(job) as db:
            await (await db.connection()).exec_driver_sql(statement)
    except errors.TenantContextViolation:
        return
    except Exception as exc:  # any other failure means the guard did not stop it
        raise AssertionError(f"{label}: expected TenantContextViolation, got {type(exc).__name__}") from None
    raise AssertionError(f"{label}: a stage changed or read the tenant setting and nothing stopped it")


async def assert_tenant_switch_rejected_before_it_takes_effect(
    sessions: TenantSessions, job: Job, other_tenant: uuid.UUID, other_tenant_title: str
) -> None:
    """
    The worst case: a stage switches to tenant B and reads B's rows. The switch must be refused, so B's
    canary is never seen and the transaction ends in TenantContextViolation.
    """
    seen: set[str] = set()
    refused = False
    try:
        async with sessions.for_job(job) as db:
            await db.execute(text("SELECT set_config('app.tenant_id', :b, true)"), {"b": str(other_tenant)})
            seen = set((await db.execute(text("SELECT title FROM app.documents"))).scalars())
    except errors.TenantContextViolation:
        refused = True
    assert other_tenant_title not in seen, "a stage switched to another tenant and read its rows"
    assert refused, "the switch statement ran"


async def assert_own_session_cannot_read_a_payload_tenant(
    sessions: TenantSessions, other_tenant: uuid.UUID, other_tenant_title: str
) -> None:
    """
    A stage that does NOT use the helper: it opens a session straight from the sessionmaker and sets the
    tenant it got from a message payload (tenant B). The engine guard must refuse, so B's canary is never
    seen. Without the guard the stage reads B's rows and this helper raises AssertionError.
    """
    seen: set[str] = set()
    try:
        async with sessions.sessionmaker() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :b, true)"), {"b": str(other_tenant)})
            seen = set((await session.execute(text("SELECT title FROM app.documents"))).scalars())
    except errors.TenantContextViolation:
        return
    assert other_tenant_title not in seen, "a stage that opened its own session read another tenant's rows"


async def assert_commit_refuses_a_changed_tenant(
    sessions: TenantSessions, job: Job, other_tenant: uuid.UUID, raw_db: AsyncSession, version_id: uuid.UUID
) -> None:
    """
    The tenant is changed through a SQL function (the statement text has no set_config, so the statement
    guard cannot see it). The check right before the commit must refuse, and the write of the same
    transaction must be rolled back.
    """
    try:
        async with sessions.for_job(job) as db:
            await db.execute(
                text("UPDATE app.document_versions SET stage = 'indexing' WHERE tenant_id = :t AND id = :v"),
                {"t": job.tenant_id, "v": version_id},
            )
            await db.execute(text("SELECT public.test_switch_tenant(:b)"), {"b": str(other_tenant)})
    except errors.TenantContextViolation:
        pass
    else:
        raise AssertionError("a transaction whose tenant context was changed was committed")
    stage: Any = (
        await raw_db.execute(text("SELECT stage FROM app.document_versions WHERE id = :v"), {"v": version_id})
    ).scalar_one()
    assert stage != "indexing", "the write of a transaction with a changed tenant context was committed"
