"""
tests/test_isolation.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Phase 1 exit criterion: tenant isolation test suite.

For every tenant-scoped table in TENANT_TABLES, this suite verifies:
  1. SELECT as tenant A returns only tenant A's rows (not tenant B's).
  2. UPDATE on tenant B's row (while in tenant A's context) affects 0 rows.
  3. DELETE on tenant B's row (while in tenant A's context) affects 0 rows.
  4. INSERT with tenant_id=B (while in tenant A's context) is rejected by the
     RLS WITH CHECK clause.
  5. A query run WITHOUT setting app.tenant_id returns 0 rows (fail-closed).

The suite is table-driven: add a new entry to TENANT_TABLES and the
isolation checks run automatically for the new table.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, time

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.db.models.academic import (
    Class,
    ClassSubject,
    Enrollment,
    Subject,
    TeacherAssignment,
)
from app.db.models.audit import AuditLog
from app.db.models.auth import EmailToken, Invitation, RefreshToken
from app.db.models.base import Base
from app.db.models.branding import TenantBranding
from app.db.models.document import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
    StorageDeletion,
)
from app.db.models.rbac import (
    Role,
    RolePermission,
    UserRole,
)
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY
from tests.fixtures.academic_world import AcademicWorld, create_document_graph

pytestmark = pytest.mark.asyncio

# ── Parents for the document tables ──────────────────────────────────────────
# The new tables have composite foreign keys, so each registry row needs real parents in the
# same tenant. These builders create the parents with the privileged session and return the
# column values of the row under test (the harness inserts that row itself).


async def _document_parents(raw_db: AsyncSession, tenant_id: uuid.UUID, depth: str) -> dict:
    """
    Create parents up to `depth`: "document", "version" or "chunk".
    Returns the ids as a dict: user, subject, class_, document, version, chunk.
    """
    ids: dict = {}
    user = User(
        tenant_id=tenant_id,
        email=f"iso-doc-{uuid.uuid4().hex}@example.com",
        full_name="Isolation Uploader",
        password_hash="x",
        status="active",
    )
    subject = Subject(tenant_id=tenant_id, name=f"iso-physics-{uuid.uuid4().hex[:8]}")
    klass = Class(tenant_id=tenant_id, name=f"iso-10a-{uuid.uuid4().hex[:8]}")
    raw_db.add_all([user, subject, klass])
    await raw_db.flush()
    raw_db.add(ClassSubject(tenant_id=tenant_id, class_id=klass.id, subject_id=subject.id))
    doc = Document(
        tenant_id=tenant_id, subject_id=subject.id, title="Physics syllabus", created_by=user.id
    )
    raw_db.add(doc)
    await raw_db.flush()
    ids.update(user=user.id, subject=subject.id, class_=klass.id, document=doc.id)
    if depth == "document":
        return ids

    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=doc.id,
        version_no=1,
        status="published",
        file_kind="pdf",
        original_filename="physics.pdf",
        size_bytes=2048,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        storage_key=f"t/{tenant_id}/d/{uuid.uuid4().hex}",
        uploaded_by=user.id,
    )
    raw_db.add(version)
    await raw_db.flush()
    ids["version"] = version.id
    if depth == "version":
        return ids

    chunk = Chunk(
        tenant_id=tenant_id,
        document_version_id=version.id,
        document_id=doc.id,
        subject_id=subject.id,
        ordinal=0,
        content="Unit 1: Newton's laws of motion.",
        content_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        token_count=8,
        script="latn",
    )
    raw_db.add(chunk)
    await raw_db.flush()
    ids["chunk"] = chunk.id
    return ids


async def _documents_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "document")
    return {"subject_id": p["subject"], "title": "Chemistry syllabus", "created_by": p["user"]}


async def _document_classes_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "document")
    return {"document_id": p["document"], "class_id": p["class_"], "subject_id": p["subject"]}


async def _document_versions_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "document")
    return {
        "document_id": p["document"],
        "version_no": 1,
        "status": "queued",
        "file_kind": "docx",
        "original_filename": "chem.docx",
        "size_bytes": 4096,
        "sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        "storage_key": f"t/{tenant_id}/d/{uuid.uuid4().hex}",
        "uploaded_by": p["user"],
    }


async def _chunks_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "version")
    return {
        "document_version_id": p["version"],
        "document_id": p["document"],
        "subject_id": p["subject"],
        "ordinal": 0,
        "heading_path": "Physics > Unit 2",
        "content": "Unit 2: work, energy and power.",
        "content_sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        "token_count": 9,
        "script": "latn",
    }


async def _chunk_embeddings_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "chunk")
    return {
        "chunk_id": p["chunk"],
        "embedding_model": E5_SMALL_MODEL_KEY,
        "embedding_dim": E5_SMALL_DIM,
        "embedding": [0.01] * E5_SMALL_DIM,
    }


async def _storage_deletions_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    return {"storage_key": f"t/{tenant_id}/d/{uuid.uuid4().hex}", "reason": "document_deleted"}


async def _ingest_jobs_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    p = await _document_parents(raw_db, tenant_id, "version")
    return {"version_id": p["version"], "status": "queued"}


# â”€â”€ Table registry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Each entry: (ORM model class, dict of extra columns needed for a valid INSERT).
# The test harness fills in tenant_id, id automatically.
TENANT_TABLES = [
    (User, {
        "email": lambda: f"iso-test-{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "Isolation Test User",
        "password_hash": "x",
        "status": "active",
    }),
    (RefreshToken, {
        "user_id": lambda: uuid.uuid4(),
        "token_hash": lambda: uuid.uuid4().hex,
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
    (Invitation, {
        "email": lambda: f"iso-inv-{uuid.uuid4().hex[:8]}@example.com",
        "token_hash": lambda: uuid.uuid4().hex,
        "invited_by": lambda: uuid.uuid4(),
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
    (EmailToken, {
        "user_id": lambda: uuid.uuid4(),
        "token_hash": lambda: uuid.uuid4().hex,
        "purpose": "verify_email",
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
    (Role, {
        "name": lambda: f"iso-role-{uuid.uuid4().hex[:8]}",
        "description": "Isolation Role",
        "is_system": False,
    }),
    (Class, {
        "name": lambda: f"iso-class-{uuid.uuid4().hex[:8]}",
        "kind": "section",
        "description": "Isolation Class",
    }),
    (Subject, {
        "name": lambda: f"iso-subject-{uuid.uuid4().hex[:8]}",
        "code": "ISO101",
        "description": "Isolation Subject",
    }),
    (AuditLog, {
        "action": "iso:audit_test",
        "target_type": "user",
        "details": {"test": True},
    }),
    (TenantBranding, {
        "tokens": lambda: {
            "primary": "#2563eb",
            "secondary": "#475569",
            "accent": "#06b6d4",
            "radius": "md",
            "font_family": "system-ui",
        },
        "display_name": "Isolation Branding",
    }),
    # Phase 5 document tables (migration 0008). `extra` is an async builder that creates the
    # parent rows (composite FKs) and returns the row values.
    (Document, _documents_row),
    (DocumentClass, _document_classes_row),
    (DocumentVersion, _document_versions_row),
    (Chunk, _chunks_row),
    (ChunkEmbedding, _chunk_embeddings_row),
    (StorageDeletion, _storage_deletions_row),
    (IngestJob, _ingest_jobs_row),
]


async def _build_row(
    model: type, tenant_id: uuid.UUID, extra: object, raw_db: AsyncSession
) -> dict:
    """
    Build the column values of one row for `tenant_id`.
    `extra` is either a dict (callables are resolved) or an async builder that creates parent
    rows with the privileged session and returns the values.
    """
    if callable(extra):
        resolved = await extra(raw_db, tenant_id)
    else:
        assert isinstance(extra, dict)
        resolved = {k: (v() if callable(v) else v) for k, v in extra.items()}
    resolved["tenant_id"] = tenant_id
    return resolved


# ── Guard: every ORM table with a tenant_id column must be covered ───────────
# Tables covered by a dedicated test instead of the generic TENANT_TABLES loop. Each entry
# names the test function that proves isolation and says why the generic loop does not fit.
CUSTOM_COVERAGE: dict[str, tuple[str, str]] = {
    "role_permissions": (
        "test_academic_and_rbac_junctions_isolation",
        "junction table with composite FKs to roles; covered with the other junctions",
    ),
    "user_roles": (
        "test_academic_and_rbac_junctions_isolation",
        "junction table with composite FKs to users and roles",
    ),
    "class_subjects": (
        "test_academic_and_rbac_junctions_isolation",
        "junction table with composite FKs to classes and subjects",
    ),
    "enrollments": (
        "test_academic_and_rbac_junctions_isolation",
        "junction table with composite FKs to users and classes",
    ),
    "teacher_assignments": (
        "test_academic_and_rbac_junctions_isolation",
        "junction table with composite FKs to users and class_subjects",
    ),
    "timetables": (
        "test_timetables_and_entries_isolation",
        "needs a class parent and has partial unique indexes on status",
    ),
    "timetable_entries": (
        "test_timetables_and_entries_isolation",
        "needs timetable, class, subject and teacher parents",
    ),
}


def find_unregistered_tenant_tables(
    metadata: object, registered: set[str], custom: dict[str, tuple[str, str]]
) -> list[str]:
    """Names of mapped tables that have a tenant_id column but no isolation coverage."""
    tables = getattr(metadata, "tables")  # noqa: B009
    return sorted(
        t.name
        for t in tables.values()
        if "tenant_id" in t.c and t.name not in registered and t.name not in custom
    )


# â”€â”€ 1. SELECT isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_select_returns_only_own_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    Rows inserted for tenant B must NOT appear in a session locked to tenant A.
    """
    # Insert a row for tenant B using the privileged (raw, no-RLS) session.
    row_b = await _build_row(model, tenant_b.id, extra, raw_db)
    await raw_db.execute(insert(model).values(**row_b))
    # Commit so the row is visible to the other (RLS-restricted) connection; without this the
    # test would pass even with RLS switched off, because uncommitted rows are invisible anyway.
    await raw_db.commit()

    # SELECT through tenant A's RLS context.
    result = await tenant_a_session.execute(select(model))
    rows = result.scalars().all()

    for row in rows:
        assert row.tenant_id == tenant_a.id, (
            f"{model.__tablename__}: got row with tenant_id={row.tenant_id!r} "
            f"(belongs to tenant B) in tenant A's session."
        )


