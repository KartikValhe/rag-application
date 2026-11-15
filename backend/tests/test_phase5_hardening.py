"""
tests/test_phase5_hardening.py
──────────────────────────────
Security review of step 5a-2: tests for migration 0009_phase5_hardening (and the 0008 downgrade
fix). Every test was written first and fails on the 0008 schema. The ingest_jobs policy and grant
tests live in tests/test_isolation.py next to the other app_worker tests.

Setup rows are created with the privileged session. The checks that matter for the API role run
through tenant_session_context (app_tenant, RLS on), committing like a real request would.
"""

import importlib.util
import re
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.academic import Subject
from app.db.models.document import (
    CHUNK_SCRIPTS,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
    StorageDeletion,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.ml.model_keys import E5_SMALL_MODEL_KEY
from tests.fixtures.academic_world import (
    AcademicWorld,
    WorldTenant,
    create_document_graph,
    fake_vector,
)

pytestmark = pytest.mark.asyncio

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


def _load_migration(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, VERSIONS_DIR / filename)
    assert spec is not None and spec.loader is not None, f"{filename} does not exist"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _expect_constraint(db: AsyncSession, stmt: object, constraint: str) -> None:
    """Run `stmt` in a savepoint; it must fail because of exactly the named constraint or index."""
    with pytest.raises(IntegrityError) as exc_info:
        async with db.begin_nested():
            await db.execute(stmt)  # type: ignore[arg-type]
    assert constraint in str(exc_info.value), f"expected {constraint}, got: {exc_info.value}"


def _version_values(w: WorldTenant, document_id: uuid.UUID, **over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tenant_id": w.id,
        "document_id": document_id,
        "version_no": over.pop("version_no", 50 + uuid.uuid4().int % 100000),
        "status": "failed",
        "file_kind": "pdf",
        "original_filename": "v.pdf",
        "size_bytes": 10,
        "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        "storage_key": f"t/{w.id}/d/{uuid.uuid4().hex}",
        "uploaded_by": w.t1.id,
    }
    values.update(over)
    return values


async def _make_user(db: AsyncSession, w: WorldTenant, label: str) -> User:
    user = User(
        tenant_id=w.id,
        email=f"{label}-{uuid.uuid4().hex}@hardening.example.com",
        full_name=label,
        password_hash=None,
        status="active",
    )
    db.add(user)
    await db.flush()
    return user


# ── Finding 3: a subject with documents cannot be deleted ────────────────────


async def test_fk_documents_subject_restricts_instead_of_cascading(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    await _expect_constraint(
        raw_db,
        delete(Subject).where(Subject.tenant_id == w.id, Subject.id == graph.document.subject_id),
        "fk_documents_subject",
    )
    # Nothing was removed on the way.
    for model in (Document, DocumentVersion, Chunk, ChunkEmbedding):
        count = (
            await raw_db.execute(text(f"SELECT count(*) FROM {model.__table__.fullname} WHERE tenant_id = :t"), {"t": w.id})  # type: ignore[attr-defined]
        ).scalar_one()
        assert count >= 1, f"{model.__tablename__} rows disappeared"


@pytest.mark.parametrize("tombstone", [False, True], ids=["live_document", "soft_deleted_document"])
async def test_deleting_a_subject_with_documents_returns_409(
    client: AsyncClient, raw_db: AsyncSession, academic_world: AcademicWorld, tombstone: bool
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    if tombstone:
        await raw_db.execute(
            text("UPDATE app.documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": w.admin.id, "d": graph.document.id},
        )
    await raw_db.commit()
    # Plain ids: ORM objects expire on commit and must not be touched again.
    subject_id = graph.document.subject_id
    kept = (
        (Document, graph.document.id),
        (DocumentVersion, graph.version.id),
        (Chunk, graph.chunk.id),
        (IngestJob, graph.job.id),
    )

    resp = await client.delete(
        f"/api/v1/subjects/{subject_id}", headers=_headers(w.token(w.admin), w.tenant.slug)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "subject_has_documents"

    # The subject, the document, its version, chunk, embedding and job all still exist.
    raw_db.expire_all()
    assert (await raw_db.execute(select(Subject.id).where(Subject.id == subject_id))).scalar_one()
    for model, key in kept:
        found = (await raw_db.execute(select(model.id).where(model.id == key))).scalar_one_or_none()  # type: ignore[attr-defined]
        assert found is not None, f"{model.__tablename__} row was deleted"


async def test_deleting_a_subject_without_documents_still_works(
    client: AsyncClient, raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    spare = Subject(tenant_id=w.id, name=f"Spare-{uuid.uuid4().hex[:8]}")
    raw_db.add(spare)
    await raw_db.commit()
    spare_id = spare.id
    resp = await client.delete(
        f"/api/v1/subjects/{spare_id}", headers=_headers(w.token(w.admin), w.tenant.slug)
    )
    assert resp.status_code == 200, resp.text
    raw_db.expire_all()
    assert (
        await raw_db.execute(select(Subject.id).where(Subject.id == spare_id))
    ).scalar_one_or_none() is None


# ── Finding 4: the storage outbox is not cascaded away with the tenant ──────


async def test_storage_deletions_outbox_blocks_tenant_deletion(raw_db: AsyncSession) -> None:
    tenant = Tenant(slug=f"outbox-{uuid.uuid4().hex}", name="Outbox Org", kind="school")
    raw_db.add(tenant)
    await raw_db.flush()
    raw_db.add(
        StorageDeletion(
            tenant_id=tenant.id, storage_key=f"t/{tenant.id}/d/{uuid.uuid4().hex}", reason="test"
        )
    )
    await raw_db.flush()
    await _expect_constraint(
        raw_db, delete(Tenant).where(Tenant.id == tenant.id), "storage_deletions_tenant_id_fkey"
    )
    count = (
        await raw_db.execute(
            text("SELECT count(*) FROM app.storage_deletions WHERE tenant_id = :t"), {"t": tenant.id}
        )
    ).scalar_one()
    assert count == 1, "the outbox row was cascaded away"

    # Once the outbox is drained the tenant can go (the offboarding order in docs/backlog.md).
    await raw_db.execute(delete(StorageDeletion).where(StorageDeletion.tenant_id == tenant.id))
    await raw_db.execute(delete(Tenant).where(Tenant.id == tenant.id))


# ── Finding 5: storage keys must live under the row's own tenant prefix ─────


@pytest.mark.parametrize("table", ["document_versions", "storage_deletions"])
@pytest.mark.parametrize(
    "bad_key",
    [
        "other_tenant_prefix",  # replaced below with the other tenant's prefix
        "no/prefix/at/all",
        "t/{own}",  # no trailing slash
        "x/{own}/d/abc",  # wrong leading segment
    ],
)
async def test_storage_key_must_carry_the_tenant_prefix(
    raw_db: AsyncSession, academic_world: AcademicWorld, table: str, bad_key: str
) -> None:
    w, other = academic_world.a, academic_world.b
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    key = (
        f"t/{other.id}/d/{uuid.uuid4().hex}"
        if bad_key == "other_tenant_prefix"
        else bad_key.format(own=w.id)
    )
    good = f"t/{w.id}/d/{uuid.uuid4().hex}"
    constraint = f"ck_{table}_storage_key_prefix"
    if table == "document_versions":
        await _expect_constraint(
            raw_db,
            insert(DocumentVersion).values(**_version_values(w, graph.document.id, storage_key=key)),
            constraint,
        )
        # An UPDATE cannot move the key out of the prefix either.
        await _expect_constraint(
            raw_db,
            update(DocumentVersion).where(DocumentVersion.id == graph.version.id).values(storage_key=key),
            constraint,
        )
        await raw_db.execute(
            insert(DocumentVersion).values(**_version_values(w, graph.document.id, storage_key=good))
        )
    else:
        await _expect_constraint(
            raw_db,
            insert(StorageDeletion).values(tenant_id=w.id, storage_key=key, reason="t"),
            constraint,
        )
        await raw_db.execute(
            insert(StorageDeletion).values(tenant_id=w.id, storage_key=good, reason="t")
        )


# ── Finding 7: uniqueness of the file bytes applies to active versions only ──


async def test_failed_upload_and_rollback_bytes_can_be_uploaded_again(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1, status="published"
    )
    doc_id = graph.document.id
    sha = "a" * 64

    # A permanently failed upload, then the same bytes again: allowed.
    await raw_db.execute(insert(DocumentVersion).values(**_version_values(w, doc_id, status="failed", sha256=sha)))
    await raw_db.execute(insert(DocumentVersion).values(**_version_values(w, doc_id, status="queued", sha256=sha)))
    # Rolling back to bytes that were archived earlier: allowed while the new copy is not active.
    old = "b" * 64
    await raw_db.execute(insert(DocumentVersion).values(**_version_values(w, doc_id, status="archived", sha256=old)))
    await raw_db.execute(update(DocumentVersion).where(DocumentVersion.sha256 == sha).values(status="failed"))
    await raw_db.execute(insert(DocumentVersion).values(**_version_values(w, doc_id, status="queued", sha256=old)))
    # Do not commit: rows with repeated bytes would make `alembic downgrade` (which restores the
    # old UNIQUE) refuse to run on a database that has seen this test.
    await raw_db.rollback()


@pytest.mark.parametrize("status", ["queued", "processing", "ready", "published"])
async def test_same_bytes_as_an_active_version_are_still_rejected(
    raw_db: AsyncSession, academic_world: AcademicWorld, status: str
) -> None:
    """duplicate_version (409) still applies against active versions."""
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1, status=status
    )
    # Pick a duplicate status that cannot trip the one-published / one-in-flight indexes, so only
    # the sha256 index is in play.
    if status == "published":
        dup: dict[str, Any] = {"status": "queued"}
    else:
        dup = {"status": "published", "published_at": _now()}
    await _expect_constraint(
        raw_db,
        insert(DocumentVersion).values(
            **_version_values(w, graph.document.id, sha256=graph.version.sha256, **dup)
        ),
        "uq_document_versions_active_sha256",
    )
    # The same bytes as history (failed or archived) are not a duplicate.
    for history in ("failed", "archived"):
        await raw_db.execute(
            insert(DocumentVersion).values(
                **_version_values(w, graph.document.id, status=history, sha256=graph.version.sha256)
            )
        )
    await raw_db.rollback()  # see test_failed_upload_and_rollback_bytes_can_be_uploaded_again


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# ── Finding 8: the migration role must be able to bypass RLS ─────────────────


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value

    def one(self) -> object:
        return self._value


class _FakeConnection:
    """Answers the catalog lookups of the migration; `can_bypass` controls the role check."""

    dialect = postgresql.dialect()

    def __init__(self, can_bypass: bool, vector_schema: str | None = "app") -> None:
        self._can_bypass = can_bypass
        self._vector_schema = vector_schema
        self.statements: list[str] = []

    def execute(self, statement: object, params: object = None) -> _FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if "rolbypassrls" in sql:
            return _FakeResult(self._can_bypass)
        if "HAVING" in sql:  # the downgrade's duplicate-bytes check
            return _FakeResult(None)
        if "pg_extension" in sql:
            return _FakeResult(self._vector_schema)
        if "pg_roles" in sql:
            return _FakeResult(1)
        raise AssertionError(f"unexpected statement before the checks finished: {sql}")


def _patched(module: ModuleType, monkeypatch: pytest.MonkeyPatch, conn: _FakeConnection) -> list[str]:
    executed: list[str] = []
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(get_bind=lambda: conn, execute=lambda stmt, *a, **k: executed.append(str(stmt))),
    )
    return executed


async def test_0009_refuses_to_run_when_the_role_cannot_bypass_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration("0009_phase5_hardening.py", "migration_0009_rls_check")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=False))
    with pytest.raises(RuntimeError) as exc_info:
        module.upgrade()
    message = str(exc_info.value)
    assert "BYPASSRLS" in message and "superuser" in message
    assert "0 rows" in message or "silently" in message
    # It checks first: no statement was issued before the refusal.
    assert executed == []


async def test_0009_runs_its_check_before_anything_else_and_accepts_a_bypass_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration("0009_phase5_hardening.py", "migration_0009_rls_ok")
    conn = _FakeConnection(can_bypass=True)
    _patched(module, monkeypatch, conn)
    module._require_rls_bypass()  # must not raise
    assert any("rolbypassrls" in s and "rolsuper" in s for s in conn.statements)


# ── Finding 9: actor columns have composite foreign keys ─────────────────────


@pytest.mark.parametrize(
    "column,constraint",
    [
        ("uploaded_by", "fk_document_versions_uploaded_by"),
        ("published_by", "fk_document_versions_published_by"),
        ("flags_acknowledged_by", "fk_document_versions_flags_acknowledged_by"),
    ],
)
@pytest.mark.parametrize("who", ["unknown_user", "user_of_another_tenant"])
async def test_version_actor_columns_must_reference_a_user_of_the_same_tenant(
    raw_db: AsyncSession, academic_world: AcademicWorld, column: str, constraint: str, who: str
) -> None:
    w, other = academic_world.a, academic_world.b
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    actor = uuid.uuid4() if who == "unknown_user" else other.t1.id
    await _expect_constraint(
        raw_db,
        insert(DocumentVersion).values(**_version_values(w, graph.document.id, **{column: actor})),
        constraint,
    )
    # A real user of the same tenant is accepted.
    await raw_db.execute(
        insert(DocumentVersion).values(**_version_values(w, graph.document.id, **{column: w.t2.id}))
    )


@pytest.mark.parametrize("who", ["unknown_user", "user_of_another_tenant"])
async def test_document_deleted_by_must_reference_a_user_of_the_same_tenant(
    raw_db: AsyncSession, academic_world: AcademicWorld, who: str
) -> None:
    w, other = academic_world.a, academic_world.b
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    actor = uuid.uuid4() if who == "unknown_user" else other.t1.id
    await _expect_constraint(
        raw_db,
        update(Document).where(Document.id == graph.document.id).values(deleted_by=actor),
        "fk_documents_deleted_by",
    )


async def test_deleting_the_uploader_is_restricted_but_other_actors_are_set_null(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    uploader = await _make_user(raw_db, w, "uploader")
    publisher = await _make_user(raw_db, w, "publisher")
    reviewer = await _make_user(raw_db, w, "reviewer")
    deleter = await _make_user(raw_db, w, "deleter")
    await raw_db.execute(
        insert(DocumentVersion).values(
            **_version_values(
                w,
                graph.document.id,
                status="archived",
                uploaded_by=uploader.id,
                published_by=publisher.id,
                flags_acknowledged_by=reviewer.id,
                published_at=_now(),
            )
        )
    )
    await raw_db.execute(
        update(Document).where(Document.id == graph.document.id).values(deleted_by=deleter.id)
    )

    # NOT NULL actor: RESTRICT.
    await _expect_constraint(
        raw_db,
        delete(User).where(User.tenant_id == w.id, User.id == uploader.id),
        "fk_document_versions_uploaded_by",
    )
    # Nullable actors: the user can go and only that column is cleared (tenant_id stays).
    for user in (publisher, reviewer, deleter):
        await raw_db.execute(delete(User).where(User.tenant_id == w.id, User.id == user.id))
    version = (
        await raw_db.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == graph.document.id,
                DocumentVersion.uploaded_by == uploader.id,
            )
        )
    ).scalar_one()
    await raw_db.refresh(version)
    assert version.tenant_id == w.id
    assert version.published_by is None
    assert version.flags_acknowledged_by is None
    doc = (await raw_db.execute(select(Document).where(Document.id == graph.document.id))).scalar_one()
    await raw_db.refresh(doc)
    assert doc.tenant_id == w.id and doc.deleted_by is None


