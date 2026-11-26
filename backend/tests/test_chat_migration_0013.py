"""
Step 6a-8 (docs/phase6-plan.md 9.2, 9.7 step 2, C21, C22, C26): the statements migration 0013 issues, read with a
fake `op` (no database), and the places that create the app_chat_maint role. Fast tier.

The database-side checks (catalog, behaviour as the real roles, the real downgrade and upgrade) are in
test_phase6_schema.py, test_chat_isolation.py and security/test_prove_it_fails_chat_rls.py.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
VERSIONS = BACKEND / "alembic" / "versions"
REPO = BACKEND.parent

CHAT_TABLES = ("conversations", "messages", "message_citations", "message_feedback", "chat_citation_blank_queue")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0013_static", VERSIONS / "0013_chat.py")
    assert spec is not None and spec.loader is not None, "0013_chat.py is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """Answers the migration's role checks. `maint` = (exists, superuser, bypassrls, member of another role)."""

    def __init__(self, *, roles: tuple[str, ...] = ("app_tenant", "app_platform", "app_worker", "app_chat_maint"),
                 maint: tuple[bool, bool, bool] = (False, False, False)) -> None:
        self.roles = roles
        self.maint = maint

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        role = (params or {}).get("r")
        if "rolsuper" in sql:
            row = self.maint if role in self.roles else None
            return SimpleNamespace(one_or_none=lambda: row)
        if "pg_roles" in sql:
            return SimpleNamespace(scalar=lambda: 1 if role in self.roles else None)
        raise AssertionError(f"unexpected query: {sql}")


def _run(fn: str, monkeypatch: pytest.MonkeyPatch, conn: _FakeConn | None = None) -> list[str]:
    module = _load()
    executed: list[str] = []
    fake = conn or _FakeConn()
    monkeypatch.setattr(
        module, "op",
        SimpleNamespace(get_bind=lambda: fake, execute=lambda stmt, *a, **k: executed.append(" ".join(str(stmt).split()))),
    )
    getattr(module, fn)()
    return executed


# ── revision and role checks ─────────────────────────────────────────────────────────────────────────


def test_0013_follows_0012() -> None:
    module = _load()
    assert (module.revision, module.down_revision) == ("0013", "0012")


