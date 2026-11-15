"""
Phase 5 documents: documents, document_classes, document_versions, chunks, chunk_embeddings,
storage_deletions, ingest_jobs, tenants.doc_limits, the app_worker grants and the new
document:view / document:publish permissions (design section A2 / A3 / A4).

Prerequisites (checked first, with a clear error; this migration creates neither):
  * the pgvector extension must exist in this database. The schema it lives in is read from
    pg_extension (locally it sits next to citext, on Supabase it is "extensions"), so no schema
    name is hardcoded here;
  * the app_worker role must exist (infra/postgres/init.sql creates it for local and CI).

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.models.document import (
    DOCUMENT_VERSION_STATUSES,
    FILE_KINDS,
    IN_FLIGHT_VERSION_STATUSES,
    INGEST_JOB_STATUSES,
    INGEST_JOB_WORKER_UPDATE_COLUMNS,
    INGEST_STAGES,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    VERSION_STATUS_PUBLISHED,
    sql_in_list,
)
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PERMISSIONS = (
    ("document:view", "Content Management", "View documents in scope, their status, flags and test search"),
    ("document:publish", "Content Management", "Publish document versions"),
)

# New tables, in dependency order (children last). All get the same RLS treatment.
NEW_TABLES = (
    "documents",
    "document_classes",
    "document_versions",
    "chunks",
    "chunk_embeddings",
    "storage_deletions",
    "ingest_jobs",
)

# Backfill of existing tenants' system roles (a test runs this exact statement).
BACKFILL_ROLE_PERMISSIONS_SQL = """
    INSERT INTO app.role_permissions (tenant_id, role_id, permission_id)
    SELECT r.tenant_id, r.id, p.id
    FROM app.roles r
    CROSS JOIN (VALUES ('document:view'), ('document:publish')) AS p(id)
    WHERE r.is_system AND r.name IN ('tenant_admin', 'teacher')
    ON CONFLICT (tenant_id, role_id, permission_id) DO NOTHING
