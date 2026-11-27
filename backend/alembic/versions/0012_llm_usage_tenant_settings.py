"""
Phase 6 step 6a-5: tenant_settings and llm_usage_events (docs/phase6-plan.md 9.1, 9.2, 9.7 step 1).

  tenant_settings   one row per tenant, PK tenant_id (FK tenants CASCADE); updated_by has the composite FK
                    (tenant_id, updated_by) -> users ON DELETE SET NULL (updated_by).
  llm_usage_events  one row per LLM call, PK (tenant_id, id); numbers, names and codes only, no content of any kind.
                    user_id has the composite FK (tenant_id, user_id) -> users ON DELETE SET NULL (user_id);
                    message_id has NO FK on purpose (the cost record survives the conversation).

Both tables: RLS ENABLE + FORCE with the existing tenant_isolation predicate and no anon/authenticated access
(_secure_table, as in 0008), then the grants are NARROWED to plan 9.2:
  tenant_settings   app_tenant SELECT, INSERT, UPDATE (no DELETE)
  llm_usage_events  app_tenant SELECT, INSERT (no UPDATE, no DELETE)
app_platform and app_worker get nothing on either table. The chat_maint policy and the app_chat_maint grants on
llm_usage_events are NOT here: they belong to 0013, so this migration does not need that role.

Backfill: one default tenant_settings row per existing tenant. app.tenants has FORCE RLS since 0010, so the migration
role must be a superuser or hold BYPASSRLS, or the backfill would see no tenant and silently insert nothing; it is
checked first. Tenants created later get their row from the platform create path (app/api/v1/platform.py).

Downgrade drops both tables (their grants and policies go with them).

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.models.llm_usage import (
    MODEL_KEY_MAX,
    MODEL_KEY_PATTERN,
    OUTCOME_MAX,
    PROVIDER_MAX,
    PROVIDER_PATTERN,
    PURPOSE_MAX,
    USAGE_OUTCOMES,
    USAGE_PURPOSES,
)

# The retention column as THIS migration created it (NOT NULL DEFAULT 90, 1 to 730). Frozen here on purpose: migration
# 0014 (6a-12b, C35) changed the column and the model's constants moved with it.
CHAT_RETENTION_DAYS_DEFAULT = 90
CHAT_RETENTION_DAYS_MIN = 1
CHAT_RETENTION_DAYS_MAX = 730

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ("tenant_settings", "llm_usage_events")

TENANT_ISOLATION_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# One default row per tenant that has none (a test runs this exact statement).
BACKFILL_TENANT_SETTINGS_SQL = """
    INSERT INTO app.tenant_settings (tenant_id)
    SELECT id FROM app.tenants
    ON CONFLICT (tenant_id) DO NOTHING
