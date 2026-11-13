"""
Add the settings:manage permission and let tenant sessions update the tenant timezone.

The timezone change and its audit row must commit in ONE transaction. The audit log is a
tenant table (RLS), so the update runs in the tenant session. app_tenant only gets UPDATE
on the single timezone column of app.tenants, and the route always filters by the caller's
own tenant id.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO app.permissions (id, category, description)
        VALUES ('settings:manage', 'Organization', 'Manage organization settings such as the timezone')
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO app.role_permissions (tenant_id, role_id, permission_id)
        SELECT r.tenant_id, r.id, 'settings:manage'
        FROM app.roles r
        WHERE r.name = 'tenant_admin'
        ON CONFLICT (tenant_id, role_id, permission_id) DO NOTHING
    """)
    op.execute("GRANT UPDATE (timezone) ON app.tenants TO app_tenant")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (timezone) ON app.tenants FROM app_tenant")
    op.execute("DELETE FROM app.role_permissions WHERE permission_id = 'settings:manage'")
    op.execute("DELETE FROM app.permissions WHERE id = 'settings:manage'")
