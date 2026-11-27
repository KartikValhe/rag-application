"""
Phase 6 step 6a-12b: chat retention is optional per tenant (docs/phase6-plan.md 9.1, 9.4; owner decisions Q4 and C35).

  tenant_settings.chat_retention_days   was INTEGER NOT NULL DEFAULT 90 CHECK 1 to 730
                                        is  INTEGER NULL, NO default, CHECK NULL or 30 to 3650

NULL means "never delete". A tenant admin must set a period on purpose (whole days, 30 to 3650); new tenants and tenants
with no row therefore keep everything. The retention worker does nothing for a NULL.

THIS MIGRATION NEVER CHANGES A ROW. A row that holds the old default of 90 stays 90: the migration cannot tell a 90 that
nobody chose from a 90 an admin chose, and the owner decides what happens to those rows (see the 6a-12b record in
docs/phase6-plan.md for the query that lists them). Until the owner decides, a stored 90 is a real period to the worker.

It refuses to run (and changes nothing) when a row holds fewer than 30 days: the 6a-12a route accepted 1 to 729, and a
silent rewrite of such a value would be a decision for the owner as well. app.tenant_settings has FORCE RLS, so the
migration role must bypass it (MIGRATION_DATABASE_URL), or the check would see no row.

Downgrade restores NOT NULL DEFAULT 90 and CHECK 1 to 730, but refuses while any row holds NULL or more than 730 days:
turning "keep everything" into 90 days would make the old schema delete data.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.models.tenant_settings import CHAT_RETENTION_DAYS_MAX, CHAT_RETENTION_DAYS_MIN

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_tenant_settings_retention_days"

# The 0012 schema, for the downgrade.
OLD_DEFAULT = 90
OLD_MIN = 1
OLD_MAX = 730


def _require_rls_bypass() -> None:
    conn = op.get_bind()
    ok = conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar()
    if not ok:
        raise RuntimeError(
            "Migration 0014 must run as a role that is a superuser or has BYPASSRLS (use MIGRATION_DATABASE_URL). "
            "app.tenant_settings has FORCE ROW LEVEL SECURITY, so any other role sees no row and the check of the "
            "stored retention values would pass without looking."
        )


def upgrade() -> None:
    _require_rls_bypass()
    conn = op.get_bind()
    too_short = conn.execute(
        text(f"SELECT count(*) FROM app.tenant_settings WHERE chat_retention_days < {CHAT_RETENTION_DAYS_MIN}")
    ).scalar()
    if too_short:
        raise RuntimeError(
            f"Migration 0014 stops: {too_short} tenant_settings row(s) hold a chat_retention_days below "
            f"{CHAT_RETENTION_DAYS_MIN}, which the new rule (NULL or {CHAT_RETENTION_DAYS_MIN} to {CHAT_RETENTION_DAYS_MAX}) "
            "does not allow. Nothing was changed. The owner decides what those rows should hold (set them to NULL, or to a "
            "value from 30 up) before this migration runs."
        )
    op.execute("ALTER TABLE app.tenant_settings ALTER COLUMN chat_retention_days DROP NOT NULL")
    op.execute("ALTER TABLE app.tenant_settings ALTER COLUMN chat_retention_days DROP DEFAULT")
    op.execute(f"ALTER TABLE app.tenant_settings DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE app.tenant_settings ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK (chat_retention_days IS NULL OR chat_retention_days BETWEEN {CHAT_RETENTION_DAYS_MIN} AND {CHAT_RETENTION_DAYS_MAX})"
    )


def downgrade() -> None:
    _require_rls_bypass()
    conn = op.get_bind()
    unrepresentable = conn.execute(
        text(
            "SELECT count(*) FROM app.tenant_settings "
            f"WHERE chat_retention_days IS NULL OR chat_retention_days > {OLD_MAX}"
        )
    ).scalar()
    if unrepresentable:
        raise RuntimeError(
            f"Migration 0014 cannot be undone: {unrepresentable} tenant_settings row(s) hold NULL (keep everything) or "
            f"more than {OLD_MAX} days, which the old column cannot hold. Nothing was changed. Turning NULL into "
            f"{OLD_DEFAULT} days would make the old schema delete data; the owner decides what those rows hold first."
        )
    op.execute(f"ALTER TABLE app.tenant_settings DROP CONSTRAINT {CONSTRAINT}")
    op.execute(f"ALTER TABLE app.tenant_settings ALTER COLUMN chat_retention_days SET DEFAULT {OLD_DEFAULT}")
    op.execute("ALTER TABLE app.tenant_settings ALTER COLUMN chat_retention_days SET NOT NULL")
    op.execute(
        f"ALTER TABLE app.tenant_settings ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK (chat_retention_days BETWEEN {OLD_MIN} AND {OLD_MAX})"
    )
