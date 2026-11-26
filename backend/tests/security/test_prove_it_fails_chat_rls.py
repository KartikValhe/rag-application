"""
Prove it fails (step 6a-8, test database only): break each defence of the chat tables on purpose and check that the
helpers the real tests use (tests/security/chat_assertions.py) notice.

Every case: the check passes on the migrated schema, the break is applied, the check must now FAIL, the transaction
is rolled back, and the check passes again. GRANT, REVOKE, CREATE/DROP POLICY, DROP INDEX and ALTER TABLE are
transactional in Postgres, so nothing persists. The behaviour checks run as the real roles (SET LOCAL ROLE).

The breaks listed in the 6a-8 brief, and the test that goes red for each:
  owner policy dropped                      -> test_owner_policy_dropped_is_caught
  owner policy made PERMISSIVE              -> test_owner_policy_made_permissive_is_caught
  FORCE RLS off                             -> test_force_rls_off_is_caught
  UPDATE grant widened to all columns       -> test_update_widened_to_all_columns_is_caught
  maint UPDATE policy without pending       -> test_maint_update_policy_without_the_pending_condition_is_caught
  unique assistant-reply index removed      -> test_unique_reply_index_removed_is_caught
  outbox SELECT granted to app_tenant       -> test_queue_select_for_app_tenant_is_caught
plus the C26 case (tenant_isolation without TO on messages) and the 6a-8 app_tenant narrowing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.conftest import _test_engine
from tests.security import chat_assertions as ca

Check = Callable[[AsyncConnection], Awaitable[None]]

PRED = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OWNER = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"


@pytest.fixture
async def owner_conn() -> AsyncIterator[AsyncConnection]:
    async with _test_engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_database()"))).scalar_one().endswith("_test")
        await conn.rollback()
        yield conn
        await conn.rollback()


async def _broken_then_restored(conn: AsyncConnection, break_sql: list[str], check: Check) -> None:
    """The check passes, fails after the break (inside a transaction), and passes again after the rollback."""
    await check(conn)
    for statement in break_sql:
        await conn.execute(text(statement))
    with pytest.raises(AssertionError):
        await check(conn)
    await conn.rollback()
    await check(conn)


def _catalog(table: str) -> Check:
    async def check(conn: AsyncConnection) -> None:
        await ca.assert_chat_rls_forced(conn, table)
        await ca.assert_chat_policies(conn, table)
        await ca.assert_chat_table_grants(conn, table)

    return check


# ── owner policy ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ca.OWNER_TABLES)
async def test_owner_policy_dropped_is_caught(table: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [f"DROP POLICY chat_owner ON app.{table}"], ca.check_owner_isolation)
    await _broken_then_restored(owner_conn, [f"DROP POLICY chat_owner ON app.{table}"], _catalog(table))


@pytest.mark.parametrize("table", ca.OWNER_TABLES)
async def test_owner_policy_made_permissive_is_caught(table: str, owner_conn: AsyncConnection) -> None:
    breaks = [
        f"DROP POLICY chat_owner ON app.{table}",
        f"CREATE POLICY chat_owner ON app.{table} AS PERMISSIVE FOR ALL TO app_tenant USING ({OWNER}) WITH CHECK ({OWNER})",
    ]
    await _broken_then_restored(owner_conn, breaks, ca.check_owner_isolation)
    await _broken_then_restored(owner_conn, breaks, _catalog(table))


async def test_owner_policy_with_a_maintenance_bypass_is_caught(owner_conn: AsyncConnection) -> None:
    """The retired 6-0b design: an OR in the owner policy."""
    breaks = [
        "DROP POLICY chat_owner ON app.messages",
        f"CREATE POLICY chat_owner ON app.messages AS RESTRICTIVE FOR ALL TO app_tenant "
        f"USING ({OWNER} OR current_setting('app.chat_maintenance', true) = 'on') WITH CHECK ({OWNER})",
    ]
    await _broken_then_restored(owner_conn, breaks, _catalog("messages"))


# ── FORCE RLS ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ca.CHAT_TABLES)
@pytest.mark.parametrize("statement", ["NO FORCE ROW LEVEL SECURITY", "DISABLE ROW LEVEL SECURITY"])
async def test_force_rls_off_is_caught(table: str, statement: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [f"ALTER TABLE app.{table} {statement}"], _catalog(table))


async def test_rls_disabled_on_messages_is_caught_by_behaviour(owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, ["ALTER TABLE app.messages DISABLE ROW LEVEL SECURITY"], ca.check_owner_isolation)


# ── grants ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role,check",
    [("app_chat_maint", ca.check_maint_message_update), ("app_tenant", ca.check_tenant_message_writes)],
)
async def test_update_widened_to_all_columns_is_caught(role: str, check: Check, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [f"GRANT UPDATE ON app.messages TO {role}"], check)
    await _broken_then_restored(owner_conn, [f"GRANT UPDATE ON app.messages TO {role}"], _catalog("messages"))


@pytest.mark.parametrize(
    "break_sql,check",
    [
        ("GRANT UPDATE (content) ON app.messages TO app_chat_maint", ca.check_maint_message_update),
        ("GRANT UPDATE (role) ON app.messages TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT UPDATE (user_id) ON app.messages TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT INSERT ON app.messages TO app_chat_maint", ca.check_maint_message_update),
        ("GRANT UPDATE (label) ON app.message_citations TO app_chat_maint", ca.check_maint_message_update),
        ("GRANT DELETE ON app.messages TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT DELETE ON app.message_citations TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT UPDATE (snippet) ON app.message_citations TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT TRUNCATE ON app.conversations TO app_tenant", ca.check_tenant_message_writes),
        ("GRANT SELECT ON app.messages TO app_worker", ca.check_other_roles_get_nothing),
        ("GRANT SELECT ON app.conversations TO app_platform", ca.check_other_roles_get_nothing),
    ],
)
async def test_a_wider_grant_is_caught_by_behaviour(break_sql: str, check: Check, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [break_sql], check)


@pytest.mark.parametrize(
    "break_sql,table",
    [
        ("GRANT UPDATE (content) ON app.messages TO app_chat_maint", "messages"),
        ("GRANT INSERT ON app.llm_usage_events TO app_chat_maint", "llm_usage_events"),
        ("GRANT SELECT ON app.messages TO PUBLIC", "messages"),
        ("REVOKE DELETE ON app.conversations FROM app_chat_maint", "conversations"),
        ("REVOKE UPDATE (outcome) ON app.messages FROM app_chat_maint", "messages"),
    ],
)
async def test_a_changed_grant_is_caught_by_the_catalog(break_sql: str, table: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [break_sql], _catalog(table))


async def test_maint_grant_on_another_table_is_caught(owner_conn: AsyncConnection) -> None:
    for break_sql in ("GRANT SELECT ON app.audit_log TO app_chat_maint", "GRANT SELECT (id) ON app.documents TO app_chat_maint",
                      "GRANT app_tenant TO app_chat_maint"):
        await _broken_then_restored(owner_conn, [break_sql], ca.assert_chat_maint_reaches_only_the_plan)


# ── the maintenance UPDATE policy (C22) and its scope (C26) ──────────────────────────────────────────


async def test_maint_update_policy_without_the_pending_condition_is_caught(owner_conn: AsyncConnection) -> None:
    breaks = [
        "DROP POLICY chat_maint_update ON app.messages",
        f"CREATE POLICY chat_maint_update ON app.messages AS PERMISSIVE FOR UPDATE TO app_chat_maint "
        f"USING ({PRED}) WITH CHECK ({PRED})",
    ]
    await _broken_then_restored(owner_conn, breaks, ca.check_maint_message_update)
    await _broken_then_restored(owner_conn, breaks, _catalog("messages"))


async def test_maint_update_policy_without_the_tenant_condition_is_caught(owner_conn: AsyncConnection) -> None:
    breaks = [
        "DROP POLICY chat_maint_update ON app.messages",
        "CREATE POLICY chat_maint_update ON app.messages AS PERMISSIVE FOR UPDATE TO app_chat_maint "
        "USING (role = 'assistant' AND outcome = 'pending') WITH CHECK (true)",
    ]
    await _broken_then_restored(owner_conn, breaks, ca.check_tenant_isolation)


async def test_tenant_isolation_without_to_clause_is_caught(owner_conn: AsyncConnection) -> None:
    """C26: the plan's first SQL. Permissive policies are ORed, so this alone lets the maintenance role update a
    finished row."""
    breaks = [
        "DROP POLICY tenant_isolation ON app.messages",
        f"CREATE POLICY tenant_isolation ON app.messages USING ({PRED}) WITH CHECK ({PRED})",
    ]
    await _broken_then_restored(owner_conn, breaks, ca.check_maint_message_update)
    await _broken_then_restored(owner_conn, breaks, _catalog("messages"))


async def test_tenant_pending_only_policy_dropped_is_caught(owner_conn: AsyncConnection) -> None:
    breaks = ["DROP POLICY chat_tenant_update_pending ON app.messages"]
    await _broken_then_restored(owner_conn, breaks, ca.check_tenant_message_writes)
    await _broken_then_restored(owner_conn, breaks, _catalog("messages"))
    # Without the policy a final row can be rewritten: the second UPDATE changes 1 row.
    await _broken_then_restored(owner_conn, breaks, lambda c: ca.check_tenant_finishes_pending_answer(c, "answered"))


@pytest.mark.parametrize("outcome", ["answered", "cancelled", "interrupted", "error"])
async def test_tenant_pending_policy_without_with_check_is_caught(outcome: str, owner_conn: AsyncConnection) -> None:
    """With no WITH CHECK, Postgres applies USING (role = 'assistant' AND outcome = 'pending') to the NEW row too, so
    T3 could never write a final outcome."""
    breaks = [
        "DROP POLICY chat_tenant_update_pending ON app.messages",
        "CREATE POLICY chat_tenant_update_pending ON app.messages AS RESTRICTIVE FOR UPDATE TO app_tenant "
        "USING (role = 'assistant' AND outcome = 'pending')",
    ]
    await _broken_then_restored(owner_conn, breaks, lambda c: ca.check_tenant_finishes_pending_answer(c, outcome))
    await _broken_then_restored(owner_conn, breaks, _catalog("messages"))


async def test_an_extra_permissive_policy_is_caught(owner_conn: AsyncConnection) -> None:
    """The catalog check catches it. The behaviour does not change, and that is the point of a RESTRICTIVE owner
    policy: an extra permissive policy ORs only with tenant_isolation, and chat_owner is still ANDed on top."""
    breaks = ["CREATE POLICY open_door ON app.conversations AS PERMISSIVE FOR SELECT TO app_tenant USING (true)"]
    await _broken_then_restored(owner_conn, breaks, _catalog("conversations"))
    for statement in breaks:
        await owner_conn.execute(text(statement))
    await ca.check_owner_isolation(owner_conn)  # still holds with the extra policy in place
    await owner_conn.rollback()


# ── the unique assistant reply ───────────────────────────────────────────────────────────────────────


async def test_unique_reply_index_removed_is_caught(owner_conn: AsyncConnection) -> None:
    breaks = [f"DROP INDEX app.{ca.ASSISTANT_REPLY_INDEX}"]
    await _broken_then_restored(owner_conn, breaks, ca.check_assistant_reply_unique)
    await _broken_then_restored(owner_conn, breaks, ca.assert_assistant_reply_index)


async def test_unique_reply_index_without_the_tenant_is_caught(owner_conn: AsyncConnection) -> None:
    breaks = [
        f"DROP INDEX app.{ca.ASSISTANT_REPLY_INDEX}",
        f"CREATE UNIQUE INDEX {ca.ASSISTANT_REPLY_INDEX} ON app.messages (reply_to_id) WHERE role = 'assistant'",
    ]
    await _broken_then_restored(owner_conn, breaks, ca.check_assistant_reply_unique)


# ── the citation-blank queue (C21) ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "break_sql",
    [
        "GRANT SELECT ON app.chat_citation_blank_queue TO app_tenant",
        "GRANT DELETE ON app.chat_citation_blank_queue TO app_tenant",
        "GRANT UPDATE (document_id) ON app.chat_citation_blank_queue TO app_tenant",
        "GRANT INSERT ON app.chat_citation_blank_queue TO app_chat_maint",
        "REVOKE INSERT ON app.chat_citation_blank_queue FROM app_tenant",
    ],
)
async def test_queue_grant_changes_are_caught(break_sql: str, owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(owner_conn, [break_sql], ca.check_queue_grants)
    await _broken_then_restored(owner_conn, [break_sql], _catalog("chat_citation_blank_queue"))


async def test_queue_select_for_app_tenant_is_caught(owner_conn: AsyncConnection) -> None:
    await _broken_then_restored(
        owner_conn, ["GRANT SELECT ON app.chat_citation_blank_queue TO app_tenant"], ca.check_queue_grants
    )
