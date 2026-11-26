"""
Assertions on the chat tables of migration 0013 (step 6a-8, docs/phase6-plan.md 9.1, 9.2, 9.7 step 2, C21, C22, C26).

Shared by tests/test_chat_isolation.py, tests/test_phase6_schema.py, tests/test_isolation.py and
tests/security/test_prove_it_fails_chat_rls.py, so a prove-it break is checked against THESE helpers.

Every helper takes an AsyncConnection of the superuser test engine with a transaction open. The behaviour helpers
seed their own rows inside that transaction (nothing is committed) and run each probe as a REAL role
(`SET LOCAL ROLE app_tenant` / `app_chat_maint` / ...) inside a savepoint that is always rolled back, so a probe
never changes what the next probe sees. RLS and privileges are checked against the current role, so a probe is
exactly what that role would get on its own login.

The expected grants and policies below are written out as literals on purpose: they do not import the migration's
or the model's column lists, so widening those lists turns these checks red.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

# ── what the plan says (9.2, C21, C22, C26, 6a-8 narrowing) ──────────────────────────────────────────────

CHAT_TABLES = ("conversations", "messages", "message_citations", "message_feedback", "chat_citation_blank_queue")
OWNER_TABLES = ("conversations", "messages", "message_citations", "message_feedback")
MAINT_TABLES = (*CHAT_TABLES, "llm_usage_events")
APP_ROLES = ("app_tenant", "app_platform", "app_worker", "app_chat_maint")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# app_tenant may UPDATE these messages columns only: what T3 and the shielded cancel step write (plan 2.1, 8.6).
TENANT_MESSAGE_UPDATE_COLUMNS = (
    "content",
    "route",
    "outcome",
    "partial",
    "intent",
    "rule_ids",
    "router_confidence",
    "language",
    "retrieval_query",
    "provider",
    "model_key",
    "prompt_version",
    "prompt_tokens",
    "completion_tokens",
    "tokens_estimated",
    "latency_ms",
    "retrieval",
    "invalid_citation_count",
    "flagged_sources_used",
    "needs_review",
    "completed_at",
)
# Columns nobody but the owner may ever change on a message.
MESSAGE_FROZEN_COLUMNS = (
    "tenant_id",
    "id",
    "conversation_id",
    "user_id",
    "role",
    "reply_to_id",
    "client_request_id",
    "created_at",
)
MAINT_MESSAGE_UPDATE_COLUMNS = ("outcome", "partial", "completed_at")


def _tp(*pairs: tuple[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(pairs)


_MAINT_SD = (("app_chat_maint", "SELECT"), ("app_chat_maint", "DELETE"))
# Table-level grants (relacl): (grantee, privilege). Nobody has TRUNCATE, REFERENCES or TRIGGER.
EXPECTED_TABLE_GRANTS: dict[str, frozenset[tuple[str, str]]] = {
    "conversations": _tp(("app_tenant", "SELECT"), ("app_tenant", "INSERT"), ("app_tenant", "DELETE"), *_MAINT_SD),
    "messages": _tp(("app_tenant", "SELECT"), ("app_tenant", "INSERT"), *_MAINT_SD),
    "message_citations": _tp(("app_tenant", "SELECT"), ("app_tenant", "INSERT"), *_MAINT_SD),
    "message_feedback": _tp(("app_tenant", "SELECT"), ("app_tenant", "INSERT"), ("app_tenant", "DELETE"), *_MAINT_SD),
    "chat_citation_blank_queue": _tp(("app_tenant", "INSERT"), *_MAINT_SD),
    "llm_usage_events": _tp(("app_tenant", "SELECT"), ("app_tenant", "INSERT"), *_MAINT_SD),
}


def _cols(role: str, privilege: str, columns: Sequence[str]) -> set[tuple[str, str, str]]:
    return {(c, role, privilege) for c in columns}


# Column-level grants (attacl): (column, grantee, privilege).
EXPECTED_COLUMN_GRANTS: dict[str, frozenset[tuple[str, str, str]]] = {
    "conversations": frozenset(_cols("app_tenant", "UPDATE", ("title", "last_message_at", "message_count"))),
    "messages": frozenset(
        _cols("app_tenant", "UPDATE", TENANT_MESSAGE_UPDATE_COLUMNS)
        | _cols("app_chat_maint", "UPDATE", MAINT_MESSAGE_UPDATE_COLUMNS)
    ),
    "message_citations": frozenset(_cols("app_chat_maint", "UPDATE", ("snippet", "heading_path"))),
    "message_feedback": frozenset(_cols("app_tenant", "UPDATE", ("rating", "comment", "updated_at"))),
    "chat_citation_blank_queue": frozenset(),
    "llm_usage_events": frozenset(),
}

# Policy texts as pg_policies prints them.
TENANT_PRED = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
OWNER_PRED = "(user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)"
ASSISTANT = "((role)::text = 'assistant'::text)"
PENDING = "((outcome)::text = 'pending'::text)"
MAINT_UPDATE_USING = f"({TENANT_PRED} AND {ASSISTANT} AND {PENDING})"
TENANT_PENDING_USING = f"({ASSISTANT} AND {PENDING})"

# (name, command, permissive, roles, USING, WITH CHECK)
PolicyRow = tuple[str, str, str, tuple[str, ...], str | None, str | None]

_ISOLATION_TO_TENANT: PolicyRow = ("tenant_isolation", "ALL", "PERMISSIVE", ("app_tenant",), TENANT_PRED, TENANT_PRED)
_OWNER: PolicyRow = ("chat_owner", "ALL", "RESTRICTIVE", ("app_tenant",), OWNER_PRED, OWNER_PRED)
_MAINT_ALL: PolicyRow = ("chat_maint", "ALL", "PERMISSIVE", ("app_chat_maint",), TENANT_PRED, TENANT_PRED)

EXPECTED_CHAT_POLICIES: dict[str, frozenset[PolicyRow]] = {
    "conversations": frozenset({_ISOLATION_TO_TENANT, _OWNER, _MAINT_ALL}),
    "messages": frozenset(
        {
            _ISOLATION_TO_TENANT,
            _OWNER,
            ("chat_tenant_update_pending", "UPDATE", "RESTRICTIVE", ("app_tenant",), TENANT_PENDING_USING, ASSISTANT),
            ("chat_maint_select", "SELECT", "PERMISSIVE", ("app_chat_maint",), TENANT_PRED, None),
            ("chat_maint_delete", "DELETE", "PERMISSIVE", ("app_chat_maint",), TENANT_PRED, None),
            ("chat_maint_update", "UPDATE", "PERMISSIVE", ("app_chat_maint",), MAINT_UPDATE_USING, TENANT_PRED),
        }
    ),
    "message_citations": frozenset({_ISOLATION_TO_TENANT, _OWNER, _MAINT_ALL}),
    "message_feedback": frozenset({_ISOLATION_TO_TENANT, _OWNER, _MAINT_ALL}),
    "chat_citation_blank_queue": frozenset({_ISOLATION_TO_TENANT, _MAINT_ALL}),
    # 0012's tenant_isolation stays without a TO clause; 0013 adds the maintenance policy (same predicate).
    "llm_usage_events": frozenset(
        {("tenant_isolation", "ALL", "PERMISSIVE", ("public",), TENANT_PRED, TENANT_PRED), _MAINT_ALL}
    ),
}

ASSISTANT_REPLY_INDEX = "ux_messages_assistant_reply"


# ── running a statement as a real role ───────────────────────────────────────────────────────────────


@dataclass
class Probe:
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    rowcount: int = -1
    error: str | None = None

    @property
    def denied(self) -> bool:
        return self.error is not None and "permission denied" in self.error

    @property
    def rls_refused(self) -> bool:
        return self.error is not None and "row-level security" in self.error

    def ok(self, what: str) -> Probe:
        assert self.error is None, f"{what}: expected to work, got: {self.error}"
        return self


Statement = str | tuple[str, dict[str, Any]]


async def run_as(
    conn: AsyncConnection,
    role: str,
    statements: Statement | Sequence[Statement],
    *,
    tenant: uuid.UUID | None = None,
    user: uuid.UUID | None = None,
) -> Probe:
    """Run the statement(s) as `role` with the given GUCs, inside a savepoint that is always rolled back.
    Returns the rows and row count of the last statement, or the first database error."""
    assert role in APP_ROLES, role
    items: list[Statement] = [cast(Statement, statements)] if isinstance(statements, str | tuple) else list(statements)
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text(f"SET LOCAL ROLE {role}"))
        if tenant is not None:
            await conn.execute(text("SELECT set_config('app.tenant_id', :v, true)"), {"v": str(tenant)})
        if user is not None:
            await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": str(user)})
        probe = Probe()
        for item in items:
            params: dict[str, Any] = {}
            if isinstance(item, str):
                sql = item
            else:
                sql, params = item
            result = await conn.execute(text(sql), params)
            probe = Probe(rows=[tuple(r) for r in result.fetchall()] if result.returns_rows else [], rowcount=result.rowcount)
        return probe
    except DBAPIError as exc:
        return Probe(error=str(exc.orig))
    finally:
        await savepoint.rollback()


def _denied(probe: Probe, what: str) -> None:
    assert probe.denied, f"{what}: expected 'permission denied', got rows={probe.rows} rowcount={probe.rowcount} error={probe.error}"


def _rls_refused(probe: Probe, what: str) -> None:
    assert probe.rls_refused, f"{what}: expected a row-level security refusal, got rowcount={probe.rowcount} error={probe.error}"


def _count(probe: Probe, expected: int, what: str) -> None:
    probe.ok(what)
    assert probe.rowcount == expected, f"{what}: affected {probe.rowcount} row(s), expected {expected}"


# ── a small world: tenants X and Y, users A and B in X, user C in Y ──────────────────────────────────────


@dataclass(frozen=True)
class ChatUser:
    tenant: uuid.UUID
    user: uuid.UUID
    conversation: uuid.UUID
    question: uuid.UUID  # user message, answered
    answer: uuid.UUID  # assistant message, outcome 'answered' (finished)
    pending_question: uuid.UUID  # user message whose answer is still streaming
    pending: uuid.UUID  # assistant message, outcome 'pending'
    citation: uuid.UUID  # on the answer
    feedback: uuid.UUID  # on the answer


@dataclass(frozen=True)
class ChatWorld:
    x: uuid.UUID
    y: uuid.UUID
    class_x: uuid.UUID
    class_y: uuid.UUID
    a: ChatUser
    b: ChatUser
    c: ChatUser
    # A user message of C in tenant Y with the SAME id as A's question in tenant X (PK is (tenant_id, id)), and no
    # answer yet: the unique reply index is per tenant.
    c_shared_question: uuid.UUID
    queue_x: uuid.UUID
    queue_y: uuid.UUID
    document: uuid.UUID


async def _exec(conn: AsyncConnection, sql: str, **params: Any) -> None:
    await conn.execute(text(sql), params)


async def _seed_user(conn: AsyncConnection, tenant: uuid.UUID, klass: uuid.UUID, label: str, *, question_id: uuid.UUID | None = None) -> ChatUser:
    u = ChatUser(
        tenant=tenant,
        user=uuid.uuid4(),
        conversation=uuid.uuid4(),
        question=question_id or uuid.uuid4(),
        answer=uuid.uuid4(),
        pending_question=uuid.uuid4(),
        pending=uuid.uuid4(),
        citation=uuid.uuid4(),
        feedback=uuid.uuid4(),
    )
    await _exec(
        conn,
        "INSERT INTO app.users (tenant_id, id, email, full_name, password_hash, status) "
        "VALUES (:t, :u, :e, :n, 'x', 'active')",
        t=tenant, u=u.user, e=f"chat-{label}-{uuid.uuid4().hex}@example.com", n=f"Chat {label}",
    )
    await _exec(
        conn,
        "INSERT INTO app.conversations (tenant_id, id, user_id, class_id, title) VALUES (:t, :c, :u, :k, :title)",
        t=tenant, c=u.conversation, u=u.user, k=klass, title=f"{label} title",
    )
    insert_message = (
        "INSERT INTO app.messages (tenant_id, id, conversation_id, user_id, role, content, reply_to_id, "
        "client_request_id, outcome) VALUES (:t, :id, :c, :u, :role, :content, :reply, :crid, :outcome)"
    )
    for mid, role, content, reply, crid, outcome in (
        (u.question, "user", f"question of {label}", None, uuid.uuid4(), None),
        (u.answer, "assistant", f"answer of {label}", u.question, None, "answered"),
        (u.pending_question, "user", f"second question of {label}", None, uuid.uuid4(), None),
        (u.pending, "assistant", "", u.pending_question, None, "pending"),
    ):
        await _exec(conn, insert_message, t=tenant, id=mid, c=u.conversation, u=u.user, role=role, content=content,
                    reply=reply, crid=crid, outcome=outcome)
    await _exec(
        conn,
        "INSERT INTO app.message_citations (tenant_id, id, message_id, user_id, label, ordinal, chunk_id, document_id, "
        "version_id, document_title, version_no, file_kind, location_kind, page_start, page_end, heading_path, snippet) "
        "VALUES (:t, :id, :m, :u, 'S1', 1, gen_random_uuid(), :d, gen_random_uuid(), 'Physics', 1, 'pdf', 'page', 3, 3, "
        "'Unit 1', :snippet)",
        t=tenant, id=u.citation, m=u.answer, u=u.user, d=uuid.UUID(int=7), snippet=f"snippet of {label}",
    )
    await _exec(
        conn,
        "INSERT INTO app.message_feedback (tenant_id, id, message_id, user_id, rating, comment) "
        "VALUES (:t, :id, :m, :u, 1, :comment)",
        t=tenant, id=u.feedback, m=u.answer, u=u.user, comment=f"comment of {label}",
    )
    return u


async def seed_chat_world(conn: AsyncConnection) -> ChatWorld:
    """Rows for two tenants, written by the superuser in the caller's transaction."""
    x, y = uuid.uuid4(), uuid.uuid4()
    class_x, class_y = uuid.uuid4(), uuid.uuid4()
    for tenant, klass, label in ((x, class_x, "x"), (y, class_y, "y")):
        await _exec(conn, "INSERT INTO app.tenants (id, slug, name, kind) VALUES (:t, :s, :n, 'school')",
                    t=tenant, s=f"chat-{label}-{uuid.uuid4().hex}", n=f"Chat {label}")
        await _exec(conn, "INSERT INTO app.classes (tenant_id, id, name) VALUES (:t, :k, :n)",
                    t=tenant, k=klass, n=f"chat-{label}-{uuid.uuid4().hex[:8]}")
    a = await _seed_user(conn, x, class_x, "a")
    b = await _seed_user(conn, x, class_x, "b")
    c = await _seed_user(conn, y, class_y, "c")
    c_shared_question = a.question
    await _exec(
        conn,
        "INSERT INTO app.messages (tenant_id, id, conversation_id, user_id, role, content, client_request_id) "
        "VALUES (:t, :id, :c, :u, 'user', 'question with a shared id', gen_random_uuid())",
        t=y, id=c_shared_question, c=c.conversation, u=c.user,
    )
    document = uuid.UUID(int=7)
    queue_x, queue_y = uuid.uuid4(), uuid.uuid4()
    for tenant, qid in ((x, queue_x), (y, queue_y)):
        await _exec(conn, "INSERT INTO app.chat_citation_blank_queue (tenant_id, id, document_id) VALUES (:t, :id, :d)",
                    t=tenant, id=qid, d=document)
        await _exec(
            conn,
            "INSERT INTO app.llm_usage_events (tenant_id, purpose, provider, model_key, prompt_tokens, completion_tokens, "
            "estimated, latency_ms, outcome, attempt) VALUES (:t, 'answer', 'groq', 'groq:openai/gpt-oss-120b', 1, 1, "
            "false, 1, 'ok', 1)",
            t=tenant,
        )
    return ChatWorld(x=x, y=y, class_x=class_x, class_y=class_y, a=a, b=b, c=c, c_shared_question=c_shared_question,
                     queue_x=queue_x, queue_y=queue_y, document=document)


