"""
Step 6a-8 (docs/phase6-plan.md 9.2, 9.7 step 2, 11.1, 11.3, C21, C22, C26): row-level security and grants of the chat
tables of migration 0013, checked as the REAL roles.

Most checks run on one superuser connection inside a transaction that is rolled back: the helpers in
tests/security/chat_assertions.py seed their rows there and switch to `app_tenant` or `app_chat_maint` with
`SET LOCAL ROLE` for every probe (the prove-it file breaks a defence and runs the same helpers). One test at the end
uses committed rows and the real `app_tenant` login of the API (a separate connection).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.tenant_db import tenant_session_context
from tests.conftest import _test_engine
from tests.security import chat_assertions as ca


@pytest.fixture
async def chat_conn() -> AsyncIterator[AsyncConnection]:
    """A superuser connection on the TEST database, inside a transaction that is always rolled back."""
    async with _test_engine.connect() as conn:
        trans = await conn.begin()
        try:
            database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert database.endswith("_test"), database
            yield conn
        finally:
            await trans.rollback()


# ── owner and tenant isolation ───────────────────────────────────────────────────────────────────────


async def test_user_a_cannot_see_update_or_delete_user_bs_chat_rows(chat_conn: AsyncConnection) -> None:
    await ca.check_owner_isolation(chat_conn)


async def test_tenant_x_cannot_see_or_touch_tenant_ys_chat_rows(chat_conn: AsyncConnection) -> None:
    await ca.check_tenant_isolation(chat_conn)


# ── the unique assistant reply ───────────────────────────────────────────────────────────────────────


async def test_one_assistant_row_per_user_message_per_tenant(chat_conn: AsyncConnection) -> None:
    await ca.check_assistant_reply_unique(chat_conn)


# ── the maintenance role (C22) ───────────────────────────────────────────────────────────────────────


async def test_maint_marks_only_pending_assistant_rows_and_changes_no_other_column(chat_conn: AsyncConnection) -> None:
    await ca.check_maint_message_update(chat_conn)


async def test_maint_role_is_a_plain_role_with_no_memberships(chat_conn: AsyncConnection) -> None:
    await ca.assert_role_is_plain(chat_conn)
    await ca.assert_chat_maint_reaches_only_the_plan(chat_conn)


# ── app_tenant narrowing (6a-8) ──────────────────────────────────────────────────────────────────────


async def test_app_tenant_updates_only_its_own_pending_row_and_deletes_no_message(chat_conn: AsyncConnection) -> None:
    await ca.check_tenant_message_writes(chat_conn)


@pytest.mark.parametrize("outcome", ca.FINAL_OUTCOMES)
async def test_app_tenant_finishes_its_pending_answer_with_every_final_outcome_once(
    outcome: str, chat_conn: AsyncConnection
) -> None:
    """T3 / the cancel step: one UPDATE that sets the outcome moves the pending row to any final outcome; the same
    UPDATE on an already-final row changes 0 rows."""
    await ca.check_tenant_finishes_pending_answer(chat_conn, outcome)


def test_final_outcomes_are_every_planned_outcome_but_pending() -> None:
    from app.db.models.chat import MESSAGE_OUTCOMES

    assert set(ca.FINAL_OUTCOMES) == set(MESSAGE_OUTCOMES) - {"pending"}
    assert len(ca.FINAL_OUTCOMES) == len(set(ca.FINAL_OUTCOMES)) == 14


# ── the citation-blank queue (C21) ───────────────────────────────────────────────────────────────────


async def test_blank_queue_is_insert_only_for_tenant_and_select_delete_for_maint(chat_conn: AsyncConnection) -> None:
    await ca.check_queue_grants(chat_conn)


# ── other roles ──────────────────────────────────────────────────────────────────────────────────────


async def test_platform_and_worker_roles_get_nothing_on_chat_tables(chat_conn: AsyncConnection) -> None:
    await ca.check_other_roles_get_nothing(chat_conn)


# ── the real app_tenant login, committed rows ────────────────────────────────────────────────────────


async def test_real_app_tenant_login_sees_only_its_own_user_and_fails_closed(raw_db: AsyncSession) -> None:
    """Separate connection as the API's app_tenant login (tenant_session_context), rows committed by the superuser."""
    conn = await raw_db.connection()
    w = await ca.seed_chat_world(conn)
    await raw_db.commit()

    from app.db.models.chat import Conversation, Message

    async with tenant_session_context(str(w.x)) as session:
        role = (await session.execute(text("SELECT current_user"))).scalar_one()
        assert role == "app_tenant"
        assert (await session.execute(select(Message))).scalars().all() == [], "no app.user_id: chat rows are visible"
        await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(w.a.user)})
        conversations = (await session.execute(select(Conversation))).scalars().all()
        messages = (await session.execute(select(Message))).scalars().all()
    assert [c.id for c in conversations] == [w.a.conversation]
    assert {m.id for m in messages} == {w.a.question, w.a.answer, w.a.pending_question, w.a.pending}

    async with tenant_session_context(str(w.y)) as session:
        await session.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(w.a.user)})
        assert (await session.execute(select(Message))).scalars().all() == [], "A's user id shows rows in tenant Y"
