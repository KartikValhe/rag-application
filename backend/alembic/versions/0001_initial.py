"""
Phase 1 initial schema.

Creates:
  - app schema
  - Extensions: citext, pgcrypto (gen_random_uuid)
  - Custom DB roles: app_tenant, app_platform
  - Tables: tenants, platform_admins, users, refresh_tokens, invitations, email_tokens
  - RLS policies on all tenant-scoped tables
  - Indexes

Revision ID: 0001
Revises: —
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Schema and extensions ────────────────────────────────────────────────
    # These are idempotent; safe to run again if the schema was pre-created.
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA app")
    # pgcrypto provides gen_random_uuid() on older Postgres; not needed on PG14+
    # but harmless.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA app")

    # ── Custom DB roles ──────────────────────────────────────────────────────
    # In production: use strong passwords from a secrets manager.
    # In local dev: passwords come from infra/postgres/init.sql.
    # We use DO blocks so migrations are idempotent.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_tenant') THEN
                CREATE ROLE app_tenant LOGIN PASSWORD 'tenant_password'
                    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_platform') THEN
                CREATE ROLE app_platform LOGIN PASSWORD 'platform_password'
                    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA app TO app_tenant, app_platform")
    op.execute("ALTER ROLE app_tenant SET search_path = app")
    op.execute("ALTER ROLE app_platform SET search_path = app")

    # ── tenants (platform-level, NO RLS) ────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("registration_mode", sa.String(16), nullable=False, server_default="invite_only"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("db_dsn", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "registration_mode IN ('open', 'approval', 'invite_only')",
            name="tenants_registration_mode_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'archived')",
            name="tenants_status_check",
        ),
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$'",
            name="tenants_slug_format_check",
        ),
        schema="app",
    )
    # Only app_platform may read/write the tenants table.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.tenants TO app_platform")
    # app_tenant needs SELECT on tenants to validate tenant context.
    op.execute("GRANT SELECT ON app.tenants TO app_tenant")

    # ── platform_admins (platform-level, NO RLS) ─────────────────────────────
    op.create_table(
        "platform_admins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        schema="app",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.platform_admins TO app_platform")

    # ── users (tenant-scoped, RLS enabled) ───────────────────────────────────
    # email is app.citext for case-insensitive comparison.
    op.execute("""
        CREATE TABLE app.users (
            tenant_id   UUID        NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id          UUID        NOT NULL DEFAULT gen_random_uuid(),
            email       app.citext  NOT NULL,
            full_name   TEXT        NOT NULL,
            password_hash TEXT,
            is_admin    BOOLEAN     NOT NULL DEFAULT FALSE,
            status      TEXT        NOT NULL DEFAULT 'active',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, email),
            CONSTRAINT users_status_check CHECK (status IN ('pending', 'active', 'deactivated'))
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.users TO app_tenant")
    # Enable RLS — deny all by default.
    op.execute("ALTER TABLE app.users ENABLE ROW LEVEL SECURITY")
    # FORCE applies the policy even to the table owner.
    op.execute("ALTER TABLE app.users FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.users
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)
    op.execute("CREATE INDEX users_email_idx ON app.users (tenant_id, email)")
    op.execute("CREATE INDEX users_status_idx ON app.users (tenant_id, status)")

    # ── refresh_tokens (tenant-scoped, RLS) ──────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["app.tenants.id"], ondelete="CASCADE"),
        schema="app",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.refresh_tokens TO app_tenant")
    op.execute("ALTER TABLE app.refresh_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.refresh_tokens FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.refresh_tokens
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── invitations (tenant-scoped, RLS) ─────────────────────────────────────
    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("role_name", sa.Text, nullable=False, server_default="student"),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["app.tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'expired', 'cancelled')",
            name="invitations_status_check",
        ),
        schema="app",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.invitations TO app_tenant")
    op.execute("GRANT SELECT ON app.invitations TO app_platform")
    op.execute("ALTER TABLE app.invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.invitations FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.invitations
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY platform_read_invitations ON app.invitations
            FOR SELECT TO app_platform USING (true)
    """)

    # ── email_tokens (tenant-scoped, RLS) ────────────────────────────────────
    op.create_table(
        "email_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["app.tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name="email_tokens_purpose_check",
        ),
        schema="app",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.email_tokens TO app_tenant")
    op.execute("ALTER TABLE app.email_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.email_tokens FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.email_tokens
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.email_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS app.invitations CASCADE")
    op.execute("DROP TABLE IF EXISTS app.refresh_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS app.users CASCADE")
    op.execute("DROP TABLE IF EXISTS app.platform_admins CASCADE")
    op.execute("DROP TABLE IF EXISTS app.tenants CASCADE")
    op.execute("DROP EXTENSION IF EXISTS citext CASCADE")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")