def _owners(probe: Probe) -> set[tuple[Any, ...]]:
    return {tuple(r) for r in probe.rows}


# ── behaviour checks ─────────────────────────────────────────────────────────────────────────────────


async def check_owner_isolation(conn: AsyncConnection) -> None:
    """Plan 9.2 / 11.3: user A sees, updates and deletes only A's chat rows, even with no WHERE user_id at all."""
    w = await seed_chat_world(conn)
    a, b = w.a, w.b
    for table in OWNER_TABLES:
        mine = (await run_as(conn, "app_tenant", f"SELECT tenant_id, user_id FROM app.{table}", tenant=w.x, user=a.user)).ok(table)
        assert mine.rows, f"{table}: user A sees none of A's own rows (the probe proves nothing)"
        assert _owners(mine) == {(w.x, a.user)}, f"{table}: user A sees rows of another user: {_owners(mine)}"
        theirs = (await run_as(conn, "app_tenant", f"SELECT tenant_id, user_id FROM app.{table}", tenant=w.x, user=b.user)).ok(table)
        assert _owners(theirs) == {(w.x, b.user)}, f"{table}: user B sees {_owners(theirs)}"
        # Fail closed: the tenant alone (no app.user_id) shows nothing.
        none = (await run_as(conn, "app_tenant", f"SELECT 1 FROM app.{table}", tenant=w.x)).ok(table)
        assert none.rows == [], f"{table}: {len(none.rows)} row(s) visible with app.tenant_id only"

    as_a: dict[str, Any] = {"tenant": w.x, "user": a.user}
    writes = (
        ("conversations UPDATE", "UPDATE app.conversations SET title = 'hacked' WHERE id = :id", a.conversation, b.conversation),
        ("messages UPDATE", "UPDATE app.messages SET content = 'hacked' WHERE id = :id", a.pending, b.pending),
        ("message_feedback UPDATE", "UPDATE app.message_feedback SET comment = 'hacked' WHERE id = :id", a.feedback, b.feedback),
        ("conversations DELETE", "DELETE FROM app.conversations WHERE id = :id", a.conversation, b.conversation),
        ("message_feedback DELETE", "DELETE FROM app.message_feedback WHERE id = :id", a.feedback, b.feedback),
    )
    for what, sql, own_id, other_id in writes:
        _count(await run_as(conn, "app_tenant", (sql, {"id": own_id}), **as_a), 1, f"{what} of A's own row (control)")
        _count(await run_as(conn, "app_tenant", (sql, {"id": other_id}), **as_a), 0, f"{what} of B's row by A")
        _count(await run_as(conn, "app_tenant", sql.split(" WHERE ")[0], tenant=w.x, user=uuid.uuid4()), 0, f"{what} by a stranger")
    # A cannot write a row in B's name.
    _rls_refused(
        await run_as(conn, "app_tenant", ("INSERT INTO app.conversations (tenant_id, user_id, class_id) VALUES (:t, :u, :k)",
                                          {"t": w.x, "u": b.user, "k": w.class_x}), **as_a),
        "A inserts a conversation for B",
    )
    _rls_refused(
        await run_as(conn, "app_tenant", ("INSERT INTO app.message_feedback (tenant_id, message_id, user_id, rating) "
                                          "VALUES (:t, :m, :u, -1)", {"t": w.x, "m": b.answer, "u": b.user}), **as_a),
        "A rates B's answer in B's name",
    )


