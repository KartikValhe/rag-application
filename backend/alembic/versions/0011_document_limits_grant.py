"""
Let app_tenant read tenants.doc_limits (step 5b-1, design A2 "Access note (0010)").

The document API reads the tenant's own limits (file size, page and slide caps, quotas, upload rates)
through the tenant session. Migration 0010 left app_tenant with SELECT on id, slug, status and
timezone only. This adds ONE column grant:

    GRANT SELECT (doc_limits) ON app.tenants TO app_tenant

Rows stay limited by the tenant_self_select policy from 0010 (id = app.tenant_id), so a tenant reads
its own limits and never another tenant's. db_dsn, name, kind, registration_mode and created_at stay
unreadable, and app_tenant still cannot write doc_limits: only the superadmin endpoint
PATCH /api/v1/platform/tenants/{tenant_id}/limits does, through app_platform.

A GRANT or REVOKE needs the table owner or a superuser, so this runs with MIGRATION_DATABASE_URL like
the other migrations. The downgrade revokes exactly this column grant and so restores the 0010 grants.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_role(role: str) -> None:
    conn = op.get_bind()
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
        raise RuntimeError(
            f"Migration 0011 needs the database role '{role}' (created by infra/postgres/init.sql), "
            "but it does not exist."
        )


def upgrade() -> None:
    _require_role("app_tenant")
    op.execute("GRANT SELECT (doc_limits) ON app.tenants TO app_tenant")


def downgrade() -> None:
    _require_role("app_tenant")
    op.execute("REVOKE SELECT (doc_limits) ON app.tenants FROM app_tenant")
