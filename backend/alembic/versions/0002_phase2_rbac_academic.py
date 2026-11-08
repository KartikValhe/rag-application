"""
Phase 2 RBAC and academic structure schema.

Creates:
  - app.permissions (static catalog)
  - app.roles, app.role_permissions, app.user_roles (RBAC)
  - app.classes, app.subjects, app.class_subjects (Academic structure)
  - app.enrollments, app.teacher_assignments (Enrollments & Assignments)
  - app.audit_log (Append-only audit trail)
  - RLS policies and least-privilege grants on all tenant-scoped tables

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. Static permissions catalog ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.permissions (
            id VARCHAR(64) PRIMARY KEY,
            category VARCHAR(64) NOT NULL,
            description TEXT NOT NULL
        )
    """)
    op.execute("GRANT SELECT ON app.permissions TO app_tenant, app_platform")

    # Seed initial permission catalog rows
    op.execute("""
        INSERT INTO app.permissions (id, category, description) VALUES
            ('user:manage', 'User Management', 'Invite, deactivate, and manage tenant users'),
            ('role:manage', 'Role Management', 'Create, update, and assign custom roles'),
            ('class:manage', 'Academic Management', 'Create and manage classes and cohorts'),
            ('subject:manage', 'Academic Management', 'Create and manage subjects'),
            ('enrollment:manage', 'Academic Management', 'Enroll and unenroll students in classes'),
            ('assignment:manage', 'Academic Management', 'Assign and remove teachers from class subjects'),
            ('document:upload', 'Content Management', 'Upload course and syllabus documents'),
            ('document:delete', 'Content Management', 'Delete syllabus documents'),
            ('timetable:publish', 'Timetable Management', 'Create and publish class timetables'),
            ('chat:use', 'Student AI Chat', 'Chat with scoped RAG syllabus assistant'),
            ('analytics:view', 'Reporting', 'View system and engagement analytics'),
            ('branding:manage', 'Organization', 'Manage white-label theme and branding'),
            ('audit:view', 'Security', 'View tenant audit log history')
        ON CONFLICT (id) DO NOTHING
    """)

    # ── 2. Roles (tenant-scoped, RLS) ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.roles (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            name VARCHAR(64) NOT NULL,
            description TEXT,
            is_system BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, name)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.roles TO app_tenant")
    op.execute("ALTER TABLE app.roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.roles FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.roles
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 3. Role Permissions (tenant-scoped junction, RLS) ──────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.role_permissions (
            tenant_id UUID NOT NULL,
            role_id UUID NOT NULL,
            permission_id VARCHAR(64) NOT NULL REFERENCES app.permissions(id) ON DELETE CASCADE,
            PRIMARY KEY (tenant_id, role_id, permission_id),
            FOREIGN KEY (tenant_id, role_id) REFERENCES app.roles(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.role_permissions TO app_tenant")
    op.execute("ALTER TABLE app.role_permissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.role_permissions FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.role_permissions
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 4. User Roles (tenant-scoped junction, RLS) ───────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.user_roles (
            tenant_id UUID NOT NULL,
            user_id UUID NOT NULL,
            role_id UUID NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id, role_id),
            FOREIGN KEY (tenant_id, user_id) REFERENCES app.users(tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, role_id) REFERENCES app.roles(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.user_roles TO app_tenant")
    op.execute("ALTER TABLE app.user_roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.user_roles FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.user_roles
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 5. Classes (tenant-scoped, RLS) ────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.classes (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            name VARCHAR(100) NOT NULL,
            kind VARCHAR(50) NOT NULL DEFAULT 'section',
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, name)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.classes TO app_tenant")
    op.execute("ALTER TABLE app.classes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.classes FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.classes
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 6. Subjects (tenant-scoped, RLS) ───────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.subjects (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            name VARCHAR(100) NOT NULL,
            code VARCHAR(50),
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, name)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.subjects TO app_tenant")
    op.execute("ALTER TABLE app.subjects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.subjects FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.subjects
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 7. Class Subjects (tenant-scoped junction, RLS) ────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.class_subjects (
            tenant_id UUID NOT NULL,
            class_id UUID NOT NULL,
            subject_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, class_id, subject_id),
            FOREIGN KEY (tenant_id, class_id) REFERENCES app.classes(tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, subject_id) REFERENCES app.subjects(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.class_subjects TO app_tenant")
    op.execute("ALTER TABLE app.class_subjects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.class_subjects FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.class_subjects
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 8. Enrollments (tenant-scoped, RLS) ────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.enrollments (
            tenant_id UUID NOT NULL,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            class_id UUID NOT NULL,
            enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, user_id, class_id),
            FOREIGN KEY (tenant_id, user_id) REFERENCES app.users(tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, class_id) REFERENCES app.classes(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.enrollments TO app_tenant")
    op.execute("ALTER TABLE app.enrollments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.enrollments FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.enrollments
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 9. Teacher Assignments (tenant-scoped, RLS) ────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.teacher_assignments (
            tenant_id UUID NOT NULL,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            class_id UUID NOT NULL,
            subject_id UUID NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            UNIQUE (tenant_id, user_id, class_id, subject_id),
            FOREIGN KEY (tenant_id, user_id) REFERENCES app.users(tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, class_id, subject_id) REFERENCES app.class_subjects(tenant_id, class_id, subject_id) ON DELETE CASCADE
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.teacher_assignments TO app_tenant")
    op.execute("ALTER TABLE app.teacher_assignments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.teacher_assignments FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.teacher_assignments
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── 10. Audit Log (tenant-scoped, append-only, RLS) ────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.audit_log (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            actor_id UUID,
            action VARCHAR(100) NOT NULL,
            target_type VARCHAR(50) NOT NULL,
            target_id VARCHAR(100),
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            ip_address VARCHAR(45),
            request_id VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id)
        )
    """)
    # Append-only: SELECT and INSERT only. No UPDATE or DELETE granted to app_tenant.
    op.execute("GRANT SELECT, INSERT ON app.audit_log TO app_tenant")
    op.execute("ALTER TABLE app.audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.audit_log FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.audit_log
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS roles_tenant_idx ON app.roles (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS user_roles_user_idx ON app.user_roles (tenant_id, user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS classes_tenant_idx ON app.classes (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS subjects_tenant_idx ON app.subjects (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS enrollments_user_idx ON app.enrollments (tenant_id, user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS enrollments_class_idx ON app.enrollments (tenant_id, class_id)")
    op.execute("CREATE INDEX IF NOT EXISTS teacher_assignments_user_idx ON app.teacher_assignments (tenant_id, user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS audit_log_created_idx ON app.audit_log (tenant_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS app.teacher_assignments CASCADE")
    op.execute("DROP TABLE IF EXISTS app.enrollments CASCADE")
    op.execute("DROP TABLE IF EXISTS app.class_subjects CASCADE")
    op.execute("DROP TABLE IF EXISTS app.subjects CASCADE")
    op.execute("DROP TABLE IF EXISTS app.classes CASCADE")
    op.execute("DROP TABLE IF EXISTS app.user_roles CASCADE")
    op.execute("DROP TABLE IF EXISTS app.role_permissions CASCADE")
    op.execute("DROP TABLE IF EXISTS app.roles CASCADE")
    op.execute("DROP TABLE IF EXISTS app.permissions CASCADE")

