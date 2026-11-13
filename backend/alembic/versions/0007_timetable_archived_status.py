"""
Timetable lifecycle: add the 'archived' status and allow at most one draft and one
published timetable per class.

Before the unique indexes are created, existing data is cleaned up. An older bug turned the
previous published timetable back into a draft on every publish, so a class can hold several
drafts. For each class the most recently updated draft (and published) row is kept and the
others are archived, which keeps them as read-only history.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.timetables DROP CONSTRAINT IF EXISTS timetables_status_check")
    op.execute("""
        ALTER TABLE app.timetables
        ADD CONSTRAINT timetables_status_check CHECK (status IN ('draft', 'published', 'archived'))
    """)

    # Archive every duplicate draft / published row, keeping the newest one per class.
    for st in ("draft", "published"):
        op.execute(f"""
            UPDATE app.timetables t
            SET status = 'archived'
            FROM (
                SELECT tenant_id, id,
                       row_number() OVER (
                           PARTITION BY tenant_id, class_id
                           ORDER BY updated_at DESC, version DESC, id
                       ) AS rn
                FROM app.timetables
                WHERE status = '{st}'
            ) ranked
            WHERE t.tenant_id = ranked.tenant_id AND t.id = ranked.id AND ranked.rn > 1
        """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_timetables_one_draft_per_class
        ON app.timetables (tenant_id, class_id) WHERE status = 'draft'
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_timetables_one_published_per_class
        ON app.timetables (tenant_id, class_id) WHERE status = 'published'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.uq_timetables_one_published_per_class")
    op.execute("DROP INDEX IF EXISTS app.uq_timetables_one_draft_per_class")
    # The old schema has no 'archived' status; history rows cannot be kept.
    op.execute("DELETE FROM app.timetables WHERE status = 'archived'")
    op.execute("ALTER TABLE app.timetables DROP CONSTRAINT IF EXISTS timetables_status_check")
    op.execute("""
        ALTER TABLE app.timetables
        ADD CONSTRAINT timetables_status_check CHECK (status IN ('draft', 'published'))
    """)
