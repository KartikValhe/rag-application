"""
Add tenant_branding table with RLS and design tokens schema.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.tenant_branding (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            tokens JSONB NOT NULL,
            logo_key VARCHAR(512),
            favicon_key VARCHAR(512),
            display_name VARCHAR(255) NOT NULL DEFAULT '',
            tagline VARCHAR(255),
            footer_text VARCHAR(512),
            support_email VARCHAR(255),
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.tenant_branding TO app_tenant")
    op.execute("GRANT SELECT, INSERT ON app.tenant_branding TO app_platform")
    op.execute("ALTER TABLE app.tenant_branding ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.tenant_branding FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.tenant_branding
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # Seed default branding for existing tenants if any
    op.execute("""
        INSERT INTO app.tenant_branding (tenant_id, tokens, display_name)
        SELECT
            id,
            '{"primary": "#2563eb", "secondary": "#475569", "accent": "#06b6d4", "radius": "md", "font_family": "system-ui"}'::jsonb,
            name
        FROM app.tenants
        ON CONFLICT (tenant_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.tenant_branding CASCADE")