async def check_tenant_isolation(conn: AsyncConnection) -> None:
    """Tenant X never sees or touches tenant Y, for app_tenant and for app_chat_maint."""
    w = await seed_chat_world(conn)
    for table in OWNER_TABLES:
        # Y's user id with X's tenant id: nothing (the tenant condition still holds).
        cross = (await run_as(conn, "app_tenant", f"SELECT 1 FROM app.{table}", tenant=w.x, user=w.c.user)).ok(table)
        assert cross.rows == [], f"{table}: tenant X context shows tenant Y rows"
        own = (await run_as(conn, "app_tenant", f"SELECT tenant_id, user_id FROM app.{table}", tenant=w.y, user=w.c.user)).ok(table)
        assert own.rows and _owners(own) == {(w.y, w.c.user)}, f"{table}: tenant Y sees {_owners(own)}"
    _rls_refused(
        await run_as(conn, "app_tenant", ("INSERT INTO app.conversations (tenant_id, user_id, class_id) VALUES (:t, :u, :k)",
                                          {"t": w.y, "u": w.a.user, "k": w.class_y}), tenant=w.x, user=w.a.user),
        "a conversation written into another tenant",
    )
    for table in MAINT_TABLES:
        seen = (await run_as(conn, "app_chat_maint", f"SELECT DISTINCT tenant_id FROM app.{table}", tenant=w.x)).ok(table)
        assert seen.rows == [(w.x,)], f"{table}: the maintenance role in tenant X sees tenants {seen.rows}"
        nothing = (await run_as(conn, "app_chat_maint", f"SELECT 1 FROM app.{table}")).ok(table)
        assert nothing.rows == [], f"{table}: the maintenance role sees rows with no tenant set"
    _count(await run_as(conn, "app_chat_maint", ("DELETE FROM app.conversations WHERE id = :id", {"id": w.c.conversation}), tenant=w.x),
           0, "maintenance DELETE of tenant Y's conversation in tenant X")
    _count(await run_as(conn, "app_chat_maint", ("UPDATE app.messages SET outcome = 'interrupted' WHERE id = :id", {"id": w.c.pending}),
                        tenant=w.x), 0, "maintenance UPDATE of tenant Y's pending row in tenant X")
    # Blind statements (no WHERE, nothing read): only the UPDATE/DELETE policies themselves decide here, the SELECT
    # policy is not consulted. Exactly tenant X's rows: A's and B's pending answers, A's and B's conversations.
    _count(await run_as(conn, "app_chat_maint", "UPDATE app.messages SET outcome = 'interrupted', partial = true", tenant=w.x),
           2, "blind maintenance UPDATE in tenant X")
    _count(await run_as(conn, "app_chat_maint", "DELETE FROM app.conversations", tenant=w.x), 2, "blind maintenance DELETE in tenant X")
    _count(await run_as(conn, "app_tenant", "UPDATE app.messages SET content = 'x'", tenant=w.x, user=w.a.user),
           1, "blind app_tenant UPDATE (only A's pending answer)")


