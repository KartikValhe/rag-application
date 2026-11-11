"""
Add timetables and timetable_entries tables with RLS and tenant timezone.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add timezone column to tenants table
    op.execute("""
        ALTER TABLE app.tenants
        ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'
    """)

    # 2. Create timetables table
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.timetables (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            class_id UUID NOT NULL,
            academic_year VARCHAR(50),
            term VARCHAR(50),
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT timetables_status_check CHECK (status IN ('draft', 'published')),
            CONSTRAINT fk_timetables_class FOREIGN KEY (tenant_id, class_id)
                REFERENCES app.classes(tenant_id, id) ON DELETE CASCADE
        )
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.timetables TO app_tenant")
    op.execute("GRANT SELECT, INSERT ON app.timetables TO app_platform")
    op.execute("ALTER TABLE app.timetables ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.timetables FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.timetables
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # 3. Create timetable_entries table
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.timetable_entries (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            timetable_id UUID NOT NULL,
            class_id UUID NOT NULL,
            subject_id UUID NOT NULL,
            teacher_id UUID,
            day_of_week INTEGER NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            room VARCHAR(100),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT timetable_entries_day_check CHECK (day_of_week >= 0 AND day_of_week <= 6),
            CONSTRAINT timetable_entries_time_check CHECK (start_time < end_time),
            CONSTRAINT fk_timetable_entries_timetable FOREIGN KEY (tenant_id, timetable_id)
                REFERENCES app.timetables(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_timetable_entries_class FOREIGN KEY (tenant_id, class_id)
                REFERENCES app.classes(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_timetable_entries_subject FOREIGN KEY (tenant_id, subject_id)
                REFERENCES app.subjects(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_timetable_entries_teacher FOREIGN KEY (tenant_id, teacher_id)
                REFERENCES app.users(tenant_id, id) ON DELETE SET NULL
        )
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.timetable_entries TO app_tenant")
    op.execute("GRANT SELECT, INSERT ON app.timetable_entries TO app_platform")
    op.execute("ALTER TABLE app.timetable_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.timetable_entries FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.timetable_entries
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # 4. Indexes for high performance lookups
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetables_lookup
        ON app.timetables (tenant_id, class_id, status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetable_entries_lookup
        ON app.timetable_entries (tenant_id, timetable_id, day_of_week, start_time)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetable_entries_teacher
        ON app.timetable_entries (tenant_id, teacher_id, day_of_week)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetable_entries_room
        ON app.timetable_entries (tenant_id, room, day_of_week)
    """)

    # 5. Permission catalog and seeding for timetable permissions
    op.execute("""
        INSERT INTO app.permissions (id, category, description)
        VALUES 
            ('timetable:manage', 'Timetable Management', 'Create, edit, and delete class draft timetables and entries'),
            ('timetable:publish', 'Timetable Management', 'Publish and unpublish class timetables'),
            ('timetable:view', 'Timetable Management', 'View published class timetables and schedules')
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO app.role_permissions (tenant_id, role_id, permission_id)
        SELECT r.tenant_id, r.id, p.id
        FROM app.roles r
        CROSS JOIN (
            VALUES ('timetable:manage'), ('timetable:publish'), ('timetable:view')
        ) AS p(id)
        WHERE (r.name = 'tenant_admin')
           OR (r.name = 'teacher')
           OR (r.name = 'student' AND p.id = 'timetable:view')
        ON CONFLICT (tenant_id, role_id, permission_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.timetable_entries CASCADE")
    op.execute("DROP TABLE IF EXISTS app.timetables CASCADE")
    op.execute("ALTER TABLE app.tenants DROP COLUMN IF EXISTS timezone")
    op.execute("DELETE FROM app.permissions WHERE id IN ('timetable:manage', 'timetable:publish', 'timetable:view')")

