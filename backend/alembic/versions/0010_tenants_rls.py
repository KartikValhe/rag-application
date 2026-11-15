"""
Row level security and column grants on app.tenants (design A3, backlog B14).

Until now app.tenants had no RLS, and app_tenant could read every column of every tenant,
including db_dsn. This migration changes that:

  1. ENABLE + FORCE ROW LEVEL SECURITY on app.tenants, with one policy set per role:
       app_tenant   tenant_self_select (SELECT) and tenant_self_update (UPDATE): its own row only,
                    id = app.tenant_id. NO insert and NO delete policy.
       app_platform platform_all (ALL, USING true): the platform lists and manages every tenant.
       app_worker   worker_select (SELECT, USING true): the sweeper lists tenant ids.
  2. Column grants, so a row the role may see still shows only the columns it needs:
       app_tenant   SELECT (id, slug, status, timezone) and UPDATE (timezone) (the UPDATE grant is
                    from 0006). It no longer has table-wide SELECT, so db_dsn, doc_limits, name,
                    kind, registration_mode and created_at are unreadable (SQLSTATE 42501).
       app_platform SELECT on the whole table (unchanged), INSERT on every column except id, and
                    UPDATE on every column except id, slug and created_at. The identity of a
                    tenant (its id and its subdomain) can no longer change, and no id can be
                    chosen at insert time. There is no trigger for this: column grants only.
       app_worker   SELECT (id), unchanged (0008). Its policy only makes the rows visible.
     app_tenant gets no grant on doc_limits here: the 5b document API adds it with its first user.
  3. On Supabase, REVOKE ALL on the table from anon and authenticated (same DO block as 0008).

Child tables that reference tenants (users, documents, ...) keep working for app_tenant: foreign
key checks are made by the system and are not subject to row security or to the caller's column
grants (a test covers it).

Public pre-auth lookups (login, refresh, register, public branding) stay on the platform pool.

The migration role must be a superuser or hold BYPASSRLS (the same check as 0009): after this
migration the owner is subject to FORCE ROW LEVEL SECURITY like everybody else.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ID_PREDICATE = "id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

TENANT_SELECT_COLUMNS = ("id", "slug", "status", "timezone")
PLATFORM_INSERT_COLUMNS = (
    "slug", "name", "kind", "registration_mode", "status", "db_dsn", "timezone", "doc_limits", "created_at",
)
PLATFORM_UPDATE_COLUMNS = (
    "name", "kind", "registration_mode", "status", "db_dsn", "timezone", "doc_limits",
)

POLICIES = ("tenant_self_select", "tenant_self_update", "platform_all", "worker_select")


def _require_rls_bypass() -> None:
    """Fail clearly if this role cannot see rows of tables with FORCE ROW LEVEL SECURITY."""
    conn = op.get_bind()
    ok = conn.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar()
    if not ok:
        raise RuntimeError(
            "This migration must run as a role that is a superuser or has BYPASSRLS "
            "(use MIGRATION_DATABASE_URL, not the app_tenant, app_platform or app_worker role). "
            "The tables it changes have FORCE ROW LEVEL SECURITY, so any other role sees 0 rows "
            "in them: the data fix and the constraint checks would silently skip every existing "
            "row. Run it again with a privileged role."
        )


def _require_role(role: str) -> None:
    conn = op.get_bind()
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
        raise RuntimeError(
            f"Migration 0010 needs the database role '{role}' (created by infra/postgres/init.sql "
            "and checked by migration 0008), but it does not exist."
        )


def upgrade() -> None:
    _require_rls_bypass()
    for role in ("app_tenant", "app_platform", "app_worker"):
        _require_role(role)

    op.execute("ALTER TABLE app.tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.tenants FORCE ROW LEVEL SECURITY")

    # app_tenant: its own row, read and update; no insert or delete policy exists for it.
    op.execute(f"""
        CREATE POLICY tenant_self_select ON app.tenants
            AS PERMISSIVE FOR SELECT TO app_tenant
            USING ({TENANT_ID_PREDICATE})
    """)
    op.execute(f"""
        CREATE POLICY tenant_self_update ON app.tenants
            AS PERMISSIVE FOR UPDATE TO app_tenant
            USING      ({TENANT_ID_PREDICATE})
            WITH CHECK ({TENANT_ID_PREDICATE})
    """)
    # app_platform manages every tenant; app_worker only lists them (column grant: id).
    op.execute("""
        CREATE POLICY platform_all ON app.tenants
            AS PERMISSIVE FOR ALL TO app_platform
            USING (true) WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY worker_select ON app.tenants
            AS PERMISSIVE FOR SELECT TO app_worker
            USING (true)
    """)

    # app_tenant: drop table-wide SELECT, keep only the columns it needs. UPDATE (timezone), from
    # 0006, is left as it is.
    op.execute("REVOKE SELECT ON app.tenants FROM app_tenant")
    op.execute(f"GRANT SELECT ({', '.join(TENANT_SELECT_COLUMNS)}) ON app.tenants TO app_tenant")

    # app_platform: SELECT stays table-wide; INSERT and UPDATE become column grants.
    op.execute("REVOKE INSERT, UPDATE ON app.tenants FROM app_platform")
    op.execute(f"GRANT INSERT ({', '.join(PLATFORM_INSERT_COLUMNS)}) ON app.tenants TO app_platform")
    op.execute(f"GRANT UPDATE ({', '.join(PLATFORM_UPDATE_COLUMNS)}) ON app.tenants TO app_platform")

    # On Supabase these API roles exist and must never touch this table.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON app.tenants FROM anon;
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON app.tenants FROM authenticated;
            END IF;
        END $$
    """)


def downgrade() -> None:
    # Back to the grants of 0009 exactly: app_tenant table-wide SELECT (plus UPDATE (timezone)),
    # app_platform table-wide SELECT, INSERT and UPDATE, app_worker SELECT (id), no RLS.
    # (The REVOKE from anon and authenticated is not undone: they never had a legitimate grant.)
    _require_rls_bypass()

    op.execute(f"REVOKE INSERT ({', '.join(PLATFORM_INSERT_COLUMNS)}) ON app.tenants FROM app_platform")
    op.execute(f"REVOKE UPDATE ({', '.join(PLATFORM_UPDATE_COLUMNS)}) ON app.tenants FROM app_platform")
    op.execute("GRANT INSERT, UPDATE ON app.tenants TO app_platform")

    op.execute(f"REVOKE SELECT ({', '.join(TENANT_SELECT_COLUMNS)}) ON app.tenants FROM app_tenant")
    op.execute("GRANT SELECT ON app.tenants TO app_tenant")

    for policy in reversed(POLICIES):
        op.execute(f"DROP POLICY {policy} ON app.tenants")
    op.execute("ALTER TABLE app.tenants NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.tenants DISABLE ROW LEVEL SECURITY")