# ── Finding 10: the 0008 downgrade ───────────────────────────────────────────


_DROP_CASCADE = re.compile(r"\bDROP\b[^;]*\bCASCADE\b", re.IGNORECASE | re.DOTALL)


async def test_0008_downgrade_has_no_cascade_and_revokes_the_vector_schema_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration("0008_phase5_documents.py", "migration_0008_downgrade")
    conn = _FakeConnection(can_bypass=True, vector_schema="ext")
    executed = _patched(module, monkeypatch, conn)
    module.downgrade()
    assert not [s for s in executed if _DROP_CASCADE.search(s)], "DROP ... CASCADE hides dependencies"
    assert any(
        re.search(r"REVOKE\s+USAGE\s+ON\s+SCHEMA\s+ext\s+FROM\s+app_tenant", s, re.IGNORECASE)
        for s in executed
    ), "the USAGE grant on the vector schema must be revoked again"


async def test_0008_downgrade_keeps_app_usage_when_the_vector_schema_is_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locally pgvector sits in schema app, whose USAGE for app_tenant comes from init.sql."""
    module = _load_migration("0008_phase5_documents.py", "migration_0008_downgrade_app")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=True, vector_schema="app"))
    module.downgrade()
    assert not [
        s for s in executed if re.search(r"REVOKE\s+USAGE\s+ON\s+SCHEMA\s+app\s+FROM\s+app_tenant", s, re.I)
    ]


async def test_0009_downgrade_has_no_drop_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration("0009_phase5_hardening.py", "migration_0009_downgrade")
    executed = _patched(module, monkeypatch, _FakeConnection(can_bypass=True))
    module.downgrade()
    assert executed, "downgrade() issued nothing"
    assert not [s for s in executed if _DROP_CASCADE.search(s)]


# ── Finding 11: chunk script and published_at invariants ────────────────────


async def test_chunk_script_is_limited_to_the_shared_constant(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )

    def chunk(ordinal: int, script: str) -> object:
        return insert(Chunk).values(
            tenant_id=w.id,
            document_version_id=graph.version.id,
            document_id=graph.document.id,
            subject_id=graph.document.subject_id,
            ordinal=ordinal,
            content="x",
            content_sha256="0" * 64,
            token_count=1,
            script=script,
        )

    for i, script in enumerate(CHUNK_SCRIPTS, start=10):
        await raw_db.execute(chunk(i, script))
    await _expect_constraint(raw_db, chunk(99, "klingon"), "chunks_script_check")
    await _expect_constraint(raw_db, chunk(98, ""), "chunks_script_check")

    definition = (
        await raw_db.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'chunks_script_check'")
        )
    ).scalar_one()
    assert set(re.findall(r"'([a-z]+)'", definition)) == set(CHUNK_SCRIPTS)


async def test_published_versions_must_have_a_published_at(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    # No published version yet, so only the CHECK can reject the first insert below.
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1, status="archived"
    )
    constraint = "document_versions_published_at_check"
    await _expect_constraint(
        raw_db,
        insert(DocumentVersion).values(
            **_version_values(w, graph.document.id, status="published", published_at=None)
        ),
        constraint,
    )
    # With a timestamp it is accepted, and the timestamp cannot be removed again.
    published = _version_values(w, graph.document.id, status="published", published_at=_now())
    await raw_db.execute(insert(DocumentVersion).values(**published))
    await _expect_constraint(
        raw_db,
        update(DocumentVersion)
        .where(DocumentVersion.document_id == graph.document.id, DocumentVersion.status == "published")
        .values(published_at=None),
        constraint,
    )
    # Other statuses do not need it.
    for status in ("queued", "failed", "archived"):
        await raw_db.execute(
            insert(DocumentVersion).values(**_version_values(w, graph.document.id, status=status))
        )


# ── Negative tests: composite FKs reject a parent in another tenant ──────────


async def _cross_tenant_cases(
    raw_db: AsyncSession, world: AcademicWorld
) -> dict[str, tuple[Any, str]]:
    """Statements run as tenant A that point at tenant B's parent rows, with the FK expected."""
    wa, wb = world.a, world.b
    await create_document_graph(raw_db, wa, subject_name="Math", class_names=("10-A",), uploader=wa.t1)
    gb = await create_document_graph(raw_db, wb, subject_name="Math", class_names=("10-A",), uploader=wb.t1)
    await raw_db.commit()
    return {
        "chunks": (
            insert(Chunk).values(
                tenant_id=wa.id,
                document_version_id=gb.version.id,
                document_id=gb.document.id,
                subject_id=gb.document.subject_id,
                ordinal=77,
                content="x",
                content_sha256="0" * 64,
                token_count=1,
                script="latn",
            ),
            "fk_chunks_version",
        ),
        "document_versions": (
            insert(DocumentVersion).values(**_version_values(wa, gb.document.id)),
            "fk_document_versions_document",
        ),
        "document_classes": (
            insert(DocumentClass).values(
                tenant_id=wa.id,
                document_id=gb.document.id,
                class_id=wa.classes["10-B"].id,
                subject_id=wa.subjects["Math"].id,
            ),
            "fk_document_classes_document",
        ),
        "chunk_embeddings": (
            insert(ChunkEmbedding).values(
                tenant_id=wa.id,
                chunk_id=gb.chunk.id,
                embedding_model="test/other-model@1",
                embedding_dim=3,
                embedding=[0.1, 0.2, 0.3],
            ),
            "fk_chunk_embeddings_chunk",
        ),
        "ingest_jobs": (
            insert(IngestJob).values(tenant_id=wa.id, version_id=gb.version.id, status="queued"),
            "fk_ingest_jobs_version",
        ),
        "documents": (
            insert(Document).values(
                tenant_id=wa.id,
                subject_id=wb.subjects["Physics"].id,
                title="cross tenant subject",
                created_by=wa.t1.id,
            ),
            "fk_documents_subject",
        ),
    }