"""

TENANT_ISOLATION_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
HNSW_INDEX_NAME = "ix_chunk_embeddings_hnsw_e5_small"


def _vector_schema() -> str:
    """Return the quoted schema of the pgvector extension, or fail with a clear message."""
    conn = op.get_bind()
    schema = conn.execute(
        text(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
        )
    ).scalar()
    if schema is None:
        raise RuntimeError(
            "Migration 0008 needs the pgvector extension, but it is not installed in this "
            "database. Install it first as a superuser, in the same schema as citext, for "
            "example: CREATE EXTENSION vector WITH SCHEMA <schema of citext>; "
            "(infra/postgres/init.sql does this for local and CI databases; on Supabase use "
            "the extensions schema), then run the migration again."
        )
    return conn.dialect.identifier_preparer.quote_schema(schema)


def _require_role(role: str) -> None:
    conn = op.get_bind()
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    if not exists:
        raise RuntimeError(
            f"Migration 0008 needs the database role '{role}', but it does not exist. "
            "Create it first (see infra/postgres/init.sql: LOGIN NOSUPERUSER NOBYPASSRLS with "
            "your own password) and run the migration again."
        )


def _secure_table(table: str) -> None:
    """GRANT to app_tenant only, ENABLE + FORCE RLS and the tenant_isolation policy."""
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
    vs = _vector_schema()
    _require_role("app_worker")

    # 1. Per-tenant limits (same pattern as tenants.timezone in 0005).
    op.execute("""
        ALTER TABLE app.tenants
        ADD COLUMN IF NOT EXISTS doc_limits JSONB NOT NULL DEFAULT '{}'::jsonb
    """)

    # 2. documents
    op.execute("""
        CREATE TABLE app.documents (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            subject_id UUID NOT NULL,
            title VARCHAR(200) NOT NULL,
            description TEXT,
            created_by UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            deleted_by UUID,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_documents_tenant_id_subject UNIQUE (tenant_id, id, subject_id),
            CONSTRAINT fk_documents_subject FOREIGN KEY (tenant_id, subject_id)
                REFERENCES app.subjects(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_documents_created_by FOREIGN KEY (tenant_id, created_by)
                REFERENCES app.users(tenant_id, id)
        )
    """)

    # 3. document_classes: one document, many classes, embeddings stored once. The second
    #    composite FK rejects a (class, subject) pair that the class does not offer.
    op.execute("""
        CREATE TABLE app.document_classes (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            document_id UUID NOT NULL,
            class_id UUID NOT NULL,
            subject_id UUID NOT NULL,
            PRIMARY KEY (tenant_id, document_id, class_id),
            CONSTRAINT fk_document_classes_document FOREIGN KEY (tenant_id, document_id, subject_id)
                REFERENCES app.documents(tenant_id, id, subject_id) ON DELETE CASCADE,
            CONSTRAINT fk_document_classes_class_subject FOREIGN KEY (tenant_id, class_id, subject_id)
                REFERENCES app.class_subjects(tenant_id, class_id, subject_id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX ix_document_classes_class
        ON app.document_classes (tenant_id, class_id, subject_id)
    """)

    # 4. document_versions
    op.execute(f"""
        CREATE TABLE app.document_versions (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            document_id UUID NOT NULL,
            version_no INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            stage VARCHAR(20),
            error_code VARCHAR(64),
            file_kind VARCHAR(8) NOT NULL,
            original_filename VARCHAR(100) NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 CHAR(64) NOT NULL,
            storage_key TEXT NOT NULL,
            page_count INTEGER,
            chunk_count INTEGER,
            flagged_chunk_count INTEGER,
            flags_acknowledged_by UUID,
            embedding_model VARCHAR(120),
            uploaded_by UUID NOT NULL,
            published_by UUID,
            published_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT document_versions_status_check
                CHECK (status IN ({sql_in_list(DOCUMENT_VERSION_STATUSES)})),
            CONSTRAINT document_versions_stage_check
                CHECK (stage IN ({sql_in_list(INGEST_STAGES)})),
            CONSTRAINT document_versions_file_kind_check
                CHECK (file_kind IN ({sql_in_list(FILE_KINDS)})),
            CONSTRAINT uq_document_versions_version_no UNIQUE (tenant_id, document_id, version_no),
            CONSTRAINT uq_document_versions_sha256 UNIQUE (tenant_id, document_id, sha256),
            CONSTRAINT uq_document_versions_id_document UNIQUE (tenant_id, id, document_id),
            CONSTRAINT fk_document_versions_document FOREIGN KEY (tenant_id, document_id)
                REFERENCES app.documents(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX uq_document_versions_one_published
        ON app.document_versions (tenant_id, document_id)
        WHERE status = '{VERSION_STATUS_PUBLISHED}'
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX uq_document_versions_one_in_flight
        ON app.document_versions (tenant_id, document_id)
        WHERE status IN ({sql_in_list(IN_FLIGHT_VERSION_STATUSES)})
    """)

    # 5. chunks. The full-text column is generated with the 'simple' config (no stemming, safe
    #    for Hindi and Marathi). subject_id is denormalized; the second FK keeps it consistent.
    op.execute("""
        CREATE TABLE app.chunks (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            document_version_id UUID NOT NULL,
            document_id UUID NOT NULL,
            subject_id UUID NOT NULL,
            ordinal INTEGER NOT NULL,
            page_start INTEGER,
            page_end INTEGER,
            heading_path TEXT,
            content TEXT NOT NULL,
            content_sha256 CHAR(64) NOT NULL,
            token_count INTEGER NOT NULL,
            script VARCHAR(8) NOT NULL,
            content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            injection_flags TEXT[] NOT NULL DEFAULT '{}',
            excluded BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_chunks_version_ordinal UNIQUE (tenant_id, document_version_id, ordinal),
            CONSTRAINT fk_chunks_version FOREIGN KEY (tenant_id, document_version_id, document_id)
                REFERENCES app.document_versions(tenant_id, id, document_id) ON DELETE CASCADE,
            CONSTRAINT fk_chunks_document_subject FOREIGN KEY (tenant_id, document_id, subject_id)
                REFERENCES app.documents(tenant_id, id, subject_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_chunks_version ON app.chunks (tenant_id, document_version_id)")
    op.execute("CREATE INDEX ix_chunks_subject ON app.chunks (tenant_id, subject_id)")
    op.execute("CREATE INDEX ix_chunks_content_tsv ON app.chunks USING gin (content_tsv)")
    op.execute("CREATE INDEX ix_chunks_content_sha256 ON app.chunks (tenant_id, content_sha256)")

    # 6. chunk_embeddings. The column has no fixed dimension; the CHECK ties it to the stored
    #    embedding_dim and each model gets its own partial expression HNSW index. Types,
    #    functions and operator classes are qualified with the schema found in pg_extension.
    op.execute(f"""
        CREATE TABLE app.chunk_embeddings (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            chunk_id UUID NOT NULL,
            embedding_model VARCHAR(120) NOT NULL,
            embedding_dim SMALLINT NOT NULL,
            embedding {vs}.vector NOT NULL,
            PRIMARY KEY (tenant_id, chunk_id, embedding_model),
            CONSTRAINT chunk_embeddings_dim_check
                CHECK ({vs}.vector_dims(embedding) = embedding_dim),
            CONSTRAINT fk_chunk_embeddings_chunk FOREIGN KEY (tenant_id, chunk_id)
                REFERENCES app.chunks(tenant_id, id) ON DELETE CASCADE
        )
    """)
    model_literal = E5_SMALL_MODEL_KEY.replace("'", "''")
    op.execute(f"""
        CREATE INDEX {HNSW_INDEX_NAME}
        ON app.chunk_embeddings
        USING hnsw ((embedding::{vs}.vector({E5_SMALL_DIM})) {vs}.vector_cosine_ops)
        WHERE embedding_model = '{model_literal}'
    """)
    # The type, functions and operators live in the extension schema; make sure the API role
    # may use it (a no-op when it is the same schema as app).
    op.execute(f"GRANT USAGE ON SCHEMA {vs} TO app_tenant")

    # 7. storage_deletions (outbox for object-store deletes)
    op.execute("""
        CREATE TABLE app.storage_deletions (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            storage_key TEXT NOT NULL,
            reason VARCHAR(40) NOT NULL,
            attempts SMALLINT NOT NULL DEFAULT 0,
            not_before TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id)
        )
    """)
    op.execute("CREATE INDEX ix_storage_deletions_due ON app.storage_deletions (tenant_id, not_before)")

    # 8. ingest_jobs (the Postgres job queue)
    op.execute(f"""
        CREATE TABLE app.ingest_jobs (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            version_id UUID NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT '{JOB_STATUS_QUEUED}',
            attempts SMALLINT NOT NULL DEFAULT 0,
            run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_expires_at TIMESTAMPTZ,
            worker_id TEXT,
            last_error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT ingest_jobs_status_check CHECK (status IN ({sql_in_list(INGEST_JOB_STATUSES)})),
            CONSTRAINT uq_ingest_jobs_version UNIQUE (tenant_id, version_id),
            CONSTRAINT fk_ingest_jobs_version FOREIGN KEY (tenant_id, version_id)
                REFERENCES app.document_versions(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute(f"""
        CREATE INDEX ix_ingest_jobs_queued ON app.ingest_jobs (run_after)
        WHERE status = '{JOB_STATUS_QUEUED}'
    """)
    op.execute(f"""
        CREATE INDEX ix_ingest_jobs_running ON app.ingest_jobs (lease_expires_at)
        WHERE status = '{JOB_STATUS_RUNNING}'
    """)

    # 9. RLS, grants to app_tenant only (none to app_platform), no anon/authenticated access.
    for table in NEW_TABLES:
        _secure_table(table)

    # 10. app_worker: column-level grants on ingest_jobs and tenant ids, nothing else.
    #     tenants has no RLS (verified in design 0d), so SELECT (id) lists every tenant id for
    #     the sweeper while db_dsn and the other columns stay hidden from the worker.
    op.execute("GRANT USAGE ON SCHEMA app TO app_worker")
    op.execute("GRANT SELECT ON app.ingest_jobs TO app_worker")
    update_cols = ", ".join(INGEST_JOB_WORKER_UPDATE_COLUMNS)
    op.execute(f"GRANT UPDATE ({update_cols}) ON app.ingest_jobs TO app_worker")
    op.execute("GRANT DELETE ON app.ingest_jobs TO app_worker")
    op.execute("GRANT SELECT (id) ON app.tenants TO app_worker")
    # The worker sees job metadata across tenants; real work runs in a tenant session as app_tenant.
    op.execute("""
        CREATE POLICY worker_claim ON app.ingest_jobs
            AS PERMISSIVE FOR ALL TO app_worker
            USING (true) WITH CHECK (true)
    """)

    # 11. Permission catalog and backfill of existing tenants' system roles (same pattern as 0005).
    for perm_id, category, description in NEW_PERMISSIONS:
        op.execute(
            text(
                "INSERT INTO app.permissions (id, category, description) "
                "VALUES (:id, :category, :description) ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=perm_id, category=category, description=description)
        )
    # Teachers and tenant admins get both. Students get none of the document permissions.
    op.execute(BACKFILL_ROLE_PERMISSIONS_SQL)


def downgrade() -> None:
    # Permissions first (custom roles holding document:view lose it; there is no older schema for it).
    ids = ", ".join(f"'{p[0]}'" for p in NEW_PERMISSIONS)
    op.execute(f"DELETE FROM app.role_permissions WHERE permission_id IN ({ids})")
    op.execute(f"DELETE FROM app.permissions WHERE id IN ({ids})")

    # app_worker grants. Dropping ingest_jobs removes its table privileges and policies as well.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_worker') THEN
                REVOKE SELECT (id) ON app.tenants FROM app_worker;
                REVOKE USAGE ON SCHEMA app FROM app_worker;
            END IF;
        END $$
    """)

    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS app.{table} CASCADE")
    op.execute("ALTER TABLE app.tenants DROP COLUMN IF EXISTS doc_limits")