async def check_assistant_reply_unique(conn: AsyncConnection) -> None:
    """Plan 9.1 (review 6-0b): at most one assistant row per user message, per tenant."""
    w = await seed_chat_world(conn)
    insert = (
        "INSERT INTO app.messages (tenant_id, conversation_id, user_id, role, content, reply_to_id, outcome) "
        "VALUES (:t, :c, :u, 'assistant', '', :r, 'pending')"
    )
    second = await run_as(conn, "app_tenant", (insert, {"t": w.x, "c": w.a.conversation, "u": w.a.user, "r": w.a.question}),
                          tenant=w.x, user=w.a.user)
    assert second.error is not None and ASSISTANT_REPLY_INDEX in second.error, (
        f"a second assistant row for one user message was not refused by {ASSISTANT_REPLY_INDEX}: "
        f"rowcount={second.rowcount} error={second.error}"
    )
    # The same reply_to_id value in another tenant is a different message: allowed once, refused the second time.
    in_y = (insert, {"t": w.y, "c": w.c.conversation, "u": w.c.user, "r": w.c_shared_question})
    _count(await run_as(conn, "app_tenant", in_y, tenant=w.y, user=w.c.user), 1, "first answer in tenant Y to the shared id")
    twice = await run_as(conn, "app_tenant", [in_y, in_y], tenant=w.y, user=w.c.user)
    assert twice.error is not None and ASSISTANT_REPLY_INDEX in twice.error, f"two answers in tenant Y: {twice}"


