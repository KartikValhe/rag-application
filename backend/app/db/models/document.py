"""
app/db/models/document.py
─────────────────────────
Phase 5 document ingestion tables (migration 0008, design section A2):

  - Document:        one syllabus document for ONE subject (soft-deletable).
  - DocumentClass:   links a document to many classes (embeddings are stored once).
  - DocumentVersion: one uploaded file; the lifecycle lives here.
  - Chunk:           searchable text pieces of a version (full-text column is generated).
  - ChunkEmbedding:  one vector per chunk and embedding model.
  - StorageDeletion: outbox of files to delete from the object store.
  - IngestJob:       the Postgres job queue (claimed by the app_worker role).

Every table has tenant_id, a composite primary key that starts with tenant_id and composite
foreign keys. RLS (ENABLE + FORCE and the tenant_isolation policy) is created by the
migration. The status and kind constants below are used by both these models and the
migration, so the CHECK constraints cannot drift from the code.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.db.models.base import Base

# ── Shared constants (also imported by migration 0008) ───────────────────────

VERSION_STATUS_QUEUED = "queued"
VERSION_STATUS_PROCESSING = "processing"
VERSION_STATUS_READY = "ready"
VERSION_STATUS_PUBLISHED = "published"
VERSION_STATUS_ARCHIVED = "archived"
VERSION_STATUS_FAILED = "failed"

DOCUMENT_VERSION_STATUSES: tuple[str, ...] = (
    VERSION_STATUS_QUEUED,
    VERSION_STATUS_PROCESSING,
    VERSION_STATUS_READY,
    VERSION_STATUS_PUBLISHED,
    VERSION_STATUS_ARCHIVED,
    VERSION_STATUS_FAILED,
)
# A version in one of these states counts as "the one draft in flight" for a document.
IN_FLIGHT_VERSION_STATUSES: tuple[str, ...] = (
    VERSION_STATUS_QUEUED,
    VERSION_STATUS_PROCESSING,
    VERSION_STATUS_READY,
)
INGEST_STAGES: tuple[str, ...] = ("parsing", "chunking", "scanning", "embedding", "indexing")
FILE_KINDS: tuple[str, ...] = ("pdf", "docx", "pptx", "txt", "md")

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"
INGEST_JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_DONE,
    JOB_STATUS_FAILED,
)

# Job states the worker may still change, and the finished states it may delete (RLS policies
# worker_update / worker_delete in migration 0009).
ACTIVE_JOB_STATUSES: tuple[str, ...] = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)
FINISHED_JOB_STATUSES: tuple[str, ...] = (JOB_STATUS_DONE, JOB_STATUS_FAILED)
# A version in one of these states does not count as a duplicate of the same bytes.
INACTIVE_VERSION_STATUSES: tuple[str, ...] = (VERSION_STATUS_FAILED, VERSION_STATUS_ARCHIVED)

# Columns the app_worker role may UPDATE on ingest_jobs (nothing else is granted).
INGEST_JOB_WORKER_UPDATE_COLUMNS: tuple[str, ...] = (
    "status",
    "attempts",
    "run_after",
    "lease_expires_at",
    "worker_id",
    "last_error_code",
    "updated_at",
)

# Columns the API role (app_tenant) may UPDATE on ingest_jobs: exactly what the retry path needs
# (design A3). The lease and the worker id stay worker-only.
INGEST_JOB_API_UPDATE_COLUMNS: tuple[str, ...] = (
    "status",
    "attempts",
    "run_after",
    "last_error_code",
    "updated_at",
)

CHUNK_SCRIPTS: tuple[str, ...] = ("latn", "deva", "mixed")


def sql_in_list(values: Sequence[str]) -> str:
    """Render 'a', 'b' style literals for CHECK constraints from trusted constants."""
    for v in values:
        if "'" in v:
            raise ValueError("constants used in CHECK constraints must not contain quotes")
    return ", ".join(f"'{v}'" for v in values)


class Vector(UserDefinedType[Any]):
    """
    pgvector `vector` column without a fixed dimension.

    The dimension is locked per model by the partial expression index and by a CHECK
    constraint, not by the column type (design A1). Values travel as text ('[1,2,3]'), so
    no driver codec is needed. The type name is unqualified on purpose: the table is
    created by the migration, which finds the extension schema in pg_extension.
    """

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Sequence[float] | str | None) -> str | None:
            if value is None or isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            body = value.strip().lstrip("[").rstrip("]")
            return [float(x) for x in body.split(",")] if body else []

        return process


# ── Models ───────────────────────────────────────────────────────────────────


class Document(Base):
    """One document = one subject. Soft-deleted through deleted_at (tombstone)."""

    __tablename__ = "documents"
    __table_args__ = (
        # Target of the document_classes / chunks composite FKs (subject stays consistent).
        UniqueConstraint("tenant_id", "id", "subject_id", name="uq_documents_tenant_id_subject"),
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"],
            ["app.subjects.tenant_id", "app.subjects.id"],
            # A subject that still has documents cannot be deleted (the API answers 409).
            ondelete="RESTRICT",
            name="fk_documents_subject",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["app.users.tenant_id", "app.users.id"],
            name="fk_documents_created_by",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deleted_by"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="SET NULL (deleted_by)",  # PostgreSQL 15+: tenant_id is never nulled
            name="fk_documents_deleted_by",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class DocumentClass(Base):
    """Links a document to a class. The (class, subject) pair must be offered in class_subjects."""

    __tablename__ = "document_classes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "subject_id"],
            ["app.documents.tenant_id", "app.documents.id", "app.documents.subject_id"],
            ondelete="CASCADE",
            name="fk_document_classes_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "class_id", "subject_id"],
            [
                "app.class_subjects.tenant_id",
                "app.class_subjects.class_id",
                "app.class_subjects.subject_id",
            ],
            ondelete="CASCADE",
            name="fk_document_classes_class_subject",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class DocumentVersion(Base):
    """One uploaded file of a document and its processing lifecycle."""

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(DOCUMENT_VERSION_STATUSES)})",
            name="document_versions_status_check",
        ),
        CheckConstraint(
            f"stage IN ({sql_in_list(INGEST_STAGES)})",
            name="document_versions_stage_check",
        ),
        CheckConstraint(
            f"file_kind IN ({sql_in_list(FILE_KINDS)})",
            name="document_versions_file_kind_check",
        ),
        UniqueConstraint(
            "tenant_id", "document_id", "version_no", name="uq_document_versions_version_no"
        ),
        # A published version always records when it was published.
        CheckConstraint(
            f"status <> '{VERSION_STATUS_PUBLISHED}' OR published_at IS NOT NULL",
            name="document_versions_published_at_check",
        ),
        # A storage key always lives under the row's own tenant prefix.
        CheckConstraint(
            "storage_key LIKE 't/' || tenant_id::text || '/%'",
            name="ck_document_versions_storage_key_prefix",
        ),
        # Target of the chunks composite FK (document stays consistent with the version).
        UniqueConstraint("tenant_id", "id", "document_id", name="uq_document_versions_id_document"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["app.documents.tenant_id", "app.documents.id"],
            ondelete="CASCADE",
            name="fk_document_versions_document",
        ),
        # Idempotency among ACTIVE versions: the same bytes cannot be uploaded twice, but a
        # permanently failed upload or a rollback to older bytes can be uploaded again.
        Index(
            "uq_document_versions_active_sha256",
            "tenant_id",
            "document_id",
            "sha256",
            unique=True,
            postgresql_where=text(f"status NOT IN ({sql_in_list(INACTIVE_VERSION_STATUSES)})"),
        ),
        # Actor columns: same-tenant users. The NOT NULL actor is RESTRICTed, the nullable ones
        # are cleared when the user row goes (PostgreSQL 15+ column list keeps tenant_id).
        ForeignKeyConstraint(
            ["tenant_id", "uploaded_by"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="RESTRICT",
            name="fk_document_versions_uploaded_by",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "published_by"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="SET NULL (published_by)",
            name="fk_document_versions_published_by",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "flags_acknowledged_by"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="SET NULL (flags_acknowledged_by)",
            name="fk_document_versions_flags_acknowledged_by",
        ),
        # At most one published version, and one draft in flight, per document.
        Index(
            "uq_document_versions_one_published",
            "tenant_id",
            "document_id",
            unique=True,
            postgresql_where=text(f"status = '{VERSION_STATUS_PUBLISHED}'"),
        ),
        Index(
            "uq_document_versions_one_in_flight",
            "tenant_id",
            "document_id",
            unique=True,
            postgresql_where=text(f"status IN ({sql_in_list(IN_FLIGHT_VERSION_STATUSES)})"),
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    # sanitize_filename() output, for display only.
    original_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flagged_chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flags_acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Chunk(Base):
    """A searchable piece of text. The subject is denormalized for fast filtering."""

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint(f"script IN ({sql_in_list(CHUNK_SCRIPTS)})", name="chunks_script_check"),
        UniqueConstraint(
            "tenant_id", "document_version_id", "ordinal", name="uq_chunks_version_ordinal"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_version_id", "document_id"],
            [
                "app.document_versions.tenant_id",
                "app.document_versions.id",
                "app.document_versions.document_id",
            ],
            ondelete="CASCADE",
            name="fk_chunks_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "subject_id"],
            ["app.documents.tenant_id", "app.documents.id", "app.documents.subject_id"],
            ondelete="CASCADE",
            name="fk_chunks_document_subject",
        ),
        Index("ix_chunks_version", "tenant_id", "document_version_id"),
        Index("ix_chunks_subject", "tenant_id", "subject_id"),
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        # Embedding reuse: find a vector by the hash of the chunk text.
        Index("ix_chunks_content_sha256", "tenant_id", "content_sha256"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    script: Mapped[str] = mapped_column(String(8), nullable=False)  # latn | deva | mixed
    # The 'simple' config does no stemming, which is the safe choice for Hindi and Marathi.
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=False
    )
    injection_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class ChunkEmbedding(Base):
    """
    One vector per chunk and model. The column has no fixed dimension (see Vector); the
    partial expression HNSW index for each model is created by the migration.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "chunk_id"],
            ["app.chunks.tenant_id", "app.chunks.id"],
            ondelete="CASCADE",
            name="fk_chunk_embeddings_chunk",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(120), primary_key=True)
    embedding_dim: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)