@pytest.mark.parametrize(
    "table",
    ["chunks", "document_versions", "document_classes", "chunk_embeddings", "ingest_jobs", "documents"],
)
async def test_child_row_in_tenant_a_cannot_point_at_a_tenant_b_parent(
    raw_db: AsyncSession, academic_world: AcademicWorld, table: str
) -> None:
    stmt, constraint = (await _cross_tenant_cases(raw_db, academic_world))[table]
    # The API role in tenant A: the RLS check passes (tenant_id is A's), the composite FK must not.
    async with tenant_session_context(str(academic_world.a.id)) as session:
        with pytest.raises(IntegrityError) as exc_info:
            async with session.begin_nested():
                await session.execute(stmt)
        assert constraint in str(exc_info.value), str(exc_info.value)


# ── Negative test: a vector with the wrong dimension is rejected ────────────


async def test_wrong_dimension_embedding_is_rejected(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1
    )
    await raw_db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == graph.chunk.id))
    # A 3-dimensional vector under the e5-small key (384): embedding_dim says 3, so the dimension
    # CHECK passes, but the model's partial HNSW index casts to vector(384) and rejects it.
    with pytest.raises(DBAPIError) as exc_info:
        async with raw_db.begin_nested():
            await raw_db.execute(
                insert(ChunkEmbedding).values(
                    tenant_id=w.id,
                    chunk_id=graph.chunk.id,
                    embedding_model=E5_SMALL_MODEL_KEY,
                    embedding_dim=3,
                    embedding=[0.1, 0.2, 0.3],
                )
            )
    assert "384" in str(exc_info.value)
    # The right dimension for the model is accepted.
    await raw_db.execute(
        insert(ChunkEmbedding).values(
            tenant_id=w.id,
            chunk_id=graph.chunk.id,
            embedding_model=E5_SMALL_MODEL_KEY,
            embedding_dim=384,
            embedding=fake_vector("ok"),
        )
    )