# â”€â”€ 2. UPDATE isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_update_cannot_affect_other_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    An UPDATE in tenant A's context must not touch tenant B's rows.
    """
    row_b = await _build_row(model, tenant_b.id, extra, raw_db)
    await raw_db.execute(insert(model).values(**row_b))
    # Commit so the row is visible to the other (RLS-restricted) connection; without this the
    # test would pass even with RLS switched off, because uncommitted rows are invisible anyway.
    await raw_db.commit()

    # Attempt UPDATE from tenant A's session: it must match 0 rows because RLS hides tenant B's
    # rows, or fail with permission denied if the table is append-only (like audit_log).
    # The predicate is on tenant_id, which every tenant table has (the old one compared the
    # first primary key column, tenant_id, with an id, so it could never match anything).
    try:
        update_result = await tenant_a_session.execute(
            update(model).where(model.tenant_id == tenant_b.id).values(tenant_id=tenant_a.id)
        )
        assert update_result.rowcount == 0, (
            f"{model.__tablename__}: UPDATE in tenant A's context affected "
            f"{update_result.rowcount} row(s) belonging to tenant B."
        )
    except Exception as exc:
        assert any(kw in str(exc).lower() for kw in ("permission denied", "policy", "violat"))


# â”€â”€ 3. DELETE isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_delete_cannot_affect_other_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    DELETE in tenant A's context must leave tenant B's rows untouched.
    """
    row_b = await _build_row(model, tenant_b.id, extra, raw_db)
    await raw_db.execute(insert(model).values(**row_b))
    # Commit so the row is visible to the other (RLS-restricted) connection; without this the
    # test would pass even with RLS switched off, because uncommitted rows are invisible anyway.
    await raw_db.commit()

    try:
        del_result = await tenant_a_session.execute(
            delete(model).where(model.tenant_id == tenant_b.id)
        )
        assert del_result.rowcount == 0, (
            f"{model.__tablename__}: DELETE in tenant A's context removed "
            f"{del_result.rowcount} row(s) belonging to tenant B."
        )
    except Exception as exc:
        assert any(kw in str(exc).lower() for kw in ("permission denied", "policy", "violat"))


