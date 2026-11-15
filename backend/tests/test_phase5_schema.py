"""
tests/test_phase5_schema.py
───────────────────────────
Migration 0008 (documents schema): pgvector, the partial HNSW index, constraints that are
shared with the ORM constants, and the migration's prerequisite checks.
"""

import importlib.util
import re
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import (
    DOCUMENT_VERSION_STATUSES,
    FILE_KINDS,
    INGEST_JOB_STATUSES,
    INGEST_STAGES,
    Chunk,
    ChunkEmbedding,
)
from app.db.models.tenant import Tenant
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY, E5_SMALL_REPO, E5_SMALL_REVISION
from tests.fixtures.academic_world import (
    AcademicWorld,
    create_document_graph,
    fake_vector,
)

pytestmark = pytest.mark.asyncio

HNSW_INDEX = "ix_chunk_embeddings_hnsw_e5_small"


async def _vector_schema(db: AsyncSession) -> str:
    """The quoted schema of the vector extension, read from pg_extension (never hardcoded)."""
    row = (
        await db.execute(
            text(
                "SELECT quote_ident(n.nspname) FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )
        )
    ).scalar()
    assert row is not None, "the vector extension is not installed"
    return str(row)


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# ── pgvector and the HNSW index ──────────────────────────────────────────────


async def test_vector_extension_is_installed_next_to_citext(raw_db: AsyncSession) -> None:
    rows = (
        await raw_db.execute(
            text(
                "SELECT e.extname, n.nspname, e.extversion FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname IN ('vector', 'citext')"
            )
        )
    ).all()
    by_name = {r[0]: r for r in rows}
    assert {"vector", "citext"} <= set(by_name)
    assert by_name["vector"][1] == by_name["citext"][1]
    version = tuple(int(p) for p in by_name["vector"][2].split("."))
    assert version >= (0, 8, 0), f"pgvector {by_name['vector'][2]} is too old for iterative scans"


async def test_embedding_column_uses_the_extension_schema_and_is_unconstrained(
    raw_db: AsyncSession,
) -> None:
    vs_name = (
        await raw_db.execute(
            text(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )
        )
    ).scalar_one()
    col = (
        await raw_db.execute(
            text("""
                SELECT tn.nspname, t.typname, a.atttypmod
                FROM pg_attribute a
                JOIN pg_type t ON t.oid = a.atttypid
                JOIN pg_namespace tn ON tn.oid = t.typnamespace
                WHERE a.attrelid = 'app.chunk_embeddings'::regclass AND a.attname = 'embedding'
            """)
        )
    ).one()
    assert col[0] == vs_name
    assert col[1] == "vector"
    assert col[2] == -1, "the column must not carry a fixed dimension"


async def test_hnsw_index_definition_uses_the_shared_model_key(raw_db: AsyncSession) -> None:
    assert f"{E5_SMALL_REPO}@{E5_SMALL_REVISION}" == E5_SMALL_MODEL_KEY
    assert re.fullmatch(r"[0-9a-f]{40}", E5_SMALL_REVISION)
    indexdef = (
        await raw_db.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'app' AND indexname = :n"
            ),
            {"n": HNSW_INDEX},
        )
    ).scalar_one()
    assert "USING hnsw" in indexdef
    assert f"vector({E5_SMALL_DIM})" in indexdef
    assert "vector_cosine_ops" in indexdef
    assert f"'{E5_SMALL_MODEL_KEY}'" in indexdef


async def test_partial_expression_hnsw_index_is_used_by_the_planner(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    """
    EXPLAIN proof: the cosine ORDER BY on the cast expression, restricted to the e5-small
    model key, is answered by the partial HNSW index (and a different model key is not).
    """
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    chunks = [
        Chunk(
            tenant_id=w.id,
            document_version_id=graph.version.id,
            document_id=graph.document.id,
            subject_id=graph.document.subject_id,
            ordinal=i,
            content=f"Chunk number {i} about motion and force.",
            content_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            token_count=9,
            script="latn",
        )
        for i in range(1, 60)
    ]
    raw_db.add_all(chunks)
    await raw_db.flush()
    raw_db.add_all(
        ChunkEmbedding(
            tenant_id=w.id,
            chunk_id=c.id,
            embedding_model=E5_SMALL_MODEL_KEY,
            embedding_dim=E5_SMALL_DIM,
            embedding=fake_vector(f"hnsw-{i}"),
        )
        for i, c in enumerate(chunks)
    )
    await raw_db.flush()

    vs = await _vector_schema(raw_db)
    qvec = _vector_literal(fake_vector("query"))
    # Superuser session: RLS does not apply, so the plan is about the index only. On a table this
    # small the planner may price a bitmap scan plus sort below the index, so the competing plan
    # shapes are switched off: the question is whether the index CAN answer this query form.
    await raw_db.execute(text("SET LOCAL enable_seqscan = off"))
    await raw_db.execute(text("SET LOCAL enable_bitmapscan = off"))
    await raw_db.execute(text("SET LOCAL enable_sort = off"))
    await raw_db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))

    def query(model_key: str) -> str:
        return (
            "SELECT chunk_id FROM app.chunk_embeddings "
            f"WHERE embedding_model = '{model_key}' "
            f"ORDER BY (embedding::{vs}.vector({E5_SMALL_DIM})) OPERATOR({vs}.<=>) "
            f"'{qvec}'::{vs}.vector({E5_SMALL_DIM}) LIMIT 5"
        )

    plan = "\n".join(
        r[0] for r in (await raw_db.execute(text("EXPLAIN " + query(E5_SMALL_MODEL_KEY)))).all()
    )
    assert HNSW_INDEX in plan, f"HNSW index not used:\n{plan}"

    # The index scan really runs and returns neighbours.
    rows = (await raw_db.execute(text(query(E5_SMALL_MODEL_KEY)))).all()
    assert len(rows) == 5

    # Negative control: the partial predicate does not cover another model.
    other = "\n".join(
        r[0] for r in (await raw_db.execute(text("EXPLAIN " + query("other/model@abc")))).all()
    )
    assert HNSW_INDEX not in other


