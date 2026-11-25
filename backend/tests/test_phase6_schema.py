"""
Step 6a-5 (docs/phase6-plan.md 9.1, 9.2, 9.7 step 1): migration 0012, tenant_settings and llm_usage_events.

FORCE RLS, the one tenant_isolation policy, the grants per role (narrower than _secure_table's default), the
columns and their checks, the FK actions, the backfill, and the statements the migration itself runs.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.tenant_db import tenant_session_context
from app.db.models.llm_usage import USAGE_OUTCOMES, USAGE_PURPOSES
from app.db.models.tenant import Tenant
from app.db.models.user import User
from tests.security.usage_assertions import (
    PHASE6_USAGE_TABLES,
    assert_policies,
    assert_rls_forced,
    assert_table_grants,
)

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MIGRATION_FILE = "0012_llm_usage_tenant_settings.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0012_under_test", VERSIONS / MIGRATION_FILE)
    assert spec is not None and spec.loader is not None, f"{MIGRATION_FILE} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _tenant(raw_db: AsyncSession, label: str = "p6") -> Tenant:
    t = Tenant(slug=f"{label}-{uuid.uuid4().hex}", name="Phase 6 schema", kind="school")
    raw_db.add(t)
    await raw_db.commit()
    return t


async def _user(raw_db: AsyncSession, tenant_id: uuid.UUID) -> User:
    u = User(
        tenant_id=tenant_id, email=f"p6-{uuid.uuid4().hex}@example.com", full_name="P6", password_hash="x", status="active"
    )
    raw_db.add(u)
    await raw_db.commit()
    return u


def _usage_values(tenant_id: uuid.UUID, **over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "t": tenant_id,
        "id": uuid.uuid4(),
        "user_id": None,
        "message_id": None,
        "purpose": "answer",
        "provider": "groq",
        "model_key": "groq:openai/gpt-oss-120b",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "estimated": False,
        "latency_ms": 120,
        "outcome": "ok",
        "attempt": 1,
    }
    values.update(over)
    return values


INSERT_USAGE = text("""
    INSERT INTO app.llm_usage_events (tenant_id, id, user_id, message_id, purpose, provider, model_key, prompt_tokens,
                                      completion_tokens, estimated, latency_ms, outcome, attempt)
    VALUES (:t, :id, :user_id, :message_id, :purpose, :provider, :model_key, :prompt_tokens, :completion_tokens,
            :estimated, :latency_ms, :outcome, :attempt)
