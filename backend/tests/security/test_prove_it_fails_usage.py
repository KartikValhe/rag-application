"""
Prove it fails (step 6a-5, test database only): break each defence of tenant_settings and llm_usage_events on purpose
and check that the assertion helpers the real tests use notice.

  * RLS alone holds: the application's tenant filter is dropped, and tenant A still reads only its own rows. The same
    read through the superuser (RLS bypassed) shows tenant B's rows, and the helper catches it;
  * a wider grant (UPDATE or DELETE on llm_usage_events, DELETE on tenant_settings, anything for app_platform) and a
    missing FORCE RLS are caught by the schema helpers.

Every break is made inside a transaction of the superuser test connection and rolled back: nothing persists. GRANT,
REVOKE and ALTER TABLE are transactional in Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.tenant_db import tenant_session_context
from app.services import tenant_settings as ts
from app.services.llm_usage import record_usage
from tests.conftest import _test_engine, _TestSession
from tests.fixtures.academic_world import AcademicWorld
from tests.security.usage_assertions import (
    assert_policies,
    assert_rls_forced,
    assert_rows_belong_to,
    assert_table_grants,
)


async def _settings_rows(raw_db: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> None:
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, chat_daily_token_budget) VALUES (:a, 1111), (:b, 2222)"),
        {"a": a, "b": b},
    )
    await raw_db.commit()


async def _usage_row(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session_context(str(tenant_id)) as session:
        return await record_usage(
            session,
            tenant_id=tenant_id,
            user_id=None,
            message_id=None,
            purpose="answer",
            provider="groq",
            model_key="groq:openai/gpt-oss-120b",
            prompt_tokens=10,
            completion_tokens=2,
            estimated=False,
            latency_ms=5,
            outcome="ok",
            attempt=1,
        )


async def _privileged_session_for(tenant_id: uuid.UUID) -> Any:
    """The superuser session (RLS bypassed) with app.tenant_id set, as a broken deployment would have it."""
    session = _TestSession()
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    return session


# ── RLS alone holds ──────────────────────────────────────────────────────────────────────────────────


async def test_rls_alone_holds_for_tenant_settings_and_bypassing_it_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = academic_world.a.id, academic_world.b.id
    await _settings_rows(raw_db, a, b)
    monkeypatch.setattr(ts, "TENANT_FILTER", "TRUE")  # the application's own tenant filter is gone

    async with tenant_session_context(str(a)) as session:  # app_tenant, RLS on
        assert await ts.read_token_budget_override(session, a) == 1111
        assert await ts.read_token_budget_override(session, b) == 1111, "the filter is off: only RLS decides now"

    privileged = await _privileged_session_for(a)  # RLS bypassed
    try:
        with pytest.raises(MultipleResultsFound):
            await ts.read_token_budget_override(privileged, a)  # tenant B's row is visible: caught
    finally:
        await privileged.rollback()
        await privileged.close()


async def test_rls_alone_holds_for_llm_usage_events_and_bypassing_it_is_caught(academic_world: AcademicWorld) -> None:
    a, b = academic_world.a.id, academic_world.b.id
    mine, theirs = await _usage_row(a), await _usage_row(b)
    unfiltered = text("SELECT tenant_id, id FROM app.llm_usage_events")  # no tenant filter at all

    async with tenant_session_context(str(a)) as session:
        rows = (await session.execute(unfiltered)).all()
    assert mine in {r[1] for r in rows}
    assert_rows_belong_to((r[0] for r in rows), a, "llm_usage_events, RLS alone")

    privileged = await _privileged_session_for(a)
    try:
        rows = (await privileged.execute(unfiltered)).all()
        assert theirs in {r[1] for r in rows}
        with pytest.raises(AssertionError, match="rows of other tenants are visible"):
            assert_rows_belong_to((r[0] for r in rows), a, "llm_usage_events, RLS bypassed")
    finally:
        await privileged.rollback()
        await privileged.close()


# ── wider grants and a missing FORCE are caught ──────────────────────────────────────────────────────


@pytest.fixture
async def owner_conn() -> AsyncIterator[AsyncConnection]:
    async with _test_engine.connect() as conn:
        yield conn
        await conn.rollback()


async def _broken_then_restored(
    conn: AsyncConnection, break_sql: str, check: Callable[[AsyncConnection], Awaitable[None]]
) -> None:
    """The check passes, fails after the break (inside a transaction), and passes again after the rollback."""
    await check(conn)
    await conn.execute(text(break_sql))
    with pytest.raises(AssertionError):
        await check(conn)
    await conn.rollback()
    await check(conn)


@pytest.mark.parametrize(
    "break_sql",
    [
        "GRANT UPDATE ON app.llm_usage_events TO app_tenant",
        "GRANT DELETE ON app.llm_usage_events TO app_tenant",
        "GRANT UPDATE (latency_ms) ON app.llm_usage_events TO app_tenant",
        "GRANT SELECT ON app.llm_usage_events TO app_platform",
        "GRANT INSERT ON app.llm_usage_events TO app_worker",
        "GRANT SELECT ON app.llm_usage_events TO PUBLIC",
    ],
)
async def test_a_wider_usage_grant_is_caught(break_sql: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, break_sql, lambda c: assert_table_grants(c, "llm_usage_events"))


@pytest.mark.parametrize(
    "break_sql",
    [
        "GRANT DELETE ON app.tenant_settings TO app_tenant",
        "GRANT INSERT ON app.tenant_settings TO app_platform",
        "REVOKE UPDATE ON app.tenant_settings FROM app_tenant",
    ],
)
async def test_a_changed_settings_grant_is_caught(break_sql: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, break_sql, lambda c: assert_table_grants(c, "tenant_settings"))


@pytest.mark.parametrize("table", ["tenant_settings", "llm_usage_events"])
@pytest.mark.parametrize("break_sql", ["ALTER TABLE app.{t} NO FORCE ROW LEVEL SECURITY", "ALTER TABLE app.{t} DISABLE ROW LEVEL SECURITY"])
async def test_missing_force_rls_is_caught(table: str, break_sql: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, break_sql.format(t=table), lambda c: assert_rls_forced(c, table))


@pytest.mark.parametrize("table", ["tenant_settings", "llm_usage_events"])
async def test_an_extra_permissive_policy_is_caught(table: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(
        owner_conn,
        f"CREATE POLICY open_door ON app.{table} AS PERMISSIVE FOR SELECT TO app_tenant USING (true)",
        lambda c: assert_policies(c, table),
    )