async def check_maint_message_update(conn: AsyncConnection) -> None:
    """C22: app_chat_maint marks pending assistant rows of its tenant (any user) and can do NOTHING else on messages."""
    w = await seed_chat_world(conn)
    mark = "UPDATE app.messages SET outcome = 'interrupted', partial = true, completed_at = now() WHERE id = ANY(:ids)"

    def ids(*values: uuid.UUID) -> tuple[str, dict[str, Any]]:
        return (mark, {"ids": list(values)})

    _count(await run_as(conn, "app_chat_maint", ids(w.a.pending, w.b.pending), tenant=w.x), 2, "marking pending rows of A and B")
    _count(await run_as(conn, "app_chat_maint", ids(w.a.answer, w.b.answer), tenant=w.x), 0, "marking FINISHED assistant rows")
    _count(await run_as(conn, "app_chat_maint", ids(w.a.question, w.a.pending_question), tenant=w.x), 0, "marking USER rows")
    _count(await run_as(conn, "app_chat_maint", ids(w.c.pending), tenant=w.x), 0, "marking another tenant's pending row")
    _count(await run_as(conn, "app_chat_maint", ids(w.a.pending)), 0, "marking with no tenant set")
    for column in (*MESSAGE_FROZEN_COLUMNS, "content", "needs_review", "retrieval", "route"):
        _denied(
            await run_as(conn, "app_chat_maint", (f"UPDATE app.messages SET {column} = {column} WHERE id = :id", {"id": w.a.pending}),
                         tenant=w.x),
            f"maintenance UPDATE of messages.{column}",
        )
    _denied(
        await run_as(conn, "app_chat_maint", ("UPDATE app.message_citations SET label = 'S9' WHERE id = :id", {"id": w.a.citation}),
                     tenant=w.x),
        "maintenance UPDATE of message_citations.label",
    )
    for table in MAINT_TABLES:
        _denied(await run_as(conn, "app_chat_maint", f"INSERT INTO app.{table} (tenant_id) VALUES (gen_random_uuid())", tenant=w.x),
                f"maintenance INSERT into {table}")
        _denied(await run_as(conn, "app_chat_maint", f"TRUNCATE app.{table}", tenant=w.x), f"maintenance TRUNCATE {table}")
    # What the role is for (plan 9.4): delete and blank across users of its tenant.
    _count(await run_as(conn, "app_chat_maint", ("UPDATE app.message_citations SET snippet = '', heading_path = NULL "
                                                 "WHERE document_id = :d", {"d": w.document}), tenant=w.x),
           2, "blanking the citations of A and B")
    _count(await run_as(conn, "app_chat_maint", ("DELETE FROM app.messages WHERE id = ANY(:ids)", {"ids": [w.a.answer, w.b.answer]}),
                        tenant=w.x), 2, "maintenance DELETE of A's and B's answers")
    _count(await run_as(conn, "app_chat_maint", "DELETE FROM app.conversations", tenant=w.x), 2, "retention DELETE in tenant X")