# ── Constraints ──────────────────────────────────────────────────────────────


async def _expect_error(db: AsyncSession, stmt: object, constraint: str) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        async with db.begin_nested():
            await db.execute(stmt)  # type: ignore[arg-type]
    assert constraint in str(exc_info.value), str(exc_info.value)


async def test_embedding_dimension_check_and_mixed_dimensions(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    # embedding_dim must match the real vector length.
    await _expect_error(
        raw_db,
        insert(ChunkEmbedding).values(
            tenant_id=w.id,
            chunk_id=graph.chunk.id,
            embedding_model="test/model-b@1",
            embedding_dim=10,
            embedding=[0.1, 0.2, 0.3],
        ),
        "chunk_embeddings_dim_check",
    )
    # The column is unconstrained: another model with another dimension sits next to e5-small.
    await raw_db.execute(
        insert(ChunkEmbedding).values(
            tenant_id=w.id,
            chunk_id=graph.chunk.id,
            embedding_model="test/model-b@1",
            embedding_dim=3,
            embedding=[0.1, 0.2, 0.3],
        )
    )
    # Same chunk and model twice is rejected (composite primary key).
    await _expect_error(
        raw_db,
        insert(ChunkEmbedding).values(
            tenant_id=w.id,
            chunk_id=graph.chunk.id,
            embedding_model="test/model-b@1",
            embedding_dim=3,
            embedding=[0.3, 0.2, 0.1],
        ),
        "chunk_embeddings_pkey",
    )


async def test_vector_round_trip_through_the_orm(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    await raw_db.flush()
    stored = (
        await raw_db.execute(
            select(ChunkEmbedding.embedding).where(ChunkEmbedding.chunk_id == graph.chunk.id)
        )
    ).scalar_one()
    assert len(stored) == E5_SMALL_DIM
    assert stored == pytest.approx(graph.embedding.embedding, abs=1e-5)


async def test_chunk_text_search_column_is_generated_with_the_simple_config(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    generated = (
        await raw_db.execute(
            text(
                "SELECT attgenerated::text FROM pg_attribute "
                "WHERE attrelid = 'app.chunks'::regclass AND attname = 'content_tsv'"
            )
        )
    ).scalar_one()
    assert generated == "s"  # stored generated column
    hit = (
        await raw_db.execute(
            text(
                "SELECT count(*) FROM app.chunks WHERE id = :id "
                "AND content_tsv @@ plainto_tsquery('simple', 'Newton')"
            ),
            {"id": graph.chunk.id},
        )
    ).scalar_one()
    assert hit == 1


async def test_chunk_keeps_document_and_subject_consistent(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    other = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )

    def chunk(**over: Any) -> object:
        values: dict[str, Any] = {
            "tenant_id": w.id,
            "document_version_id": graph.version.id,
            "document_id": graph.document.id,
            "subject_id": graph.document.subject_id,
            "ordinal": 99,
            "content": "x",
            "content_sha256": "0" * 64,
            "token_count": 1,
            "script": "latn",
        }
        values.update(over)
        return insert(Chunk).values(**values)

    # Wrong denormalized subject for the document.
    await _expect_error(
        raw_db, chunk(subject_id=w.subjects["Chemistry"].id), "fk_chunks_document_subject"
    )
    # A version that belongs to another document.
    await _expect_error(
        raw_db, chunk(document_id=other.document.id, subject_id=other.document.subject_id),
        "fk_chunks_version",
    )
    # The same ordinal twice in one version.
    await _expect_error(raw_db, chunk(ordinal=0), "uq_chunks_version_ordinal")
    await raw_db.execute(chunk())


async def test_check_constraints_match_the_shared_python_constants(raw_db: AsyncSession) -> None:
    """The migration and the ORM build their CHECK lists from the same constants."""
    rows = (
        await raw_db.execute(
            text("""
                SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conname IN ('document_versions_status_check', 'document_versions_stage_check',
                                  'document_versions_file_kind_check', 'ingest_jobs_status_check')
            """)
        )
    ).all()
    defs = {name: set(re.findall(r"'([a-z_]+)'", definition)) for name, definition in rows}
    assert defs["document_versions_status_check"] == set(DOCUMENT_VERSION_STATUSES)
    assert defs["document_versions_stage_check"] == set(INGEST_STAGES)
    assert defs["document_versions_file_kind_check"] == set(FILE_KINDS)
    assert defs["ingest_jobs_status_check"] == set(INGEST_JOB_STATUSES)


async def test_tenants_have_doc_limits_defaulting_to_empty(
    raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    limits = (
        await raw_db.execute(select(Tenant.doc_limits).where(Tenant.id == tenant_a.id))
    ).scalar_one()
    assert limits == {}


async def test_new_tables_are_not_granted_to_the_platform_role(raw_db: AsyncSession) -> None:
    """No table-level grant, no column-level grant and no effective privilege (PUBLIC included)."""
    tables = [
        "documents", "document_classes", "document_versions", "chunks",
        "chunk_embeddings", "storage_deletions", "ingest_jobs",
    ]
    table_grants = (
        await raw_db.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'app_platform' AND table_schema = 'app' AND table_name = ANY(:t)"
            ),
            {"t": tables},
        )
    ).all()
    assert table_grants == []
    # Column-level grants (GRANT SELECT (col) ...) do not show up in role_table_grants.
    column_grants = (
        await raw_db.execute(
            text(
                "SELECT table_name, column_name, privilege_type "
                "FROM information_schema.column_privileges "
                "WHERE grantee = 'app_platform' AND table_schema = 'app' AND table_name = ANY(:t)"
            ),
            {"t": tables},
        )
    ).all()
    assert column_grants == []
    # Effective privileges, which also cover grants to PUBLIC and role inheritance.
    for table in tables:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            has = (
                await raw_db.execute(
                    text("SELECT has_table_privilege('app_platform', :t, :p)"),
                    {"t": f"app.{table}", "p": privilege},
                )
            ).scalar_one()
            assert not has, f"app_platform effectively has {privilege} on app.{table}"
        any_column = (
            await raw_db.execute(
                text(
                    "SELECT has_any_column_privilege('app_platform', :t, 'SELECT,INSERT,UPDATE,REFERENCES')"
                ),
                {"t": f"app.{table}"},
            )
        ).scalar_one()
        assert not any_column, f"app_platform has a column privilege on app.{table}"


# ── Migration prerequisites ──────────────────────────────────────────────────


def _load_migration() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0008_phase5_documents.py"
    spec = importlib.util.spec_from_file_location("migration_0008_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeConnection:
    """Answers the migration's catalog lookups; `vector_schema` / `has_role` control them."""

    dialect = postgresql.dialect()

    def __init__(self, vector_schema: str | None, has_role: bool) -> None:
        self._vector_schema = vector_schema
        self._has_role = has_role

    def execute(self, statement: object, params: object = None) -> _FakeResult:
        sql = str(statement)
        if "pg_extension" in sql:
            return _FakeResult(self._vector_schema)
        if "pg_roles" in sql:
            return _FakeResult(1 if self._has_role else None)
        raise AssertionError(f"unexpected statement before the checks: {sql}")


def _patched_migration(
    monkeypatch: pytest.MonkeyPatch, conn: _FakeConnection
) -> tuple[ModuleType, list[object]]:
    """Load migration 0008 with `op` replaced by a fake; returns (module, executed statements)."""
    module = _load_migration()
    executed: list[object] = []
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(get_bind=lambda: conn, execute=lambda *a, **k: executed.append(a)),
    )
    return module, executed


async def test_migration_fails_clearly_when_pgvector_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, executed = _patched_migration(
        monkeypatch, _FakeConnection(vector_schema=None, has_role=True)
    )
    with pytest.raises(RuntimeError) as exc_info:
        module.upgrade()
    message = str(exc_info.value)
    assert "pgvector" in message and "CREATE EXTENSION" in message
    # It checks first and never creates the extension or anything else.
    assert executed == []


async def test_migration_fails_clearly_when_the_worker_role_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, executed = _patched_migration(
        monkeypatch, _FakeConnection(vector_schema="ext", has_role=False)
    )
    with pytest.raises(RuntimeError, match="app_worker"):
        module.upgrade()
    assert executed == []


async def test_migration_reads_the_vector_schema_from_pg_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any schema name works (the schema is quoted, never hardcoded)."""
    module, _ = _patched_migration(
        monkeypatch, _FakeConnection(vector_schema="Weird Schema", has_role=True)
    )
    assert module._vector_schema() == '"Weird Schema"'
    module, _ = _patched_migration(
        monkeypatch, _FakeConnection(vector_schema="extensions", has_role=True)
    )
    assert module._vector_schema() == "extensions"
