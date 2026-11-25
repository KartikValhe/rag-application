"""
Assertions on the two tables of migration 0012 (step 6a-5, docs/phase6-plan.md 9.1, 9.2, 9.7): tenant_settings and
llm_usage_events. Shared by tests/test_phase6_schema.py and tests/security/test_prove_it_fails_usage.py, so the
prove-it run breaks a defence and checks that THESE helpers notice.

Every helper takes anything with an async `execute(text(...))` (an AsyncSession or an AsyncConnection) of the
superuser test connection, so it reads the catalog as it is inside the caller's transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

PHASE6_USAGE_TABLES = ("tenant_settings", "llm_usage_events")

# Plan 9.2: app_tenant only, narrower than _secure_table's default. Nobody else gets anything (app_platform,
# app_worker, PUBLIC). The chat maintenance role arrives in 0013, not here.
EXPECTED_TABLE_PRIVILEGES: dict[str, frozenset[tuple[str, str]]] = {
    "tenant_settings": frozenset({("app_tenant", "SELECT"), ("app_tenant", "INSERT"), ("app_tenant", "UPDATE")}),
    "llm_usage_events": frozenset({("app_tenant", "SELECT"), ("app_tenant", "INSERT")}),
}

ROLES = ("app_tenant", "app_platform", "app_worker")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

ISOLATION_PREDICATE = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
EXPECTED_POLICIES = frozenset(
    {("tenant_isolation", "ALL", "PERMISSIVE", ("public",), ISOLATION_PREDICATE, ISOLATION_PREDICATE)}
)


async def table_privileges(conn: Any, table: str) -> set[tuple[str, str]]:
    """(grantee, privilege) of every table-level grant on app.<table> except the owner's. PUBLIC shows as 'PUBLIC'."""
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


async def role_privilege_matrix(conn: Any, table: str) -> set[tuple[str, str]]:
    """(role, privilege) that each application role HAS (has_table_privilege also counts inherited grants)."""
    found: set[tuple[str, str]] = set()
    for role in ROLES:
        for privilege in PRIVILEGES:
            ok = (
                await conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": f"app.{table}", "p": privilege}
                )
            ).scalar_one()
            if ok:
                found.add((role, privilege))
    return found


async def column_grants(conn: Any, table: str) -> set[tuple[str, str, str]]:
    """Column-level grants (attacl), which a table-level check would not show."""
    rows = (
        await conn.execute(
            text("""
                SELECT a.attname, pg_get_userbyid(x.grantee), x.privilege_type
                FROM pg_attribute a, LATERAL aclexplode(a.attacl) x
                WHERE a.attrelid = to_regclass(:t) AND a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL
            """),
            {"t": f"app.{table}"},
        )
    ).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def assert_table_grants(conn: Any, table: str) -> None:
    """EXACTLY the plan's grants on the table, and nothing a role could inherit or get per column."""
    expected = EXPECTED_TABLE_PRIVILEGES[table]
    actual = await table_privileges(conn, table)
    assert actual == expected, f"{table}: grants differ. unexpected: {sorted(actual - expected)}; missing: {sorted(expected - actual)}"
    matrix = await role_privilege_matrix(conn, table)
    assert matrix == set(expected), f"{table}: effective role privileges differ: {sorted(matrix ^ set(expected))}"
    extra = await column_grants(conn, table)
    assert not extra, f"{table}: column-level grants exist: {sorted(extra)}"


async def assert_rls_forced(conn: Any, table: str) -> None:
    row = (
        await conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
            {"t": f"app.{table}"},
        )
    ).one_or_none()
    assert row is not None, f"app.{table} does not exist"
    assert row[0], f"{table}: row level security is not enabled"
    assert row[1], f"{table}: row level security is not forced"


async def assert_policies(conn: Any, table: str) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT policyname, cmd, permissive, roles::text[], qual, with_check "
                "FROM pg_policies WHERE schemaname = 'app' AND tablename = :t"
            ),
            {"t": table},
        )
    ).all()
    actual = {(r[0], r[1], r[2], tuple(sorted(r[3])), r[4], r[5]) for r in rows}
    assert actual == set(EXPECTED_POLICIES), f"{table}: policies differ: {sorted(actual ^ set(EXPECTED_POLICIES))}"


def assert_rows_belong_to(tenant_ids: Iterable[uuid.UUID], tenant_id: uuid.UUID, what: str) -> None:
    """Every row read in tenant `tenant_id`'s context belongs to that tenant."""
    foreign = sorted({str(t) for t in tenant_ids if t != tenant_id})
    assert not foreign, f"{what}: rows of other tenants are visible: {foreign}"