async def check_tenant_message_writes(conn: AsyncConnection) -> None:
    """6a-8 narrowing: app_tenant updates only T3's columns of its OWN PENDING assistant row, deletes no message or
    citation, and the conversation delete still cascades."""
    w = await seed_chat_world(conn)
    me: dict[str, Any] = {"tenant": w.x, "user": w.a.user}
    t3 = (
        "UPDATE app.messages SET content = 'final answer', outcome = 'answered', partial = false, route = 'rag', "
        "language = 'en', prompt_tokens = 10, completion_tokens = 5, tokens_estimated = false, completed_at = now() "
        "WHERE id = :id"
    )
    _count(await run_as(conn, "app_tenant", (t3, {"id": w.a.pending}), **me), 1, "T3 on A's own pending row")
    _count(await run_as(conn, "app_tenant", (t3, {"id": w.a.answer}), **me), 0, "T3 on A's FINISHED answer")
    _count(await run_as(conn, "app_tenant", (t3, {"id": w.a.question}), **me), 0, "T3 on A's own question")
    for column in (*MESSAGE_FROZEN_COLUMNS, "guardrail"):
        _denied(await run_as(conn, "app_tenant", (f"UPDATE app.messages SET {column} = {column} WHERE id = :id", {"id": w.a.pending}), **me),
                f"app_tenant UPDATE of messages.{column}")
    _denied(await run_as(conn, "app_tenant", ("DELETE FROM app.messages WHERE id = :id", {"id": w.a.pending}), **me), "DELETE a message")
    _denied(await run_as(conn, "app_tenant", ("DELETE FROM app.message_citations WHERE id = :id", {"id": w.a.citation}), **me),
            "DELETE a citation")
    _denied(await run_as(conn, "app_tenant", ("UPDATE app.message_citations SET snippet = '' WHERE id = :id", {"id": w.a.citation}), **me),
            "UPDATE a citation")
    for column in ("tenant_id", "id", "user_id", "class_id", "subject_id", "created_at"):
        _denied(await run_as(conn, "app_tenant", (f"UPDATE app.conversations SET {column} = {column} WHERE id = :id",
                                                  {"id": w.a.conversation}), **me), f"app_tenant UPDATE of conversations.{column}")
    for column in ("tenant_id", "id", "message_id", "user_id", "created_at"):
        _denied(await run_as(conn, "app_tenant", (f"UPDATE app.message_feedback SET {column} = {column} WHERE id = :id",
                                                  {"id": w.a.feedback}), **me), f"app_tenant UPDATE of message_feedback.{column}")
    _count(await run_as(conn, "app_tenant", ("UPDATE app.conversations SET title = 't', message_count = 4, last_message_at = now() "
                                             "WHERE id = :id", {"id": w.a.conversation}), **me), 1, "counters and title")
    _count(await run_as(conn, "app_tenant", ("UPDATE app.message_feedback SET rating = -1, comment = NULL, updated_at = now() "
                                             "WHERE id = :id", {"id": w.a.feedback}), **me), 1, "feedback upsert columns")
    for table in CHAT_TABLES:
        _denied(await run_as(conn, "app_tenant", f"TRUNCATE app.{table}", **me), f"app_tenant TRUNCATE {table}")

    # The delete route: the conversation goes, and its messages, citations and feedback go with it (the cascade runs as
    # the table owner, so app_tenant needs no DELETE on them). B's rows stay.
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text("SET LOCAL ROLE app_tenant"))
        await conn.execute(text("SELECT set_config('app.tenant_id', :v, true)"), {"v": str(w.x)})
        await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": str(w.a.user)})
        deleted = await conn.execute(text("DELETE FROM app.conversations WHERE id = :id"), {"id": w.a.conversation})
        assert deleted.rowcount == 1, "A could not delete A's own conversation"
        await conn.execute(text("RESET ROLE"))
        for table in ("messages", "message_citations", "message_feedback"):
            per_user = dict((await conn.execute(
                text(f"SELECT user_id, count(*) FROM app.{table} WHERE tenant_id = :t GROUP BY 1"), {"t": w.x}
            )).all())
            assert set(per_user) == {w.b.user}, f"{table}: after A's conversation delete the rows per user are {per_user}"
    finally:
        await savepoint.rollback()


# Every outcome the plan (9.1) lists except 'pending': the values T3 and the cancel step may write. A literal on purpose.
FINAL_OUTCOMES = (
    "answered",
    "not_found",
    "refused_by_model",
    "no_valid_citations",
    "schedule_answered",
    "schedule_no_timetable",
    "clarify",
    "fallback_retrieval_only",
    "budget_retrieval_only",
    "interrupted",
    "cancelled",
    "truncated",
    "blocked_leak",
    "error",
)


async def check_tenant_finishes_pending_answer(conn: AsyncConnection, outcome: str) -> None:
    """As app_tenant (user A): ONE UPDATE that sets `outcome` moves A's pending answer to the final `outcome` (1 row,
    stored as written), and the very same UPDATE on a row that is already final changes 0 rows: on that row right
    after (now final) and on A's answer that was final from the start. Needs a WITH CHECK that accepts a non-pending
    new row: without one, Postgres would apply USING (outcome = 'pending') to the new row and refuse every outcome."""
    w = await seed_chat_world(conn)
    finish = text(
        "UPDATE app.messages SET outcome = :o, content = :c, partial = :p, completed_at = now() WHERE id = :id"
    )
    params = {"o": outcome, "c": f"text for {outcome}", "p": outcome in ("cancelled", "interrupted")}
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text("SET LOCAL ROLE app_tenant"))
        await conn.execute(text("SELECT set_config('app.tenant_id', :v, true)"), {"v": str(w.x)})
        await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": str(w.a.user)})
        try:
            first = await conn.execute(finish, {**params, "id": w.a.pending})
        except DBAPIError as exc:
            raise AssertionError(f"pending -> {outcome}: refused: {exc.orig}") from exc
        assert first.rowcount == 1, f"pending -> {outcome}: changed {first.rowcount} row(s), expected 1"
        again = await conn.execute(finish, {**params, "id": w.a.pending})
        assert again.rowcount == 0, f"{outcome} -> {outcome} again: a final row changed ({again.rowcount} row(s))"
        finished = await conn.execute(finish, {**params, "id": w.a.answer})
        assert finished.rowcount == 0, f"answered -> {outcome}: a final row changed ({finished.rowcount} row(s))"
        await conn.execute(text("RESET ROLE"))
        stored = (
            await conn.execute(
                text("SELECT id, role, user_id, outcome FROM app.messages WHERE tenant_id = :t AND id = ANY(:ids)"),
                {"t": w.x, "ids": [w.a.pending, w.a.answer]},
            )
        ).all()
        assert {tuple(r) for r in stored} == {
            (w.a.pending, "assistant", w.a.user, outcome),
            (w.a.answer, "assistant", w.a.user, "answered"),
        }, f"stored rows after the {outcome} update: {stored}"
    finally:
        await savepoint.rollback()