""")


# ── revision chain ────────────────────────────────────────────────────────────────────────────────────


def test_0012_follows_0011() -> None:
    module = _load_migration()
    assert module.revision == "0012"
    assert module.down_revision == "0011"


async def test_the_test_database_is_at_0012_or_later(raw_db: AsyncSession) -> None:
    version = (await raw_db.execute(text("SELECT version_num FROM app.alembic_version"))).scalar_one()
    assert version >= "0012"


# ── RLS, policies, grants ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", PHASE6_USAGE_TABLES)
async def test_rls_is_enabled_and_forced(table: str, raw_db: AsyncSession) -> None:
    await assert_rls_forced(raw_db, table)


@pytest.mark.parametrize("table", PHASE6_USAGE_TABLES)
async def test_exactly_the_tenant_isolation_policy(table: str, raw_db: AsyncSession) -> None:
    await assert_policies(raw_db, table)


@pytest.mark.parametrize("table", PHASE6_USAGE_TABLES)
async def test_grants_are_exactly_the_plan_for_every_role(table: str, raw_db: AsyncSession) -> None:
    await assert_table_grants(raw_db, table)


async def test_app_tenant_cannot_delete_settings_or_change_usage_rows(raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": t.id})
    await raw_db.execute(INSERT_USAGE, _usage_values(t.id))
    await raw_db.commit()
    for statement in (
        "DELETE FROM app.tenant_settings",
        "UPDATE app.llm_usage_events SET latency_ms = 1",
        "DELETE FROM app.llm_usage_events",
        "TRUNCATE app.llm_usage_events",
        "TRUNCATE app.tenant_settings",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session_context(str(t.id)) as session:
                await session.execute(text(statement))
    # Control: what the plan grants works.
    async with tenant_session_context(str(t.id)) as session:
        res = await session.execute(text("UPDATE app.tenant_settings SET chat_retention_days = 30"))
        assert res.rowcount == 1
        assert (await session.execute(text("SELECT count(*) FROM app.llm_usage_events"))).scalar_one() == 1


@pytest.mark.parametrize("url_name", ["PLATFORM_DATABASE_URL", "WORKER_DATABASE_URL"])
@pytest.mark.parametrize("table", PHASE6_USAGE_TABLES)
async def test_platform_and_worker_roles_get_nothing(url_name: str, table: str) -> None:
    url = getattr(settings, url_name)
    assert url, f"{url_name} is not set"
    engine = create_async_engine(url)
    try:
        for statement in (
            f"SELECT 1 FROM app.{table} LIMIT 1",
            f"INSERT INTO app.{table} (tenant_id) VALUES (gen_random_uuid())",
            f"UPDATE app.{table} SET tenant_id = tenant_id WHERE false",
            f"DELETE FROM app.{table} WHERE false",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with engine.connect() as conn:
                    await conn.execute(text(statement))
    finally:
        await engine.dispose()


# ── tenant_settings columns ───────────────────────────────────────────────────────────────────────────


async def _columns(raw_db: AsyncSession, table: str) -> dict[str, tuple[str, str, str | None]]:
    rows = (
        await raw_db.execute(
            text("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns WHERE table_schema = 'app' AND table_name = :t
            """),
            {"t": table},
        )
    ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def test_tenant_settings_columns_and_defaults(raw_db: AsyncSession) -> None:
    cols = await _columns(raw_db, "tenant_settings")
    assert set(cols) == {
        "tenant_id", "chat_enabled", "chat_retention_days", "allowed_llm_providers", "chat_daily_token_budget",
        "updated_at", "updated_by",
    }
    assert cols["tenant_id"][:2] == ("uuid", "NO")
    assert cols["chat_enabled"][:2] == ("boolean", "NO")
    assert cols["chat_retention_days"][:2] == ("integer", "NO")
    assert cols["allowed_llm_providers"][:2] == ("ARRAY", "YES")
    assert cols["chat_daily_token_budget"][:2] == ("integer", "YES")
    assert cols["updated_by"][:2] == ("uuid", "YES")

    t = await _tenant(raw_db)
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": t.id})
    row = (
        await raw_db.execute(
            text(
                "SELECT chat_enabled, chat_retention_days, allowed_llm_providers, chat_daily_token_budget, updated_by "
                "FROM app.tenant_settings WHERE tenant_id = :t"
            ),
            {"t": t.id},
        )
    ).one()
    assert tuple(row) == (True, 90, None, None, None)


