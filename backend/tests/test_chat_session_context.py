"""
tests/test_chat_session_context.py
──────────────────────────────────
Step 6a-9a: `chat_session_context(tenant_id, user_id)` (plan 2.2, 9.2), the session every chat route uses.

Proven here, as the real app_tenant login (the app's own SessionLocal, NOBYPASSRLS):
  1. It sets app.tenant_id AND app.user_id in ONE statement with set_config(..., true) (transaction-local), as the
     first statement of the transaction, and sets nothing else (no maintenance switch of any kind).
  2. Inside it the caller sees only their own chat rows (owner policy), without any WHERE on user_id.
  3. A plain tenant session (app.user_id unset) sees zero chat rows: chat fails closed.
  4. After it closes, the connection goes back to the pool with neither setting left (one-connection pool).
  5. Ids that are not UUIDs are refused before any SQL runs.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.tenant_db as tenant_db
from app.core.config import settings
from app.core.tenant_db import chat_session_context, tenant_session_context
from app.db.engine import engine as app_engine
from app.db.models.chat import Conversation
from tests.fixtures.academic_world import AcademicWorld


async def _conversation(raw_db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, class_id: uuid.UUID) -> uuid.UUID:
    row = Conversation(tenant_id=tenant_id, user_id=user_id, class_id=class_id)
    raw_db.add(row)
    await raw_db.commit()
    return row.id


@pytest_asyncio.fixture
async def one_connection_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any, None]:
    """Every tenant and chat session of the test runs on ONE pooled app_tenant connection."""
    one = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0, connect_args={"statement_cache_size": 0})
    monkeypatch.setattr(tenant_db, "SessionLocal", async_sessionmaker(bind=one, expire_on_commit=False, autoflush=False))
    try:
        yield one
    finally:
        await one.dispose()


async def test_sets_tenant_and_user_in_one_transaction_local_statement_first(
    academic_world: AcademicWorld, one_connection_pool: Any
) -> None:
    w = academic_world.a
    log: list[tuple[str, str, Any]] = []

    def on_execute(_c: Any, _cur: Any, statement: str, parameters: Any, *_a: Any) -> None:
        log.append(("sql", statement, parameters))

    def on_begin(_c: Any) -> None:
        log.append(("begin", "", None))

    sync = one_connection_pool.sync_engine
    event.listen(sync, "before_cursor_execute", on_execute)
    event.listen(sync, "begin", on_begin)
    try:
        async with chat_session_context(w.id, w.s1.id) as db:
            got = (await db.execute(text(
                "SELECT current_setting('app.tenant_id', true), current_setting('app.user_id', true)"
            ))).one()
    finally:
        event.remove(sync, "before_cursor_execute", on_execute)
        event.remove(sync, "begin", on_begin)

    assert tuple(got) == (str(w.id), str(w.s1.id))
    kinds = [k for k, _s, _p in log]
    assert kinds[0] == "begin" and kinds.count("begin") == 1, log
    first_sql, first_params = log[1][1], log[1][2]
    # Both settings in the first statement, both transaction-local, and nothing else is set
    assert first_sql.count("set_config(") == 2, first_sql
    assert "set_config('app.tenant_id'" in first_sql and "set_config('app.user_id'" in first_sql, first_sql
    assert first_sql.count(", true)") == 2 and ", false)" not in first_sql, first_sql
    assert str(w.id) in str(first_params) and str(w.s1.id) in str(first_params), first_params
    every_sql = " ".join(s for k, s, _p in log if k == "sql")
    assert "chat_maintenance" not in every_sql and every_sql.count("set_config(") == 2, every_sql


async def test_nothing_is_left_on_the_connection_after_it_closes(academic_world: AcademicWorld, one_connection_pool: Any) -> None:
    w = academic_world.a
    async with chat_session_context(w.id, w.s1.id) as db:
        await db.execute(text("SELECT 1"))
    pool = one_connection_pool.pool
    assert pool.checkedout() == 0 and pool.checkedin() == 1
    async with tenant_db.SessionLocal() as fresh:
        left = (await fresh.execute(text(
            "SELECT current_setting('app.tenant_id', true), current_setting('app.user_id', true)"
        ))).one()
    assert all(v in (None, "") for v in left), left


async def test_owner_sees_only_own_conversations_without_a_where(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    mine = await _conversation(raw_db, w.id, w.s1.id, w.classes["10-A"].id)
    theirs = await _conversation(raw_db, w.id, w.s2.id, w.classes["10-B"].id)
    async with chat_session_context(w.id, w.s1.id) as db:
        seen = set((await db.execute(select(Conversation.id))).scalars().all())  # no WHERE at all
    assert mine in seen and theirs not in seen
    async with chat_session_context(w.id, w.s2.id) as db:
        seen2 = set((await db.execute(select(Conversation.id))).scalars().all())
    assert theirs in seen2 and mine not in seen2


async def test_another_tenants_rows_are_invisible_even_for_the_same_user_id(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    b_row = await _conversation(raw_db, b.id, b.s1.id, b.classes["10-A"].id)
    # Tenant a with tenant b's user id: the tenant policy still hides the row
    async with chat_session_context(a.id, b.s1.id) as db:
        seen = set((await db.execute(select(Conversation.id))).scalars().all())
    assert b_row not in seen


async def test_a_plain_tenant_session_sees_zero_chat_rows(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    await _conversation(raw_db, w.id, w.s1.id, w.classes["10-A"].id)
    async with tenant_session_context(str(w.id)) as db:
        count = (await db.execute(select(func.count()).select_from(Conversation))).scalar_one()
    assert count == 0


@pytest.mark.parametrize(
    ("tenant_id", "user_id"),
    [("not-a-uuid", str(uuid.uuid4())), (str(uuid.uuid4()), "not-a-uuid"), (str(uuid.uuid4()), ""), ("", str(uuid.uuid4()))],
)
async def test_ids_that_are_not_uuids_are_refused_before_any_sql(tenant_id: str, user_id: str) -> None:
    statements: list[str] = []

    def on_execute(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    event.listen(app_engine.sync_engine, "before_cursor_execute", on_execute)
    try:
        with pytest.raises(ValueError):
            async with chat_session_context(tenant_id, user_id):
                pass
    finally:
        event.remove(app_engine.sync_engine, "before_cursor_execute", on_execute)
    assert statements == []