async def check_queue_grants(conn: AsyncConnection) -> None:
    """C21: app_tenant can only INSERT into the blank queue; app_chat_maint can only SELECT and DELETE, in its tenant."""
    w = await seed_chat_world(conn)
    me: dict[str, Any] = {"tenant": w.x, "user": w.a.user}
    insert = ("INSERT INTO app.chat_citation_blank_queue (tenant_id, document_id) VALUES (:t, :d)", {"t": w.x, "d": uuid.uuid4()})
    _count(await run_as(conn, "app_tenant", insert, **me), 1, "the purge transaction's queue insert")
    _count(await run_as(conn, "app_tenant", insert, tenant=w.x), 1, "the queue insert needs no user")
    _rls_refused(await run_as(conn, "app_tenant", ("INSERT INTO app.chat_citation_blank_queue (tenant_id, document_id) VALUES (:t, :d)",
                                                   {"t": w.y, "d": uuid.uuid4()}), **me), "a queue row for another tenant")
    for what, sql in (
        ("SELECT", "SELECT id FROM app.chat_citation_blank_queue"),
        ("INSERT RETURNING", insert[0] + " RETURNING id"),
        # A constant, not `document_id = document_id`: reading the column would also need SELECT.
        ("UPDATE", "UPDATE app.chat_citation_blank_queue SET document_id = gen_random_uuid()"),
        ("DELETE", "DELETE FROM app.chat_citation_blank_queue"),
        ("TRUNCATE", "TRUNCATE app.chat_citation_blank_queue"),
    ):
        _denied(await run_as(conn, "app_tenant", (sql, insert[1]) if ":" in sql else sql, **me), f"app_tenant {what} on the queue")

    seen = (await run_as(conn, "app_chat_maint", "SELECT tenant_id, id FROM app.chat_citation_blank_queue", tenant=w.x)).ok("drain read")
    assert set(seen.rows) == {(w.x, w.queue_x)}, f"the maintenance role reads queue rows {seen.rows}"
    _count(await run_as(conn, "app_chat_maint", "DELETE FROM app.chat_citation_blank_queue", tenant=w.x), 1, "drain delete in tenant X")
    _count(await run_as(conn, "app_chat_maint", ("DELETE FROM app.chat_citation_blank_queue WHERE id = :id", {"id": w.queue_y}),
                        tenant=w.x), 0, "drain delete of tenant Y's row")
    _denied(await run_as(conn, "app_chat_maint", insert, tenant=w.x), "maintenance INSERT into the queue")
    _denied(await run_as(conn, "app_chat_maint", "UPDATE app.chat_citation_blank_queue SET document_id = gen_random_uuid()",
                         tenant=w.x), "maintenance UPDATE of the queue")


async def check_other_roles_get_nothing(conn: AsyncConnection) -> None:
    """Plan 9.2: app_platform and app_worker get nothing on any chat table."""
    for role in ("app_platform", "app_worker"):
        for table in CHAT_TABLES:
            for sql in (
                f"SELECT 1 FROM app.{table} LIMIT 1",
                f"INSERT INTO app.{table} (tenant_id) VALUES (gen_random_uuid())",
                f"UPDATE app.{table} SET tenant_id = tenant_id WHERE false",
                f"DELETE FROM app.{table} WHERE false",
                f"TRUNCATE app.{table}",
            ):
                _denied(await run_as(conn, role, sql), f"{role}: {sql}")


# ── catalog checks ───────────────────────────────────────────────────────────────────────────────────


async def table_grants(conn: Any, table: str) -> set[tuple[str, str]]:
    rows = (
        await conn.execute(
            text("""
                SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, a.privilege_type
                FROM pg_class c, LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
                WHERE c.oid = to_regclass(:t) AND a.grantee <> c.relowner
            """),
            {"t": f"app.{table}"},
        )
    ).all()
    return {(r[0], r[1]) for r in rows}


async def column_grants(conn: Any, table: str) -> set[tuple[str, str, str]]:
    rows = (
        await conn.execute(
            text("""
                SELECT a.attname,
                       CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
                       x.privilege_type
                FROM pg_attribute a, LATERAL aclexplode(a.attacl) x
                WHERE a.attrelid = to_regclass(:t) AND a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL
            """),
            {"t": f"app.{table}"},
        )
    ).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def effective_table_privileges(conn: Any, table: str) -> set[tuple[str, str]]:
    """What each application role HAS on the table (inherited and PUBLIC grants included)."""
    found: set[tuple[str, str]] = set()
    for role in APP_ROLES:
        for privilege in PRIVILEGES:
            ok = (await conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                     {"r": role, "t": f"app.{table}", "p": privilege})).scalar_one()
            if ok:
                found.add((role, privilege))
    return found


async def assert_chat_table_grants(conn: Any, table: str) -> None:
    """EXACTLY the plan's grants: table level, effective per role, and per column."""
    expected = EXPECTED_TABLE_GRANTS[table]
    actual = await table_grants(conn, table)
    assert actual == expected, f"{table}: table grants differ. unexpected: {sorted(actual - expected)}; missing: {sorted(expected - actual)}"
    effective = await effective_table_privileges(conn, table)
    assert effective == set(expected), f"{table}: effective role privileges differ: {sorted(effective ^ set(expected))}"
    expected_cols = EXPECTED_COLUMN_GRANTS[table]
    cols = await column_grants(conn, table)
    assert cols == expected_cols, f"{table}: column grants differ. unexpected: {sorted(cols - expected_cols)}; missing: {sorted(expected_cols - cols)}"


async def assert_chat_rls_forced(conn: Any, table: str) -> None:
    row = (
        await conn.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
                           {"t": f"app.{table}"})
    ).one_or_none()
    assert row is not None, f"app.{table} does not exist"
    assert row[0], f"{table}: row level security is not enabled"
    assert row[1], f"{table}: row level security is not forced"