async def test_tenant_settings_primary_key_is_the_tenant(raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": t.id})
    await raw_db.commit()
    with pytest.raises(IntegrityError, match="tenant_settings_pkey"):
        await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": t.id})


@pytest.mark.parametrize(
    "column,value,ok",
    [
        ("chat_retention_days", 0, False),
        ("chat_retention_days", 1, True),
        ("chat_retention_days", 730, True),
        ("chat_retention_days", 731, False),
        ("chat_daily_token_budget", 0, False),
        ("chat_daily_token_budget", -5, False),
        ("chat_daily_token_budget", 1, True),
    ],
)
async def test_tenant_settings_checks(column: str, value: int, ok: bool, raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    statement = text(f"INSERT INTO app.tenant_settings (tenant_id, {column}) VALUES (:t, :v)")
    if ok:
        await raw_db.execute(statement, {"t": t.id, "v": value})
    else:
        with pytest.raises(IntegrityError, match="check constraint"):
            await raw_db.execute(statement, {"t": t.id, "v": value})


# ── llm_usage_events columns: numbers and names only, no content ──────────────────────────────────────


async def test_llm_usage_events_holds_numbers_and_names_only(raw_db: AsyncSession) -> None:
    """Plan 9.1: no content of any kind. The only text columns are the two enumerations and the two names; a new
    column (a question, an answer, a prompt) fails here."""
    cols = await _columns(raw_db, "llm_usage_events")
    assert {name: dtype for name, (dtype, _n, _d) in cols.items()} == {
        "tenant_id": "uuid",
        "id": "uuid",
        "user_id": "uuid",
        "message_id": "uuid",
        "purpose": "character varying",
        "provider": "character varying",
        "model_key": "character varying",
        "prompt_tokens": "integer",
        "completion_tokens": "integer",
        "estimated": "boolean",
        "latency_ms": "integer",
        "outcome": "character varying",
        "attempt": "smallint",
        "created_at": "timestamp with time zone",
    }
    nullable = {name for name, (_t, n, _d) in cols.items() if n == "YES"}
    assert nullable == {"user_id", "message_id"}
    lengths = dict(
        (
            await raw_db.execute(
                text(
                    "SELECT column_name, character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = 'app' AND table_name = 'llm_usage_events' AND data_type = 'character varying'"
                )
            )
        ).all()
    )
    assert lengths == {"purpose": 16, "provider": 32, "model_key": 200, "outcome": 20}


async def test_llm_usage_events_primary_key_and_index(raw_db: AsyncSession) -> None:
    pk = (
        await raw_db.execute(
            text("""
                SELECT array_agg(a.attname ORDER BY k.ord)
                FROM pg_constraint c
                CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.conrelid = 'app.llm_usage_events'::regclass AND c.contype = 'p'
            """)
        )
    ).scalar_one()
    assert list(pk) == ["tenant_id", "id"]
    indexdef = (
        await raw_db.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'app' AND indexname = 'ix_llm_usage_events_tenant_created'")
        )
    ).scalar_one()
    assert "(tenant_id, created_at)" in indexdef


@pytest.mark.parametrize(
    "over",
    [
        {"purpose": "chat"},
        {"outcome": "great"},
        {"prompt_tokens": -1},
        {"completion_tokens": -1},
        {"latency_ms": -1},
        {"attempt": 0},
        {"provider": "Groq Inc"},
        {"model_key": "What is photosynthesis? Explain it in detail"},
        {"model_key": "groq:"},
        {"provider": "groq", "model_key": "openrouter:openai/gpt-oss-120b"},
    ],
)
async def test_llm_usage_events_checks_refuse(over: dict[str, Any], raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    with pytest.raises(IntegrityError, match="check constraint"):
        await raw_db.execute(INSERT_USAGE, _usage_values(t.id, **over))


async def test_llm_usage_events_checks_accept_every_planned_value(raw_db: AsyncSession) -> None:
    assert USAGE_PURPOSES == ("answer", "condense")
    assert USAGE_OUTCOMES == (
        "ok", "rate_limited", "timeout", "unavailable", "bad_request", "blocked", "auth_error", "cancelled", "interrupted",
    )
    t = await _tenant(raw_db)
    for purpose in USAGE_PURPOSES:
        for outcome in USAGE_OUTCOMES:
            await raw_db.execute(INSERT_USAGE, _usage_values(t.id, purpose=purpose, outcome=outcome))
    await raw_db.execute(INSERT_USAGE, _usage_values(t.id, provider="openrouter", model_key="openrouter:meta-llama/x:free"))


# ── foreign keys ──────────────────────────────────────────────────────────────────────────────────────


async def _foreign_keys(raw_db: AsyncSession, table: str) -> set[tuple[str, tuple[str, ...], str, tuple[str, ...] | None]]:
    """(referenced table, local columns, delete action, SET NULL columns) of every FK of the table."""
    rows = (
        await raw_db.execute(
            text("""
                SELECT c.confrelid::regclass::text,
                       (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey) WITH ORDINALITY k(n, ord)
                        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n),
                       c.confdeltype::text,
                       (SELECT array_agg(a.attname ORDER BY a.attname) FROM unnest(c.confdelsetcols) s(n)
                        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = s.n)
                FROM pg_constraint c WHERE c.conrelid = to_regclass(:t) AND c.contype = 'f'
            """),
            {"t": f"app.{table}"},
        )
    ).all()
    return {(r[0], tuple(r[1]), r[2], tuple(r[3]) if r[3] else None) for r in rows}


async def test_foreign_key_actions(raw_db: AsyncSession) -> None:
    assert await _foreign_keys(raw_db, "tenant_settings") == {
        ("app.tenants", ("tenant_id",), "c", None),
        ("app.users", ("tenant_id", "updated_by"), "n", ("updated_by",)),
    }
    # message_id has no FK on purpose: a usage row survives the deletion of its conversation (plan 9.1).
    assert await _foreign_keys(raw_db, "llm_usage_events") == {
        ("app.tenants", ("tenant_id",), "c", None),
        ("app.users", ("tenant_id", "user_id"), "n", ("user_id",)),
    }


async def test_deleting_a_user_keeps_the_usage_row_and_the_settings_row(raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    u = await _user(raw_db, t.id)
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, updated_by) VALUES (:t, :u)"), {"t": t.id, "u": u.id}
    )
    message = uuid.uuid4()
    await raw_db.execute(INSERT_USAGE, _usage_values(t.id, user_id=u.id, message_id=message))
    await raw_db.commit()

    await raw_db.execute(text("DELETE FROM app.users WHERE tenant_id = :t AND id = :u"), {"t": t.id, "u": u.id})
    await raw_db.commit()
    usage = (
        await raw_db.execute(text("SELECT tenant_id, user_id, message_id FROM app.llm_usage_events WHERE tenant_id = :t"), {"t": t.id})
    ).one()
    assert tuple(usage) == (t.id, None, message)
    s = (await raw_db.execute(text("SELECT tenant_id, updated_by FROM app.tenant_settings WHERE tenant_id = :t"), {"t": t.id})).one()
    assert tuple(s) == (t.id, None)


async def test_deleting_a_tenant_removes_both_rows(raw_db: AsyncSession) -> None:
    t = await _tenant(raw_db)
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": t.id})
    await raw_db.execute(INSERT_USAGE, _usage_values(t.id))
    await raw_db.commit()
    await raw_db.execute(text("DELETE FROM app.tenants WHERE id = :t"), {"t": t.id})
    await raw_db.commit()
    for table in PHASE6_USAGE_TABLES:
        count = (await raw_db.execute(text(f"SELECT count(*) FROM app.{table} WHERE tenant_id = :t"), {"t": t.id})).scalar_one()
        assert count == 0, table


async def test_a_user_of_another_tenant_cannot_be_referenced(raw_db: AsyncSession) -> None:
    a_id = (await _tenant(raw_db, "p6a")).id
    b_id = (await _tenant(raw_db, "p6b")).id
    ub_id = (await _user(raw_db, b_id)).id
    with pytest.raises(IntegrityError, match="fk_llm_usage_events_user"):
        await raw_db.execute(INSERT_USAGE, _usage_values(a_id, user_id=ub_id))
    await raw_db.rollback()
    with pytest.raises(IntegrityError, match="fk_tenant_settings_updated_by"):
        await raw_db.execute(
            text("INSERT INTO app.tenant_settings (tenant_id, updated_by) VALUES (:t, :u)"), {"t": a_id, "u": ub_id}
        )


# ── backfill ──────────────────────────────────────────────────────────────────────────────────────────


async def test_backfill_gives_every_tenant_exactly_one_row_and_keeps_existing_ones(raw_db: AsyncSession) -> None:
    module = _load_migration()
    bare = [await _tenant(raw_db, "bf") for _ in range(2)]
    kept = await _tenant(raw_db, "bf-kept")
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, chat_daily_token_budget) VALUES (:t, 1234)"), {"t": kept.id}
    )
    await raw_db.commit()

    for _ in range(2):  # idempotent
        await raw_db.execute(text(module.BACKFILL_TENANT_SETTINGS_SQL))
        await raw_db.commit()
        missing = (
            await raw_db.execute(
                text("SELECT count(*) FROM app.tenants t WHERE NOT EXISTS (SELECT 1 FROM app.tenant_settings s WHERE s.tenant_id = t.id)")
            )
        ).scalar_one()
        assert missing == 0, "a tenant has no settings row after the backfill"
        for t in [*bare, kept]:
            n = (await raw_db.execute(text("SELECT count(*) FROM app.tenant_settings WHERE tenant_id = :t"), {"t": t.id})).scalar_one()
            assert n == 1
    budget = (
        await raw_db.execute(text("SELECT chat_daily_token_budget FROM app.tenant_settings WHERE tenant_id = :t"), {"t": kept.id})
    ).scalar_one()
    assert budget == 1234


