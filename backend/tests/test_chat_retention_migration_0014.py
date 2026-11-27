"""
Step 6a-12b (docs/phase6-plan.md 9.1, 9.7, C35; owner decisions Q4 and C35 option a): the statements migration 0014 issues,
read with a fake `op` (no database). Fast tier.

  - 0014 makes tenant_settings.chat_retention_days nullable with NO default, and replaces the CHECK by
    "NULL or 30 to 3650";
  - it NEVER changes a row: no UPDATE, INSERT or DELETE statement. A stored 90 that no admin chose cannot be told apart
    from a chosen 90 by the migration, so it stays 90 until the owner decides (the build record shows the query);
  - it refuses to run (with a message, changing nothing) when a row already holds fewer than 30 days (a row the 6a-12a
    route accepted), so that no value is silently rewritten; it needs a role that bypasses RLS to see every row;
  - its downgrade refuses while a row holds NULL or more than 730 days, so that "keep everything" is never turned into a
    deletion period.
The database-side behaviour (the CHECK, the default row) is in test_phase6_schema.py and test_chat_settings_api.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
VERSIONS = BACKEND / "alembic" / "versions"


def _load() -> ModuleType:
    matches = sorted(VERSIONS.glob("0014_*.py"))
    assert len(matches) == 1, f"expected exactly one 0014 migration, found {matches}"
    spec = importlib.util.spec_from_file_location("migration_0014_static", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """Answers the migration's own queries: the RLS-bypass check and the count of rows it would refuse on."""

    def __init__(self, *, bypass: bool = True, too_short: int = 0, unrepresentable: int = 0) -> None:
        self.bypass = bypass
        self.too_short = too_short
        self.unrepresentable = unrepresentable
        self.queries: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.queries.append(sql)
        if "rolsuper" in sql:
            return SimpleNamespace(scalar=lambda: self.bypass)
        if "chat_retention_days IS NULL" in sql:  # the downgrade check
            return SimpleNamespace(scalar=lambda: self.unrepresentable)
        if "chat_retention_days <" in sql:  # the upgrade check
            return SimpleNamespace(scalar=lambda: self.too_short)
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


def test_0014_follows_0013() -> None:
    module = _load()
    assert (module.revision, module.down_revision) == ("0014", "0013")


def test_upgrade_makes_the_column_nullable_without_a_default_and_checks_30_to_3650_or_null(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    joined = "\n".join(executed)
    assert "ALTER COLUMN chat_retention_days DROP NOT NULL" in joined
    assert "ALTER COLUMN chat_retention_days DROP DEFAULT" in joined
    assert "DROP CONSTRAINT ck_tenant_settings_retention_days" in joined
    add = [s for s in executed if "ADD CONSTRAINT ck_tenant_settings_retention_days" in s]
    assert len(add) == 1
    assert "chat_retention_days IS NULL OR chat_retention_days BETWEEN 30 AND 3650" in add[0]


def test_upgrade_never_changes_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner decides what happens to stored 90s: the migration holds no UPDATE, INSERT, DELETE or TRUNCATE."""
    for statement in _run("upgrade", monkeypatch):
        words = statement.upper().split()
        assert not {"UPDATE", "INSERT", "DELETE", "TRUNCATE"} & set(words[:1]), statement
        assert not statement.upper().startswith(("WITH ", "MERGE ")), statement
        assert "SET DEFAULT" not in statement.upper()


def test_upgrade_refuses_when_a_row_is_below_30_days_and_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []
    module = _load()
    conn = _FakeConn(too_short=2)
    monkeypatch.setattr(
        module, "op",
        SimpleNamespace(get_bind=lambda: conn, execute=lambda stmt, *a, **k: executed.append(str(stmt))),
    )
    with pytest.raises(RuntimeError, match="30") as err:
        module.upgrade()
    assert executed == [], "nothing may be executed before the refusal"
    assert "2" in str(err.value)  # how many rows


def test_upgrade_needs_a_role_that_bypasses_rls(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        _run("upgrade", monkeypatch, _FakeConn(bypass=False))


def test_downgrade_restores_not_null_default_90_and_the_old_check(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("downgrade", monkeypatch)
    joined = "\n".join(executed)
    assert "ALTER COLUMN chat_retention_days SET DEFAULT 90" in joined
    assert "ALTER COLUMN chat_retention_days SET NOT NULL" in joined
    add = [s for s in executed if "ADD CONSTRAINT ck_tenant_settings_retention_days" in s]
    assert len(add) == 1 and "BETWEEN 1 AND 730" in add[0]
    assert not any(s.upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in executed)


def test_downgrade_refuses_while_a_row_holds_null_or_more_than_730_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old schema cannot hold NULL: turning 'keep everything' into 90 days would be a silent deletion period."""
    executed: list[str] = []
    module = _load()
    conn = _FakeConn(unrepresentable=1)
    monkeypatch.setattr(
        module, "op",
        SimpleNamespace(get_bind=lambda: conn, execute=lambda stmt, *a, **k: executed.append(str(stmt))),
    )
    with pytest.raises(RuntimeError, match="NULL"):
        module.downgrade()
    assert executed == []