async def policies(conn: Any, table: str) -> set[PolicyRow]:
    rows = (
        await conn.execute(
            text("SELECT policyname, cmd, permissive, roles::text[], qual, with_check FROM pg_policies "
                 "WHERE schemaname = 'app' AND tablename = :t"),
            {"t": table},
        )
    ).all()
    return {(r[0], r[1], r[2], tuple(sorted(r[3])), r[4], r[5]) for r in rows}


async def assert_chat_policies(conn: Any, table: str) -> None:
    expected = EXPECTED_CHAT_POLICIES[table]
    actual = await policies(conn, table)
    assert actual == set(expected), (
        f"{table}: policies differ.\n unexpected: {sorted(actual - expected)}\n missing: {sorted(expected - actual)}"
    )


async def assert_assistant_reply_index(conn: Any) -> None:
    row = (
        await conn.execute(
            text("""
                SELECT i.indisunique,
                       (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(i.indkey) WITH ORDINALITY k(n, ord)
                        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n),
                       pg_get_expr(i.indpred, i.indrelid)
                FROM pg_index i WHERE i.indexrelid = to_regclass(:ix)
            """),
            {"ix": f"app.{ASSISTANT_REPLY_INDEX}"},
        )
    ).one_or_none()
    assert row is not None, f"index {ASSISTANT_REPLY_INDEX} is missing"
    assert row[0], f"{ASSISTANT_REPLY_INDEX} is not unique"
    assert list(row[1]) == ["tenant_id", "reply_to_id"], f"{ASSISTANT_REPLY_INDEX} columns: {row[1]}"
    assert row[2] == ASSISTANT, f"{ASSISTANT_REPLY_INDEX} predicate: {row[2]}"


async def chat_maint_footprint(conn: Any) -> dict[str, set[Any]]:
    """Everything app_chat_maint can reach in this database: schema usage, table and column privileges in schema app,
    policies that name it, and the roles it is a member of."""
    tables = (
        await conn.execute(
            text("""
                SELECT c.relname, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS p(priv)
                WHERE n.nspname = 'app' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND has_table_privilege('app_chat_maint', c.oid, p.priv)
            """)
        )
    ).all()
    sequences = (
        await conn.execute(
            text("""
                SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'app' AND c.relkind = 'S'
                  AND (has_sequence_privilege('app_chat_maint', c.oid, 'USAGE')
                       OR has_sequence_privilege('app_chat_maint', c.oid, 'SELECT')
                       OR has_sequence_privilege('app_chat_maint', c.oid, 'UPDATE'))
            """)
        )
    ).all()
    columns = (
        await conn.execute(
            text("""
                SELECT c.relname, a.attname, x.privilege_type
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped,
                     LATERAL aclexplode(a.attacl) x
                WHERE n.nspname = 'app' AND x.grantee = 'app_chat_maint'::regrole
            """)
        )
    ).all()
    named = (
        await conn.execute(
            text("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'app' AND 'app_chat_maint' = ANY(roles)")
        )
    ).all()
    member_of = (
        await conn.execute(
            text("SELECT pg_get_userbyid(roleid) FROM pg_auth_members WHERE member = 'app_chat_maint'::regrole")
        )
    ).all()
    usage = (await conn.execute(text("SELECT has_schema_privilege('app_chat_maint', 'app', 'USAGE')"))).scalar_one()
    return {
        "tables": {(r[0], r[1]) for r in tables},
        "sequences": {r[0] for r in sequences},
        "columns": {(r[0], r[1], r[2]) for r in columns},
        "policies": {(r[0], r[1]) for r in named},
        "member_of": {r[0] for r in member_of},
        "schema_usage": {usage},
    }


async def assert_chat_maint_reaches_only_the_plan(conn: Any) -> None:
    """Plan 9.2: app_chat_maint gets nothing on any other table, no INSERT anywhere, no UPDATE but the five columns."""
    found = await chat_maint_footprint(conn)
    expected_tables = {(t, p) for t in MAINT_TABLES for p in ("SELECT", "DELETE")}
    assert found["tables"] == expected_tables, (
        f"app_chat_maint table privileges differ. unexpected: {sorted(found['tables'] - expected_tables)}; "
        f"missing: {sorted(expected_tables - found['tables'])}"
    )
    expected_columns = {
        ("messages", c, "UPDATE") for c in MAINT_MESSAGE_UPDATE_COLUMNS
    } | {("message_citations", c, "UPDATE") for c in ("snippet", "heading_path")}
    assert found["columns"] == expected_columns, f"app_chat_maint column grants: {sorted(found['columns'] ^ expected_columns)}"
    assert not found["sequences"], f"app_chat_maint can use sequences: {found['sequences']}"
    assert not found["member_of"], f"app_chat_maint is a member of {found['member_of']} (it would inherit their grants)"
    assert found["schema_usage"] == {True}, "app_chat_maint has no USAGE on schema app"
    policy_tables = {t for t, _ in found["policies"]}
    assert policy_tables == set(MAINT_TABLES), f"policies naming app_chat_maint are on {sorted(policy_tables)}"


async def assert_no_chat_maint_footprint(conn: Any) -> None:
    """After the 0013 downgrade: no grant, column grant, policy or schema usage of app_chat_maint is left."""
    found = await chat_maint_footprint(conn)
    leftovers = {k: v for k, v in found.items() if v and v != {False}}
    assert not leftovers, f"app_chat_maint keeps grants after the downgrade: {leftovers}"


async def assert_role_is_plain(conn: Any) -> None:
    row = (
        await conn.execute(
            text("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin "
                 "FROM pg_roles WHERE rolname = 'app_chat_maint'")
        )
    ).one_or_none()
    assert row is not None, "role app_chat_maint does not exist"
    assert tuple(row) == (False, False, False, False, False, True), f"app_chat_maint attributes: {tuple(row)}"