# ── the statements the migration runs (fake op) ───────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, *, bypass: bool = True, roles: tuple[str, ...] = ("app_tenant", "app_platform", "app_worker")) -> None:
        self.bypass = bypass
        self.roles = roles

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if "rolbypassrls" in sql:
            return SimpleNamespace(scalar=lambda: self.bypass)
        if "pg_roles" in sql:
            role = (params or {}).get("r")
            return SimpleNamespace(scalar=lambda: 1 if role in self.roles else None)
        raise AssertionError(f"unexpected query: {sql}")


def _run(fn_name: str, monkeypatch: pytest.MonkeyPatch, conn: _FakeConn | None = None) -> list[str]:
    module = _load_migration()
    executed: list[str] = []
    fake = conn or _FakeConn()
    monkeypatch.setattr(
        module, "op", SimpleNamespace(get_bind=lambda: fake, execute=lambda stmt, *a, **k: executed.append(" ".join(str(stmt).split())))
    )
    getattr(module, fn_name)()
    return executed


def test_upgrade_forces_rls_backfills_and_narrows_the_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    executed = _run("upgrade", monkeypatch)
    joined = "\n".join(executed)
    for table in PHASE6_USAGE_TABLES:
        assert f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY" in executed
        assert f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY" in executed
        assert f"CREATE POLICY tenant_isolation ON app.{table}" in joined
    assert "REVOKE DELETE ON app.tenant_settings FROM app_tenant" in executed
    assert "REVOKE UPDATE, DELETE ON app.llm_usage_events FROM app_tenant" in executed
    assert " ".join(module.BACKFILL_TENANT_SETTINGS_SQL.split()) in executed
    # The backfill runs after the table exists and after its grants are narrowed.
    backfill_at = executed.index(" ".join(module.BACKFILL_TENANT_SETTINGS_SQL.split()))
    assert backfill_at > executed.index("REVOKE DELETE ON app.tenant_settings FROM app_tenant")
    # Nothing for the other roles, and nothing of 0013 (the chat maintenance role and its policy).
    for statement in executed:
        if statement.startswith("GRANT"):
            assert statement.endswith("TO app_tenant"), statement
    assert "app_chat_maint" not in joined
    assert "chat_maint" not in joined


def test_downgrade_drops_both_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("downgrade", monkeypatch) == [
        "DROP TABLE IF EXISTS app.llm_usage_events",
        "DROP TABLE IF EXISTS app.tenant_settings",
    ]


def test_upgrade_refuses_a_role_that_cannot_see_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backfill reads app.tenants (FORCE RLS since 0010): a role without BYPASSRLS would see no tenant and
    silently backfill nothing."""
    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        _run("upgrade", monkeypatch, _FakeConn(bypass=False))


def test_upgrade_refuses_when_app_tenant_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="app_tenant"):
        _run("upgrade", monkeypatch, _FakeConn(roles=("app_platform", "app_worker")))