# â”€â”€ 4. INSERT WITH CHECK â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_insert_wrong_tenant_id_rejected_by_rls(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    Inserting a row with tenant_id=B while in tenant A's RLS context must
    be rejected by the WITH CHECK clause (raises ProgrammingError / check violation).
    The parents exist in tenant B, so only RLS (not a missing foreign key) can reject the row.
    """
    row = await _build_row(model, tenant_b.id, extra, raw_db)
    with pytest.raises(Exception) as exc_info:
        await tenant_a_session.execute(insert(model).values(**row))
        # Force the error to surface if it's deferred.
        await tenant_a_session.flush()

    error_str = str(exc_info.value).lower()
    assert any(
        kw in error_str
        for kw in ("policy", "check", "violat", "new row", "rls")
    ), (
        f"{model.__tablename__}: Expected RLS WITH CHECK violation but got: "
        f"{exc_info.value}"
    )


# â”€â”€ 5. Fail-closed: query without tenant_id returns nothing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_query_without_tenant_id_returns_no_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
) -> None:
    """
    A session that never calls set_config('app.tenant_id', ...) must see zero
    rows â€” the system fails closed, not open.

    We use a fresh session connected as app_tenant (which has NOBYPASSRLS).
    We deliberately do NOT set the tenant_id config.
    """
    # Insert a row for tenant A via raw (no-RLS) session so there's data to miss.
    row = await _build_row(model, tenant_a.id, extra, raw_db)
    await raw_db.execute(insert(model).values(**row))
    # The rows are inserted by the privileged session; make them visible to the new session.
    await raw_db.commit()

    from app.db.engine import SessionLocal

    async with SessionLocal() as session, session.begin():
        # No set_config call here â€” tenant context is absent.
        result = await session.execute(select(model))
        rows = result.scalars().all()

    assert rows == [], (
        f"{model.__tablename__}: Expected 0 rows with no tenant_id set "
        f"but got {len(rows)} row(s). RLS is NOT fail-closed!"
    )


# â”€â”€ 6. Composite Junction Tables Isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def test_academic_and_rbac_junctions_isolation(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
    tenant_b_session: AsyncSession,
    user_a: User,
    user_b: User,
) -> None:
    """
    Verify that junction tables (RolePermission, UserRole, ClassSubject, Enrollment,
    TeacherAssignment) enforce tenant isolation and RLS boundaries.
    """
    # 1. Create parents for Tenant A
    role_a = Role(tenant_id=tenant_a.id, name=f"role-a-{uuid.uuid4().hex[:6]}", is_system=False)
    class_a = Class(tenant_id=tenant_a.id, name=f"class-a-{uuid.uuid4().hex[:6]}")
    subject_a = Subject(tenant_id=tenant_a.id, name=f"sub-a-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([role_a, class_a, subject_a])
    await raw_db.flush()

    # 2. Create parents for Tenant B
    role_b = Role(tenant_id=tenant_b.id, name=f"role-b-{uuid.uuid4().hex[:6]}", is_system=False)
    class_b = Class(tenant_id=tenant_b.id, name=f"class-b-{uuid.uuid4().hex[:6]}")
    subject_b = Subject(tenant_id=tenant_b.id, name=f"sub-b-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([role_b, class_b, subject_b])
    await raw_db.flush()

    # 3. Create junctions for Tenant A
    rp_a = RolePermission(tenant_id=tenant_a.id, role_id=role_a.id, permission_id="chat:use")
    ur_a = UserRole(tenant_id=tenant_a.id, user_id=user_a.id, role_id=role_a.id)
    cs_a = ClassSubject(tenant_id=tenant_a.id, class_id=class_a.id, subject_id=subject_a.id)
    raw_db.add_all([rp_a, ur_a, cs_a])
    await raw_db.flush()

    enr_a = Enrollment(tenant_id=tenant_a.id, user_id=user_a.id, class_id=class_a.id)
    ta_a = TeacherAssignment(
        tenant_id=tenant_a.id, user_id=user_a.id, class_id=class_a.id, subject_id=subject_a.id
    )
    raw_db.add_all([enr_a, ta_a])

    # 4. Create junctions for Tenant B
    rp_b = RolePermission(tenant_id=tenant_b.id, role_id=role_b.id, permission_id="chat:use")
    ur_b = UserRole(tenant_id=tenant_b.id, user_id=user_b.id, role_id=role_b.id)
    cs_b = ClassSubject(tenant_id=tenant_b.id, class_id=class_b.id, subject_id=subject_b.id)
    raw_db.add_all([rp_b, ur_b, cs_b])
    await raw_db.flush()

    enr_b = Enrollment(tenant_id=tenant_b.id, user_id=user_b.id, class_id=class_b.id)
    ta_b = TeacherAssignment(
        tenant_id=tenant_b.id, user_id=user_b.id, class_id=class_b.id, subject_id=subject_b.id
    )
    raw_db.add_all([enr_b, ta_b])
    await raw_db.commit()

    # Query as Tenant A: must ONLY see Tenant A's junction records
    res_enr = await tenant_a_session.execute(select(Enrollment))
    enr_rows = res_enr.scalars().all()
    assert len(enr_rows) >= 1
    assert all(e.tenant_id == tenant_a.id for e in enr_rows)
    assert not any(e.tenant_id == tenant_b.id for e in enr_rows)

    res_ta = await tenant_a_session.execute(select(TeacherAssignment))
    ta_rows = res_ta.scalars().all()
    assert len(ta_rows) >= 1
    assert all(t.tenant_id == tenant_a.id for t in ta_rows)
    assert not any(t.tenant_id == tenant_b.id for t in ta_rows)

    res_cs = await tenant_a_session.execute(select(ClassSubject))
    cs_rows = res_cs.scalars().all()
    assert all(c.tenant_id == tenant_a.id for c in cs_rows)
    assert not any(c.tenant_id == tenant_b.id for c in cs_rows)

    # Cross-tenant insert attempt in Tenant A's session using Tenant B's ID must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(Enrollment).values(
                tenant_id=tenant_b.id,
                user_id=user_b.id,
                class_id=class_b.id,
            )
        )
        await tenant_a_session.flush()


async def test_timetables_and_entries_isolation(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
    user_a: User,
    user_b: User,
) -> None:
    """
    Verify that Timetable and TimetableEntry enforce tenant isolation and RLS boundaries:
      1. SELECT as tenant A returns only tenant A's timetables and entries.
      2. INSERT with tenant_id=B while in tenant A's context fails with RLS check.
    """
    # 1. Parents for Tenant A
    class_a = Class(tenant_id=tenant_a.id, name=f"class-tt-a-{uuid.uuid4().hex[:6]}")
    subject_a = Subject(tenant_id=tenant_a.id, name=f"sub-tt-a-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([class_a, subject_a])
    await raw_db.flush()

    tt_a = Timetable(
        tenant_id=tenant_a.id,
        class_id=class_a.id,
        academic_year="2026-2027",
        term="Fall",
        status="published",
        version=1,
    )
    raw_db.add(tt_a)
    await raw_db.flush()

    entry_a = TimetableEntry(
        tenant_id=tenant_a.id,
        timetable_id=tt_a.id,
        class_id=class_a.id,
        subject_id=subject_a.id,
        teacher_id=user_a.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
        room="Room 101",
    )
    raw_db.add(entry_a)

    # 2. Parents for Tenant B
    class_b = Class(tenant_id=tenant_b.id, name=f"class-tt-b-{uuid.uuid4().hex[:6]}")
    subject_b = Subject(tenant_id=tenant_b.id, name=f"sub-tt-b-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([class_b, subject_b])
    await raw_db.flush()

    tt_b = Timetable(
        tenant_id=tenant_b.id,
        class_id=class_b.id,
        academic_year="2026-2027",
        term="Fall",
        status="published",
        version=1,
    )
    raw_db.add(tt_b)
    await raw_db.flush()

    entry_b = TimetableEntry(
        tenant_id=tenant_b.id,
        timetable_id=tt_b.id,
        class_id=class_b.id,
        subject_id=subject_b.id,
        teacher_id=user_b.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
        room="Room 202",
    )
    raw_db.add(entry_b)
    await raw_db.commit()

    # 3. SELECT as Tenant A: must ONLY see Tenant A's records
    res_tt = await tenant_a_session.execute(select(Timetable))
    tt_rows = res_tt.scalars().all()
    assert len(tt_rows) >= 1
    assert all(t.tenant_id == tenant_a.id for t in tt_rows)
    assert not any(t.tenant_id == tenant_b.id for t in tt_rows)

    res_entry = await tenant_a_session.execute(select(TimetableEntry))
    entry_rows = res_entry.scalars().all()
    assert len(entry_rows) >= 1
    assert all(e.tenant_id == tenant_a.id for e in entry_rows)
    assert not any(e.tenant_id == tenant_b.id for e in entry_rows)

    # 4. Cross-tenant INSERT into Timetable inside Tenant A's session must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(Timetable).values(
                tenant_id=tenant_b.id,
                class_id=class_b.id,
                academic_year="2026-2027",
                term="Spring",
                status="draft",
                version=1,
            )
        )
        await tenant_a_session.flush()

    # 5. Cross-tenant INSERT into TimetableEntry inside Tenant A's session must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(TimetableEntry).values(
                tenant_id=tenant_b.id,
                timetable_id=tt_b.id,
                class_id=class_b.id,
                subject_id=subject_b.id,
                day_of_week=1,
                start_time=time(11, 0),
                end_time=time(12, 0),
            )
        )
        await tenant_a_session.flush()


async def test_archived_timetables_isolation(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """Archived history follows the same RLS rules: invisible and immutable across tenants."""
    class_b = Class(tenant_id=tenant_b.id, name=f"class-arch-b-{uuid.uuid4().hex[:6]}")
    raw_db.add(class_b)
    await raw_db.flush()
    archived_b = Timetable(tenant_id=tenant_b.id, class_id=class_b.id, status="archived", version=1)
    published_b = Timetable(tenant_id=tenant_b.id, class_id=class_b.id, status="published", version=2)
    raw_db.add_all([archived_b, published_b])
    await raw_db.commit()

    res = await tenant_a_session.execute(select(Timetable).where(Timetable.status == "archived"))
    assert all(t.tenant_id == tenant_a.id for t in res.scalars().all())

    # Tenant A cannot touch tenant B's archived or published rows: RLS hides them, 0 rows change.
    upd = await tenant_a_session.execute(
        update(Timetable).where(Timetable.tenant_id == tenant_b.id).values(status="draft")
    )
    assert upd.rowcount == 0
    dele = await tenant_a_session.execute(delete(Timetable).where(Timetable.tenant_id == tenant_b.id))
    assert dele.rowcount == 0

    rows = (
        await raw_db.execute(select(Timetable.status).where(Timetable.tenant_id == tenant_b.id, Timetable.class_id == class_b.id))
    ).scalars().all()
    assert sorted(rows) == ["archived", "published"]


# ── 7. Guard: no tenant table may escape the isolation tests ─────────────────

NEW_DOCUMENT_TABLES = {
    "documents",
    "document_classes",
    "document_versions",
    "chunks",
    "chunk_embeddings",
    "storage_deletions",
    "ingest_jobs",
}


def _orm_tenant_table_names() -> list[str]:
    return sorted(t.name for t in Base.metadata.tables.values() if "tenant_id" in t.c)


async def test_guard_every_tenant_table_is_registered_or_explained() -> None:
    """
    GUARD: every mapped table with a tenant_id column must be in TENANT_TABLES or in
    CUSTOM_COVERAGE (with the name of its dedicated test and a written reason).
    A new tenant table that is neither fails here, so isolation can never be forgotten.
    """
    registered = {m.__tablename__ for m, _ in TENANT_TABLES}
    missing = find_unregistered_tenant_tables(Base.metadata, registered, CUSTOM_COVERAGE)
    assert not missing, (
        f"Tenant tables without isolation coverage: {missing}. Add them to TENANT_TABLES in "
        "tests/test_isolation.py, or to CUSTOM_COVERAGE with a dedicated test and a reason."
    )

    # The allow-list itself must stay honest.
    known = set(_orm_tenant_table_names())
    for table, (test_name, reason) in CUSTOM_COVERAGE.items():
        assert table in known, f"CUSTOM_COVERAGE names unknown table {table!r}"
        assert table not in registered, f"{table!r} is both registered and allow-listed"
        assert reason.strip(), f"CUSTOM_COVERAGE[{table!r}] needs a written reason"
        assert callable(globals().get(test_name)), (
            f"CUSTOM_COVERAGE[{table!r}] points at {test_name!r}, which does not exist"
        )
    assert registered <= known
    assert registered >= NEW_DOCUMENT_TABLES


async def test_guard_detects_an_unregistered_tenant_table() -> None:
    """Prove-it-fails: the guard reports a new tenant table that nobody registered."""
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    md = MetaData()
    Table("rogue_notes", md, Column("tenant_id", PG_UUID, primary_key=True))
    Table("platform_only", md, Column("id", PG_UUID, primary_key=True))  # no tenant_id: ignored
    assert find_unregistered_tenant_tables(md, set(), {}) == ["rogue_notes"]
    assert find_unregistered_tenant_tables(md, {"rogue_notes"}, {}) == []
    assert find_unregistered_tenant_tables(md, set(), {"rogue_notes": ("t", "reason")}) == []


async def test_every_tenant_table_has_forced_rls_and_the_isolation_policy(
    raw_db: AsyncSession,
) -> None:
    """ENABLE + FORCE RLS and a tenant_isolation policy using current_setting, on every table."""
    names = _orm_tenant_table_names()
    rows = (
        await raw_db.execute(
            text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       EXISTS (
                           SELECT 1 FROM pg_policies p
                           WHERE p.schemaname = 'app' AND p.tablename = c.relname
                             AND p.policyname = 'tenant_isolation'
                             AND p.qual LIKE '%current_setting%'
                             AND p.with_check LIKE '%current_setting%')
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'app' AND c.relname = ANY(:names)
            """),
            {"names": names},
        )
    ).all()
    assert {r[0] for r in rows} == set(names), "a mapped tenant table is missing in the database"
    bad = [r[0] for r in rows if not (r[1] and r[2] and r[3])]
    assert not bad, f"RLS not enabled+forced with a tenant_isolation policy on: {bad}"


