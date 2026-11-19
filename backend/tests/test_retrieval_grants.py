"""
tests/test_retrieval_grants.py
──────────────────────────────
Step 5d adds NO grant (plan F8). This pins the exact privileges on the two tables retrieval reads, so a
future migration that widens them fails here:

  app_tenant    SELECT, INSERT, UPDATE, DELETE (the API reads; the index stage writes as app_tenant)
  app_worker    nothing
  app_platform  nothing
  PUBLIC        nothing

and RLS stays ENABLE + FORCE on both.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TABLES = ("chunks", "chunk_embeddings")
EXPECTED = {
    "app_tenant": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "app_worker": set(),
    "app_platform": set(),
    "PUBLIC": set(),
}


async def test_exact_table_privileges_on_chunks_and_embeddings(raw_db: AsyncSession) -> None:
    rows = (
        await raw_db.execute(
            text(
                "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'app' AND table_name = ANY(:t)"
            ),
            {"t": list(TABLES)},
        )
    ).all()
    for table in TABLES:
        for role, expected in EXPECTED.items():
            got = {r[2] for r in rows if r[0] == role and r[1] == table}
            assert got == expected, f"{role} on app.{table}: {sorted(got)}, expected {sorted(expected)}"


async def test_only_the_owner_and_app_tenant_hold_any_privilege(raw_db: AsyncSession) -> None:
    """Read from the ACLs themselves (pg_class.relacl, pg_attribute.attacl), not from information_schema,
    which only shows roles the current user is a member of: EVERY grantee of a table or column privilege on
    the two tables must be the table owner or app_tenant. A grant to any other role (or PUBLIC) fails here,
    whatever its name."""
    rows = (
        await raw_db.execute(
            text(
                "SELECT c.relname, pg_get_userbyid(c.relowner), "
                "       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, 'table' "
                "FROM pg_class c, aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
                "WHERE c.relnamespace = 'app'::regnamespace AND c.relname = ANY(:t) "
                "UNION ALL "
                "SELECT c.relname, pg_get_userbyid(c.relowner), "
                "       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, att.attname "
                "FROM pg_class c JOIN pg_attribute att ON att.attrelid = c.oid AND att.attacl IS NOT NULL, "
                "     aclexplode(att.attacl) a "
                "WHERE c.relnamespace = 'app'::regnamespace AND c.relname = ANY(:t)"
            ),
            {"t": list(TABLES)},
        )
    ).all()
    assert {r[0] for r in rows} == set(TABLES), "both tables were found"
    for table, owner, grantee, where in rows:
        assert grantee in {owner, "app_tenant"}, f"{grantee} holds a privilege on app.{table} ({where})"


async def test_no_column_grants_on_chunks_and_embeddings(raw_db: AsyncSession) -> None:
    rows = (
        await raw_db.execute(
            text(
                "SELECT grantee, table_name, column_name, privilege_type FROM information_schema.column_privileges "
                "WHERE table_schema = 'app' AND table_name = ANY(:t) AND grantee = ANY(:roles)"
            ),
            {"t": list(TABLES), "roles": ["app_worker", "app_platform", "PUBLIC"]},
        )
    ).all()
    assert rows == [], f"unexpected column grants: {rows}"


async def test_rls_is_enabled_and_forced(raw_db: AsyncSession) -> None:
    rows = (
        await raw_db.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'app'::regnamespace AND relname = ANY(:t)"
            ),
            {"t": list(TABLES)},
        )
    ).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {t: (True, True) for t in TABLES}