@pytest.mark.parametrize("missing", ["app_chat_maint", "app_tenant", "app_platform", "app_worker"])
def test_upgrade_refuses_a_missing_role(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    roles = tuple(r for r in ("app_tenant", "app_platform", "app_worker", "app_chat_maint") if r != missing)
    with pytest.raises(RuntimeError, match=missing):
        _run("upgrade", monkeypatch, _FakeConn(roles=roles))


@pytest.mark.parametrize("maint", [(True, False, False), (False, True, False), (False, False, True)])
def test_upgrade_refuses_a_maint_role_that_could_bypass_its_policies(maint: tuple[bool, bool, bool],
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A superuser, a BYPASSRLS role or a member of another role (it would inherit that role's grants and
    policies) would make every policy of this migration moot."""
    with pytest.raises(RuntimeError, match="app_chat_maint"):
        _run("upgrade", monkeypatch, _FakeConn(maint=maint))


# ── upgrade statements ───────────────────────────────────────────────────────────────────────────────


def test_every_chat_table_gets_forced_rls_and_tenant_isolation_to_app_tenant_only(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    for table in CHAT_TABLES:
        assert f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY" in executed
        assert f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY" in executed
        isolation = [s for s in executed if s.startswith(f"CREATE POLICY tenant_isolation ON app.{table} ")]
        assert len(isolation) == 1, table
        # C26: without TO, tenant_isolation would also apply to app_chat_maint and OR away its pending condition.
        assert " AS PERMISSIVE FOR ALL TO app_tenant USING " in isolation[0], isolation[0]
    assert not any(s.startswith("CREATE POLICY tenant_isolation ON app.llm_usage_events") for s in executed)


def test_owner_policy_is_restrictive_to_app_tenant_on_the_four_content_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    owner = [s for s in executed if s.startswith("CREATE POLICY chat_owner ")]
    assert sorted(s.split(" ON ")[1].split(" ")[0] for s in owner) == sorted(
        f"app.{t}" for t in ("conversations", "messages", "message_citations", "message_feedback")
    )
    for statement in owner:
        assert " AS RESTRICTIVE FOR ALL TO app_tenant USING " in statement, statement
        assert "OR" not in statement.split(" USING ")[1], f"the owner policy has a bypass: {statement}"


def test_maint_update_policy_matches_only_pending_assistant_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    [update] = [s for s in executed if s.startswith("CREATE POLICY chat_maint_update ")]
    using, check = update.split(" USING ")[1].split(" WITH CHECK ")
    assert "AS PERMISSIVE FOR UPDATE TO app_chat_maint" in update
    assert "role = 'assistant'" in using and "outcome = 'pending'" in using and "app.tenant_id" in using
    assert "pending" not in check and "app.tenant_id" in check


def test_grants_are_exactly_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every GRANT and REVOKE the upgrade issues, in order. A widening shows up here before any database."""
    executed = _run("upgrade", monkeypatch)
    statements = [s for s in executed if s.startswith(("GRANT", "REVOKE ALL ON app.", "REVOKE SELECT", "REVOKE UPDATE",
                                                       "REVOKE DELETE"))]
    tenant_message_cols = (
        "content, route, outcome, partial, intent, rule_ids, router_confidence, language, retrieval_query, provider, "
        "model_key, prompt_version, prompt_tokens, completion_tokens, tokens_estimated, latency_ms, retrieval, "
        "invalid_citation_count, flagged_sources_used, needs_review, completed_at"
    )
    expected = [
        *(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{t} TO app_tenant" for t in CHAT_TABLES),
        "REVOKE UPDATE ON app.conversations FROM app_tenant",
        "GRANT UPDATE (title, last_message_at, message_count) ON app.conversations TO app_tenant",
        "REVOKE UPDATE, DELETE ON app.messages FROM app_tenant",
        f"GRANT UPDATE ({tenant_message_cols}) ON app.messages TO app_tenant",
        "REVOKE UPDATE, DELETE ON app.message_citations FROM app_tenant",
        "REVOKE UPDATE ON app.message_feedback FROM app_tenant",
        "GRANT UPDATE (rating, comment, updated_at) ON app.message_feedback TO app_tenant",
        "REVOKE SELECT, UPDATE, DELETE ON app.chat_citation_blank_queue FROM app_tenant",
        *(f"REVOKE ALL ON app.{t} FROM app_platform, app_worker" for t in CHAT_TABLES),
        "GRANT USAGE ON SCHEMA app TO app_chat_maint",
        *(f"GRANT SELECT, DELETE ON app.{t} TO app_chat_maint" for t in (*CHAT_TABLES, "llm_usage_events")),
        "GRANT UPDATE (outcome, partial, completed_at) ON app.messages TO app_chat_maint",
        "GRANT UPDATE (snippet, heading_path) ON app.message_citations TO app_chat_maint",
    ]
    assert statements == expected
    joined = "\n".join(executed)
    assert "TRUNCATE" not in joined and "GRANT ALL" not in joined
    assert not re.search(r"GRANT [^\n]* TO (app_platform|app_worker|PUBLIC)", joined)
    assert not re.search(r"GRANT INSERT[^\n]* TO app_chat_maint", joined)


def test_policies_are_exactly_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    created = sorted(
        (s.split(" ")[2], s.split(" ON ")[1].split(" ")[0]) for s in executed if s.startswith("CREATE POLICY ")
    )
    expected = sorted(
        [("tenant_isolation", f"app.{t}") for t in CHAT_TABLES]
        + [("chat_owner", f"app.{t}") for t in ("conversations", "messages", "message_citations", "message_feedback")]
        + [("chat_tenant_update_pending", "app.messages"), ("chat_maint_select", "app.messages"),
           ("chat_maint_delete", "app.messages"), ("chat_maint_update", "app.messages")]
        + [("chat_maint", f"app.{t}") for t in ("conversations", "message_citations", "message_feedback",
                                                "chat_citation_blank_queue", "llm_usage_events")]
    )
    assert created == expected


def test_no_maintenance_setting_and_the_unique_reply_index(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    joined = "\n".join(executed)
    assert "app.chat_maintenance" not in joined, "the retired maintenance bypass setting is back"
    assert "CREATE UNIQUE INDEX ux_messages_assistant_reply ON app.messages (tenant_id, reply_to_id) WHERE role = 'assistant'" in executed


# ── downgrade ────────────────────────────────────────────────────────────────────────────────────────


def test_downgrade_drops_the_tables_in_reverse_order_and_revokes_every_maint_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run("downgrade", monkeypatch) == [
        "DROP POLICY IF EXISTS chat_maint ON app.llm_usage_events",
        "REVOKE ALL ON app.llm_usage_events FROM app_chat_maint",
        "DROP TABLE IF EXISTS app.chat_citation_blank_queue",
        "DROP TABLE IF EXISTS app.message_feedback",
        "DROP TABLE IF EXISTS app.message_citations",
        "DROP TABLE IF EXISTS app.messages",
        "DROP TABLE IF EXISTS app.conversations",
        "REVOKE USAGE ON SCHEMA app FROM app_chat_maint",
    ]


# ── where the role is created (no password, no secret in the repo) ───────────────────────────────────


def test_init_sql_creates_the_maint_role_in_both_databases_without_a_password() -> None:
    sql = (REPO / "infra" / "postgres" / "init.sql").read_text(encoding="utf-8")
    creates = re.findall(r"CREATE ROLE app_chat_maint[^;]*;", sql)
    assert len(creates) == 2, "init.sql must create app_chat_maint in the ragplatform and the ragplatform_test sections"
    for statement in creates:
        assert "NOBYPASSRLS" in statement and "NOSUPERUSER" in statement and "LOGIN" in statement
        assert "PASSWORD" not in statement.upper(), "no password for app_chat_maint in the repo"
    # The schema grant is the migration's (so the downgrade can take it back), not init.sql's.
    assert not re.search(r"GRANT USAGE ON SCHEMA app TO [^;]*app_chat_maint", sql)


def test_prepare_test_db_requires_and_creates_the_maint_role_without_a_password() -> None:
    from app.scripts import prepare_test_db

    assert "app_chat_maint" in prepare_test_db.REQUIRED_ROLES
    statement = " ".join(prepare_test_db.CREATE_CHAT_MAINT_ROLE_SQL.split())
    assert statement.startswith("CREATE ROLE app_chat_maint LOGIN")
    assert "NOBYPASSRLS" in statement and "NOSUPERUSER" in statement
    assert "PASSWORD" not in statement.upper()


# ── model constants the migration uses ───────────────────────────────────────────────────────────────


def test_model_constants_match_the_plan() -> None:
    from app.db.models import chat

    assert chat.MESSAGE_ROLES == ("user", "assistant")
    assert chat.MESSAGE_ROUTES == ("schedule", "clarify", "rag")
    assert chat.MESSAGE_OUTCOMES == (
        "pending", "answered", "not_found", "refused_by_model", "no_valid_citations", "schedule_answered",
        "schedule_no_timetable", "clarify", "fallback_retrieval_only", "budget_retrieval_only", "interrupted",
        "cancelled", "truncated", "blocked_leak", "error",
    )
    assert chat.MESSAGE_LANGUAGES == ("en", "mr", "hi", "unknown")
    assert chat.CITATION_LOCATION_KINDS == ("page", "slide", "section")
    assert chat.MESSAGE_MAINT_UPDATE_COLUMNS == ("outcome", "partial", "completed_at")
    assert chat.CITATION_MAINT_UPDATE_COLUMNS == ("snippet", "heading_path")
    assert chat.CONVERSATION_TENANT_UPDATE_COLUMNS == ("title", "last_message_at", "message_count")
    assert chat.FEEDBACK_TENANT_UPDATE_COLUMNS == ("rating", "comment", "updated_at")
    frozen = {"tenant_id", "id", "conversation_id", "user_id", "role", "reply_to_id", "client_request_id", "created_at",
              "guardrail"}
    assert not frozen & set(chat.MESSAGE_TENANT_UPDATE_COLUMNS)
    assert set(chat.MESSAGE_TENANT_UPDATE_COLUMNS) | frozen == {c.name for c in chat.Message.__table__.columns}