async def test_application_roles_cannot_bypass_rls(raw_db: AsyncSession) -> None:
    """app_tenant, app_platform and app_worker are plain roles: no superuser, no BYPASSRLS."""
    rows = (
        await raw_db.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname IN ('app_tenant', 'app_platform', 'app_worker')"
            )
        )
    ).all()
    assert {r[0] for r in rows} == {"app_tenant", "app_platform", "app_worker"}
    for name, is_super, bypass in rows:
        assert not is_super, f"{name} must not be a superuser"
        assert not bypass, f"{name} must not have BYPASSRLS"


# ── 8. app_tenant cannot see or touch another tenant's jobs ─────────────────


async def test_app_tenant_cannot_see_or_change_other_tenants_jobs(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    from app.core.tenant_db import tenant_session_context

    wa, wb = academic_world.a, academic_world.b
    ga = await create_document_graph(
        raw_db, wa, subject_name="Physics", class_names=("10-A",), uploader=wa.t1
    )
    gb = await create_document_graph(
        raw_db, wb, subject_name="Physics", class_names=("10-A",), uploader=wb.t1
    )
    await raw_db.commit()

    async with tenant_session_context(str(wa.id)) as session:
        seen = {j.id for j in (await session.execute(select(IngestJob))).scalars().all()}
        assert ga.job.id in seen
        assert gb.job.id not in seen
        upd = await session.execute(
            update(IngestJob).where(IngestJob.id == gb.job.id).values(status="failed")
        )
        assert upd.rowcount == 0
        dele = await session.execute(delete(IngestJob).where(IngestJob.id == gb.job.id))
        assert dele.rowcount == 0

    status = (
        await raw_db.execute(select(IngestJob.status).where(IngestJob.id == gb.job.id))
    ).scalar_one()
    assert status == "queued"


# ── 9. The app_worker role: job metadata only ────────────────────────────────


@pytest_asyncio.fixture
async def worker_engine() -> AsyncGenerator[AsyncEngine, None]:
    from app.core.config import settings

    assert settings.WORKER_DATABASE_URL, "WORKER_DATABASE_URL is not set"
    engine = create_async_engine(
        settings.WORKER_DATABASE_URL, connect_args={"statement_cache_size": 0}
    )
    yield engine
    await engine.dispose()


async def _worker_run(
    engine: AsyncEngine, sql: str, params: dict | None = None, *, commit: bool = False
) -> list:
    """Run one statement as app_worker. Returns the rows (or [rowcount] for non-SELECT)."""
    async with engine.connect() as conn:
        res = await conn.execute(text(sql), params or {})
        out = list(res.fetchall()) if res.returns_rows else [res.rowcount]
        if commit:
            await conn.commit()
        else:
            await conn.rollback()
        return out


async def _assert_worker_denied(engine: AsyncEngine, sql: str, params: dict | None = None) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        await _worker_run(engine, sql, params)
    assert "permission denied" in str(exc_info.value).lower(), (
        f"expected 'permission denied' for: {sql}; got: {exc_info.value}"
    )


async def _two_jobs(raw_db: AsyncSession, world: AcademicWorld) -> tuple[uuid.UUID, uuid.UUID]:
    ga = await create_document_graph(
        raw_db, world.a, subject_name="Math", class_names=("10-A",), uploader=world.a.t1
    )
    gb = await create_document_graph(
        raw_db, world.b, subject_name="Chemistry", class_names=("11-A",), uploader=world.b.t2
    )
    await raw_db.commit()
    return ga.job.id, gb.job.id


async def test_app_worker_cannot_select_any_tenant_data_table(worker_engine: AsyncEngine) -> None:
    """Besides ingest_jobs, the worker has NO grant on any tenant table (documents, chunks, users ...)."""
    names = [n for n in _orm_tenant_table_names() if n != "ingest_jobs"]
    assert {"documents", "chunks", "users"} <= set(names)
    for name in names:
        await _assert_worker_denied(worker_engine, f"SELECT 1 FROM app.{name} LIMIT 1")
    # Platform and catalog tables are closed as well.
    for name in ("platform_admins", "permissions"):
        await _assert_worker_denied(worker_engine, f"SELECT 1 FROM app.{name} LIMIT 1")


async def test_app_worker_sees_only_tenant_ids_not_tenant_columns(
    raw_db: AsyncSession, academic_world: AcademicWorld, worker_engine: AsyncEngine
) -> None:
    """tenants has no RLS: the worker may list ids (for the sweeper) but not db_dsn or the slug."""
    ids = {r[0] for r in await _worker_run(worker_engine, "SELECT id FROM app.tenants")}
    assert {academic_world.a.id, academic_world.b.id} <= ids
    await _assert_worker_denied(worker_engine, "SELECT db_dsn FROM app.tenants")
    await _assert_worker_denied(worker_engine, "SELECT slug FROM app.tenants")
    await _assert_worker_denied(worker_engine, "SELECT * FROM app.tenants")
    await _assert_worker_denied(worker_engine, "SELECT doc_limits FROM app.tenants")
    await _assert_worker_denied(
        worker_engine, "UPDATE app.tenants SET timezone = 'UTC' WHERE false"
    )


async def test_app_worker_sees_job_metadata_across_tenants_and_nothing_else(
    raw_db: AsyncSession, academic_world: AcademicWorld, worker_engine: AsyncEngine
) -> None:
    job_a, job_b = await _two_jobs(raw_db, academic_world)
    async with worker_engine.connect() as conn:
        res = await conn.execute(
            text("SELECT * FROM app.ingest_jobs WHERE id = ANY(:ids)"), {"ids": [job_a, job_b]}
        )
        rows = res.mappings().all()
        columns = set(res.keys())
        await conn.rollback()
    assert {r["tenant_id"] for r in rows} == {academic_world.a.id, academic_world.b.id}
    # Job metadata only: ids, status, lease and retry fields. No file names, text or user data.
    assert columns == {
        "tenant_id", "id", "version_id", "status", "attempts", "run_after",
        "lease_expires_at", "worker_id", "last_error_code", "created_at", "updated_at",
    }


async def test_app_worker_can_claim_and_update_jobs(
    raw_db: AsyncSession, academic_world: AcademicWorld, worker_engine: AsyncEngine
) -> None:
    job_a, job_b = await _two_jobs(raw_db, academic_world)

    # The claim statement of design A3 (restricted to this test's jobs so it cannot take
    # unrelated queued rows from other tests).
    claim = """
        WITH next AS (
          SELECT id FROM app.ingest_jobs
          WHERE ((status = 'queued' AND run_after <= now())
              OR (status = 'running' AND lease_expires_at < now()))
            AND attempts < 3 AND id = ANY(:ids)
          ORDER BY run_after, created_at LIMIT 1
          FOR UPDATE SKIP LOCKED)
        UPDATE app.ingest_jobs j
        SET status = 'running', attempts = j.attempts + 1,
            lease_expires_at = now() + interval '120 seconds',
            worker_id = :worker, updated_at = now()
        FROM next WHERE j.id = next.id
        RETURNING j.id, j.tenant_id, j.version_id, j.attempts
    """
    claimed = await _worker_run(
        worker_engine, claim, {"ids": [job_a, job_b], "worker": "w-1"}, commit=True
    )
    assert len(claimed) == 1
    claimed_id, claimed_tenant, _version, attempts = claimed[0]
    assert claimed_id in (job_a, job_b)
    assert claimed_tenant in (academic_world.a.id, academic_world.b.id)
    assert attempts == 1

    # Every column in the grant can be updated (lease, retry and error fields).
    updated = await _worker_run(
        worker_engine,
        """
        UPDATE app.ingest_jobs
        SET status = 'queued', attempts = 2, run_after = now() + interval '30 seconds',
            lease_expires_at = NULL, worker_id = NULL, last_error_code = 'worker_crash',
            updated_at = now()
        WHERE id = :id
        """,
        {"id": claimed_id},
        commit=True,
    )
    assert updated == [1]

    # ... and a finished job can be deleted.
    await _worker_run(
        worker_engine, "UPDATE app.ingest_jobs SET status = 'done' WHERE id = :id",
        {"id": claimed_id}, commit=True,
    )
    assert await _worker_run(
        worker_engine, "DELETE FROM app.ingest_jobs WHERE id = :id", {"id": claimed_id}, commit=True
    ) == [1]


async def test_two_workers_never_claim_the_same_job(
    raw_db: AsyncSession, academic_world: AcademicWorld, worker_engine: AsyncEngine
) -> None:
    """FOR UPDATE SKIP LOCKED works with the worker's privileges: a locked job is skipped."""
    job_a, _ = await _two_jobs(raw_db, academic_world)
    async with worker_engine.connect() as first, worker_engine.connect() as second:
        locked = await first.execute(
            text("SELECT id FROM app.ingest_jobs WHERE id = :id FOR UPDATE SKIP LOCKED"),
            {"id": job_a},
        )
        assert [r[0] for r in locked.fetchall()] == [job_a]
        other = await second.execute(
            text("SELECT id FROM app.ingest_jobs WHERE id = :id FOR UPDATE SKIP LOCKED"),
            {"id": job_a},
        )
        assert other.fetchall() == []
        await first.rollback()
        await second.rollback()


async def test_app_worker_cannot_update_columns_outside_its_grant(
    raw_db: AsyncSession, academic_world: AcademicWorld, worker_engine: AsyncEngine
) -> None:
    job_a, _ = await _two_jobs(raw_db, academic_world)
    for column in ("id", "tenant_id", "version_id", "created_at"):
        await _assert_worker_denied(
            worker_engine,
            f"UPDATE app.ingest_jobs SET {column} = {column} WHERE id = :id",
            {"id": job_a},
        )
    # No INSERT either: the API creates jobs in the same transaction as the version.
    await _assert_worker_denied(
        worker_engine,
        "INSERT INTO app.ingest_jobs (tenant_id, version_id) VALUES (:t, :v)",
        {"t": academic_world.a.id, "v": uuid.uuid4()},
    )
    # And no writes to the other tables.
    await _assert_worker_denied(
        worker_engine, "UPDATE app.document_versions SET status = 'failed' WHERE false"
    )
    await _assert_worker_denied(worker_engine, "DELETE FROM app.chunks WHERE false")


# ── 10. Composite FKs and partial unique indexes of the document tables ──────


async def _expect_db_error(
    db: AsyncSession, stmt: object, constraint: str
) -> None:
    """Run `stmt` in a savepoint and assert it fails because of the named constraint/index."""
    with pytest.raises(IntegrityError) as exc_info:
        async with db.begin_nested():
            await db.execute(stmt)  # type: ignore[arg-type]
    assert constraint in str(exc_info.value), f"expected {constraint}, got: {exc_info.value}"


async def test_document_class_fk_rejects_a_pair_the_class_does_not_offer(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A",), uploader=w.t1
    )
    doc = graph.document
    physics = w.subjects["Physics"].id

    # 11-A offers Chemistry and English only, so a Physics document cannot be linked to it.
    await _expect_db_error(
        raw_db,
        insert(DocumentClass).values(
            tenant_id=w.id, document_id=doc.id, class_id=w.classes["11-A"].id, subject_id=physics
        ),
        "fk_document_classes_class_subject",
    )
    # The link must carry the document's own subject (Math is offered in 10-B, but is not the
    # subject of this document).
    await _expect_db_error(
        raw_db,
        insert(DocumentClass).values(
            tenant_id=w.id,
            document_id=doc.id,
            class_id=w.classes["10-B"].id,
            subject_id=w.subjects["Math"].id,
        ),
        "fk_document_classes_document",
    )
    # Control: 10-B does offer Physics, so one shared document can serve both classes.
    await raw_db.execute(
        insert(DocumentClass).values(
            tenant_id=w.id, document_id=doc.id, class_id=w.classes["10-B"].id, subject_id=physics
        )
    )


async def test_document_versions_allow_one_published_and_one_in_flight(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Math", class_names=("10-A",), uploader=w.t1, status="published"
    )
    doc_id = graph.document.id
    counter = iter(range(2, 100))

    def version(status: str) -> object:
        n = next(counter)
        return insert(DocumentVersion).values(
            tenant_id=w.id,
            document_id=doc_id,
            version_no=n,
            status=status,
            file_kind="pdf",
            original_filename="v.pdf",
            size_bytes=10,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            storage_key=f"t/{w.id}/d/{uuid.uuid4().hex}",
            uploaded_by=w.t1.id,
        )

    # A second published version is rejected.
    await _expect_db_error(raw_db, version("published"), "uq_document_versions_one_published")

    # One in-flight draft is fine; a second one (queued, processing or ready) is not.
    await raw_db.execute(version("queued"))
    for status in ("queued", "processing", "ready"):
        await _expect_db_error(raw_db, version(status), "uq_document_versions_one_in_flight")

    # History and failures are unlimited.
    for status in ("failed", "failed", "archived", "archived", "archived"):
        await raw_db.execute(version(status))

    # Another document has its own published and in-flight versions.
    other = await create_document_graph(
        raw_db, w, subject_name="English", class_names=("10-A",), uploader=w.t1, status="published"
    )
    assert other.document.id != doc_id

    # Once the draft is no longer in flight, a new one is accepted again.
    await raw_db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id, DocumentVersion.status == "queued")
        .values(status="failed")
    )
    await raw_db.execute(version("queued"))
