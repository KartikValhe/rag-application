"""
Phase 5 hardening after the security review of step 5a-2 (changes to the tables of 0008).

  1. ingest_jobs policies: the app_worker role gets one policy per command (read all, update
     unfinished jobs, delete finished jobs) instead of FOR ALL, and tenant_isolation applies to
     app_tenant only, so the worker cannot widen its access by setting app.tenant_id itself.
  2. app_tenant may update only the retry columns of ingest_jobs (lease and worker id are
     worker-only).
  3. documents -> subjects is ON DELETE RESTRICT (it used to cascade away documents, versions and
     chunks and orphan the files).
  4. storage_deletions -> tenants is ON DELETE RESTRICT (the outbox must not cascade away).
  5. storage_key must start with 't/<tenant_id>/' on document_versions and storage_deletions.
  6. The same bytes are unique per document only among active versions (partial unique index),
     so a failed upload or a rollback to older bytes can be uploaded again.
  7. Composite FKs for the actor columns (uploaded_by, published_by, deleted_by,
     flags_acknowledged_by): RESTRICT for the NOT NULL column, ON DELETE SET NULL (column) for
     the nullable ones (PostgreSQL 15+).
  8. CHECKs: chunks.script and "published implies published_at".

The migration role must be a superuser or hold BYPASSRLS: the data fix and the constraint
validation read tables with FORCE ROW LEVEL SECURITY, and any other role would see 0 rows there
and skip them silently. The migration checks this first and fails with a clear message.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.models.document import (
    ACTIVE_JOB_STATUSES,
    CHUNK_SCRIPTS,
    FINISHED_JOB_STATUSES,
    INACTIVE_VERSION_STATUSES,
    INGEST_JOB_API_UPDATE_COLUMNS,
    INGEST_JOB_STATUSES,
    VERSION_STATUS_PUBLISHED,
    sql_in_list,
)

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STORAGE_KEY_PREFIX_CHECK = "storage_key LIKE 't/' || tenant_id::text || '/%'"


def _require_rls_bypass() -> None:
    """Fail clearly if this role cannot see rows of tables with FORCE ROW LEVEL SECURITY."""
    conn = op.get_bind()
    ok = conn.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar()
    if not ok:
        raise RuntimeError(
            "This migration must run as a role that is a superuser or has BYPASSRLS "
            "(use MIGRATION_DATABASE_URL, not the app_tenant, app_platform or app_worker role). "
            "The tables it changes have FORCE ROW LEVEL SECURITY, so any other role sees 0 rows "
            "in them: the data fix and the constraint checks would silently skip every existing "
            "row. Run it again with a privileged role."
        )


def _require_role(role: str) -> None:
    conn = op.get_bind()
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
        raise RuntimeError(
            f"Migration 0009 needs the database role '{role}' (created by infra/postgres/init.sql "
            "and checked by migration 0008), but it does not exist."
        )


def _require_postgres_15() -> None:
    conn = op.get_bind()
    version = int(conn.execute(text("SELECT current_setting('server_version_num')")).scalar() or 0)
    if version < 150000:
        raise RuntimeError(
            "Migration 0009 uses ON DELETE SET NULL (column), which needs PostgreSQL 15 or newer."
        )


def upgrade() -> None:
    _require_rls_bypass()
    _require_role("app_worker")
    _require_postgres_15()

    # Data fix first, so the new CHECK can be added: a published version without a timestamp
    # gets its last update time (the best record there is).
    op.execute(f"""
        UPDATE app.document_versions
        SET published_at = COALESCE(updated_at, created_at)
        WHERE status = '{VERSION_STATUS_PUBLISHED}' AND published_at IS NULL
    """)

    # 1. ingest_jobs: tenant_isolation for app_tenant only; one worker policy per command.
    op.execute("ALTER POLICY tenant_isolation ON app.ingest_jobs TO app_tenant")
    op.execute("DROP POLICY worker_claim ON app.ingest_jobs")
    op.execute("""
        CREATE POLICY worker_select ON app.ingest_jobs
            AS PERMISSIVE FOR SELECT TO app_worker
            USING (true)
    """)
    op.execute(f"""
        CREATE POLICY worker_update ON app.ingest_jobs
            AS PERMISSIVE FOR UPDATE TO app_worker
            USING      (status IN ({sql_in_list(ACTIVE_JOB_STATUSES)}))
            WITH CHECK (status IN ({sql_in_list(INGEST_JOB_STATUSES)}))
    """)
    op.execute(f"""
        CREATE POLICY worker_delete ON app.ingest_jobs
            AS PERMISSIVE FOR DELETE TO app_worker
            USING (status IN ({sql_in_list(FINISHED_JOB_STATUSES)}))
    """)

    # 2. app_tenant: only the columns of the API retry path (design A3).
    op.execute("REVOKE UPDATE ON app.ingest_jobs FROM app_tenant")
    api_cols = ", ".join(INGEST_JOB_API_UPDATE_COLUMNS)
    op.execute(f"GRANT UPDATE ({api_cols}) ON app.ingest_jobs TO app_tenant")

    # 3. A subject with documents cannot be deleted.
    op.execute("ALTER TABLE app.documents DROP CONSTRAINT fk_documents_subject")
    op.execute("""
        ALTER TABLE app.documents ADD CONSTRAINT fk_documents_subject
            FOREIGN KEY (tenant_id, subject_id) REFERENCES app.subjects(tenant_id, id)
            ON DELETE RESTRICT
    """)

    # 4. The outbox does not cascade away with its tenant.
    op.execute("ALTER TABLE app.storage_deletions DROP CONSTRAINT storage_deletions_tenant_id_fkey")
    op.execute("""
        ALTER TABLE app.storage_deletions ADD CONSTRAINT storage_deletions_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES app.tenants(id) ON DELETE RESTRICT
    """)

    # 5. Storage keys live under the tenant's own prefix.
    op.execute(f"""
        ALTER TABLE app.document_versions
        ADD CONSTRAINT ck_document_versions_storage_key_prefix CHECK ({STORAGE_KEY_PREFIX_CHECK})
    """)
    op.execute(f"""
        ALTER TABLE app.storage_deletions
        ADD CONSTRAINT ck_storage_deletions_storage_key_prefix CHECK ({STORAGE_KEY_PREFIX_CHECK})
    """)

    # 6. Same bytes: unique among active versions only.
    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT uq_document_versions_sha256")
    op.execute(f"""
        CREATE UNIQUE INDEX uq_document_versions_active_sha256
        ON app.document_versions (tenant_id, document_id, sha256)
        WHERE status NOT IN ({sql_in_list(INACTIVE_VERSION_STATUSES)})
    """)

    # 7. Actor columns reference same-tenant users.
    op.execute("""
        ALTER TABLE app.document_versions
            ADD CONSTRAINT fk_document_versions_uploaded_by
                FOREIGN KEY (tenant_id, uploaded_by) REFERENCES app.users(tenant_id, id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_document_versions_published_by
                FOREIGN KEY (tenant_id, published_by) REFERENCES app.users(tenant_id, id)
                ON DELETE SET NULL (published_by),
            ADD CONSTRAINT fk_document_versions_flags_acknowledged_by
                FOREIGN KEY (tenant_id, flags_acknowledged_by) REFERENCES app.users(tenant_id, id)
                ON DELETE SET NULL (flags_acknowledged_by)
    """)
    op.execute("""
        ALTER TABLE app.documents
            ADD CONSTRAINT fk_documents_deleted_by
                FOREIGN KEY (tenant_id, deleted_by) REFERENCES app.users(tenant_id, id)
                ON DELETE SET NULL (deleted_by)
    """)

    # 8. Value checks.
    op.execute(f"""
        ALTER TABLE app.chunks
        ADD CONSTRAINT chunks_script_check CHECK (script IN ({sql_in_list(CHUNK_SCRIPTS)}))
    """)
    op.execute(f"""
        ALTER TABLE app.document_versions
        ADD CONSTRAINT document_versions_published_at_check
        CHECK (status <> '{VERSION_STATUS_PUBLISHED}' OR published_at IS NOT NULL)
    """)


def downgrade() -> None:
    # Back to the schema of 0008, step by step and in the reverse order. No DROP ... CASCADE.
    _require_rls_bypass()

    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT document_versions_published_at_check")
    op.execute("ALTER TABLE app.chunks DROP CONSTRAINT chunks_script_check")

    op.execute("ALTER TABLE app.documents DROP CONSTRAINT fk_documents_deleted_by")
    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT fk_document_versions_flags_acknowledged_by")
    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT fk_document_versions_published_by")
    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT fk_document_versions_uploaded_by")

    # The old UNIQUE needs one row per (document, bytes): refuse if re-uploads now exist.
    conn = op.get_bind()
    duplicate = conn.execute(
        text(
            "SELECT 1 FROM app.document_versions GROUP BY tenant_id, document_id, sha256 "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar()
    if duplicate:
        raise RuntimeError(
            "Cannot downgrade 0009: some document has several versions with the same bytes "
            "(a re-upload after a failed or archived version). The old UNIQUE (tenant_id, "
            "document_id, sha256) cannot hold them. Delete the duplicate versions first."
        )
    op.execute("DROP INDEX app.uq_document_versions_active_sha256")
    op.execute("""
        ALTER TABLE app.document_versions
        ADD CONSTRAINT uq_document_versions_sha256 UNIQUE (tenant_id, document_id, sha256)
    """)

    op.execute("ALTER TABLE app.storage_deletions DROP CONSTRAINT ck_storage_deletions_storage_key_prefix")
    op.execute("ALTER TABLE app.document_versions DROP CONSTRAINT ck_document_versions_storage_key_prefix")

    op.execute("ALTER TABLE app.storage_deletions DROP CONSTRAINT storage_deletions_tenant_id_fkey")
    op.execute("""
        ALTER TABLE app.storage_deletions ADD CONSTRAINT storage_deletions_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES app.tenants(id) ON DELETE CASCADE
    """)

    op.execute("ALTER TABLE app.documents DROP CONSTRAINT fk_documents_subject")
    op.execute("""
        ALTER TABLE app.documents ADD CONSTRAINT fk_documents_subject
            FOREIGN KEY (tenant_id, subject_id) REFERENCES app.subjects(tenant_id, id)
            ON DELETE CASCADE
    """)

    # ingest_jobs: table-wide UPDATE for app_tenant, FOR ALL worker policy, PUBLIC isolation.
    op.execute("REVOKE UPDATE ON app.ingest_jobs FROM app_tenant")
    op.execute("GRANT UPDATE ON app.ingest_jobs TO app_tenant")
    op.execute("DROP POLICY worker_delete ON app.ingest_jobs")
    op.execute("DROP POLICY worker_update ON app.ingest_jobs")
    op.execute("DROP POLICY worker_select ON app.ingest_jobs")
    op.execute("""
        CREATE POLICY worker_claim ON app.ingest_jobs
            AS PERMISSIVE FOR ALL TO app_worker
            USING (true) WITH CHECK (true)
    """)
    op.execute("ALTER POLICY tenant_isolation ON app.ingest_jobs TO PUBLIC")
    # The published_at values written by the upgrade are data and stay.