"""


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _require_rls_bypass() -> None:
    """Fail clearly if this role cannot see every row of app.tenants (FORCE RLS since 0010)."""
    conn = op.get_bind()
    ok = conn.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar()
    if not ok:
        raise RuntimeError(
            "Migration 0012 must run as a role that is a superuser or has BYPASSRLS (use "
            "MIGRATION_DATABASE_URL). app.tenants has FORCE ROW LEVEL SECURITY, so any other role sees 0 tenants "
            "and the tenant_settings backfill would silently insert nothing."
        )


def _require_role(role: str) -> None:
    conn = op.get_bind()
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
        raise RuntimeError(
            f"Migration 0012 needs the database role '{role}' (created by infra/postgres/init.sql), "
            "but it does not exist."
        )


def _secure_table(table: str) -> None:
    """GRANT to app_tenant only, ENABLE + FORCE RLS and the tenant_isolation policy (the 0008 helper)."""
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO app_tenant")
    op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON app.{table}
            USING      ({TENANT_ISOLATION_PREDICATE})
            WITH CHECK ({TENANT_ISOLATION_PREDICATE})
    """)
    # On Supabase these API roles exist and must never touch these tables.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON app.{table} FROM anon;
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON app.{table} FROM authenticated;
            END IF;
        END $$
    """)


def upgrade() -> None:
    _require_rls_bypass()
    for role in ("app_tenant", "app_platform", "app_worker"):
        _require_role(role)

    # 1. tenant_settings: one row per tenant.
    op.execute(f"""
        CREATE TABLE app.tenant_settings (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            chat_enabled BOOLEAN NOT NULL DEFAULT true,
            chat_retention_days INTEGER NOT NULL DEFAULT {CHAT_RETENTION_DAYS_DEFAULT},
            allowed_llm_providers TEXT[],
            chat_daily_token_budget INTEGER,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by UUID,
            PRIMARY KEY (tenant_id),
            CONSTRAINT ck_tenant_settings_retention_days
                CHECK (chat_retention_days BETWEEN {CHAT_RETENTION_DAYS_MIN} AND {CHAT_RETENTION_DAYS_MAX}),
            CONSTRAINT ck_tenant_settings_token_budget
                CHECK (chat_daily_token_budget IS NULL OR chat_daily_token_budget > 0),
            CONSTRAINT fk_tenant_settings_updated_by FOREIGN KEY (tenant_id, updated_by)
                REFERENCES app.users(tenant_id, id) ON DELETE SET NULL (updated_by)
        )
    """)

    # 2. llm_usage_events: one row per LLM call, no content of any kind.
    op.execute(f"""
        CREATE TABLE app.llm_usage_events (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            user_id UUID,
            message_id UUID,
            purpose VARCHAR({PURPOSE_MAX}) NOT NULL,
            provider VARCHAR({PROVIDER_MAX}) NOT NULL,
            model_key VARCHAR({MODEL_KEY_MAX}) NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            estimated BOOLEAN NOT NULL,
            latency_ms INTEGER NOT NULL,
            outcome VARCHAR({OUTCOME_MAX}) NOT NULL,
            attempt SMALLINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT ck_llm_usage_events_purpose CHECK (purpose IN ({_sql_list(USAGE_PURPOSES)})),
            CONSTRAINT ck_llm_usage_events_outcome CHECK (outcome IN ({_sql_list(USAGE_OUTCOMES)})),
            CONSTRAINT ck_llm_usage_events_counts CHECK (
                prompt_tokens >= 0 AND completion_tokens >= 0 AND latency_ms >= 0 AND attempt >= 1
            ),
            CONSTRAINT ck_llm_usage_events_provider CHECK (provider ~ '{PROVIDER_PATTERN}'),
            CONSTRAINT ck_llm_usage_events_model_key CHECK (
                model_key ~ '{MODEL_KEY_PATTERN}' AND left(model_key, length(provider) + 1) = provider || ':'
            ),
            CONSTRAINT fk_llm_usage_events_user FOREIGN KEY (tenant_id, user_id)
                REFERENCES app.users(tenant_id, id) ON DELETE SET NULL (user_id)
        )
    """)
    op.execute("CREATE INDEX ix_llm_usage_events_tenant_created ON app.llm_usage_events (tenant_id, created_at)")

    # 3. RLS (ENABLE + FORCE, tenant_isolation), then narrow the helper's grants to plan 9.2.
    for table in NEW_TABLES:
        _secure_table(table)
    op.execute("REVOKE DELETE ON app.tenant_settings FROM app_tenant")
    op.execute("REVOKE UPDATE, DELETE ON app.llm_usage_events FROM app_tenant")
    # Nothing for the platform and the worker (they have no grant; this keeps it so if a default privilege exists).
    for table in NEW_TABLES:
        op.execute(f"REVOKE ALL ON app.{table} FROM app_platform, app_worker")

    # 4. Backfill: every existing tenant gets its default row.
    op.execute(BACKFILL_TENANT_SETTINGS_SQL)


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS app.{table}")