class StorageDeletion(Base):
    """
    Outbox: object-store files waiting to be deleted by the sweeper. The tenant FK is RESTRICT,
    not CASCADE: deleting a tenant must drain this table and purge its files first, otherwise
    the pending deletions would vanish and the objects would be orphaned.
    """

    __tablename__ = "storage_deletions"
    __table_args__ = (
        CheckConstraint(
            "storage_key LIKE 't/' || tenant_id::text || '/%'",
            name="ck_storage_deletions_storage_key_prefix",
        ),
        Index("ix_storage_deletions_due", "tenant_id", "not_before"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestJob(Base):
    """
    The job queue (design A3). One job per version. The app_worker role claims rows with
    FOR UPDATE SKIP LOCKED and may only change the lease and retry columns.
    """

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(INGEST_JOB_STATUSES)})", name="ingest_jobs_status_check"
        ),
        UniqueConstraint("tenant_id", "version_id", name="uq_ingest_jobs_version"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["app.document_versions.tenant_id", "app.document_versions.id"],
            ondelete="CASCADE",
            name="fk_ingest_jobs_version",
        ),
        Index(
            "ix_ingest_jobs_queued",
            "run_after",
            postgresql_where=text(f"status = '{JOB_STATUS_QUEUED}'"),
        ),
        Index(
            "ix_ingest_jobs_running",
            "lease_expires_at",
            postgresql_where=text(f"status = '{JOB_STATUS_RUNNING}'"),
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=JOB_STATUS_QUEUED)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
