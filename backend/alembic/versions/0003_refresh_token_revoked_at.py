"""
Add revoked_at to refresh_tokens for token reuse detection and grace window.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.refresh_tokens ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE app.refresh_tokens DROP COLUMN IF EXISTS revoked_at")

