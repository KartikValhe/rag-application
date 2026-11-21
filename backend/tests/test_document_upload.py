"""
tests/test_document_upload.py
─────────────────────────────
POST /api/v1/documents and POST /api/v1/documents/{document_id}/versions through the REAL app
(design A3 steps 1-8): scope, rate limits, the tenant's own file and page limits, validation,
duplicates, quotas, the private store and the single transaction with its compensating delete.

A rejected upload must leave NO stored file and NO rows.
"""

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.document import DocumentClass, DocumentVersion, IngestJob
from app.main import app
from app.services import document_store
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import NO_ROWS, TXT, make_document, replace, row_counts, upload
from tests.security.document_assertions import (
    assert_error_code,
    assert_generic_404,
    assert_no_stored_files,
    assert_quota_refused,
    assert_store_matches_rows,
    stored_files,
)
from tests.security.upload_assertions import good_docx, make_pdf

pytestmark = pytest.mark.asyncio

VERSION_KEYS = {
    "id", "document_id", "version_no", "status", "stage", "error_code", "file_kind", "original_filename",
    "size_bytes", "page_count", "chunk_count", "flagged_chunk_count", "created_at", "published_at", "retryable",
    "warnings",
}


async def _set_limits(raw_db: AsyncSession, w: WorldTenant, **limits: int) -> None:
    import json

    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": json.dumps(limits), "id": w.id},
    )
    await raw_db.commit()


async def _keys(raw_db: AsyncSession, w: WorldTenant) -> list[str]:
    return list(
        (await raw_db.execute(select(DocumentVersion.storage_key).where(DocumentVersion.tenant_id == w.id))).scalars()
    )


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_teacher_uploads_one_document_for_two_of_his_classes(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    resp = await upload(client, w, w.t1, subject="Math", classes=("10-A", "10-B"), filename="../../Unit 1?.txt")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert set(body) == VERSION_KEYS
    assert body["status"] == "queued" and body["version_no"] == 1 and body["stage"] is None
    assert body["file_kind"] == "txt" and body["size_bytes"] == len(TXT) and body["warnings"] == []
    assert body["original_filename"] == "Unit 1_.txt"  # sanitize_filename output

    version = (
        await raw_db.execute(select(DocumentVersion).where(DocumentVersion.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert version.tenant_id == w.id and version.status == "queued"
    assert version.sha256 == hashlib.sha256(TXT).hexdigest()
    assert version.storage_key.startswith(f"t/{w.id}/d/") and version.storage_key not in resp.text
    links = (
        await raw_db.execute(select(DocumentClass.class_id).where(DocumentClass.document_id == version.document_id))
    ).scalars()
    assert set(links) == {w.classes["10-A"].id, w.classes["10-B"].id}
    job = (await raw_db.execute(select(IngestJob).where(IngestJob.version_id == version.id))).scalar_one()
    assert (job.status, job.attempts, job.worker_id) == ("queued", 0, None)
    audit = (
        await raw_db.execute(
            select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "document:upload")
        )
    ).scalar_one()
    assert audit.actor_id == w.t1.id and audit.target_id == str(version.document_id)
    assert audit.details["version_id"] == str(version.id)
    # The original is in the PRIVATE store, byte for byte, under the tenant prefix.
    assert (document_store_root / version.storage_key).read_bytes() == TXT
    assert_store_matches_rows(document_store_root, [version.storage_key])


# ── Scope (A4) ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("subject", "classes"),
    [("Physics", ("10-B",)), ("Physics", ("10-A", "10-B")), ("Math", ("11-A",)), ("Chemistry", ("11-A",))],
)
async def test_teacher_cannot_upload_outside_his_pairs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    subject: str, classes: tuple[str, ...],
) -> None:
    w = academic_world.a
    resp = await upload(client, w, w.t1, subject=subject, classes=classes)
    assert_generic_404(resp)
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_class_of_another_tenant_is_a_generic_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    resp = await upload(client, a, a.admin, classes=("10-A",), extra_class_ids=(b.classes["10-B"].id,))
    assert_generic_404(resp)
    assert await row_counts(raw_db, a.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_students_and_view_only_users_cannot_upload(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    for user in (w.s1, w.viewer):
        resp = await upload(client, w, user)
        assert_error_code(resp, 403, "permission_denied")
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_admin_uploads_for_any_offered_pair(
    client: AsyncClient, academic_world: AcademicWorld, document_store_root: Path
) -> None:
    w = academic_world.a
    resp = await upload(client, w, w.admin, subject="Physics", classes=("10-A", "10-B"))
    assert resp.status_code == 202, resp.text


# ── Hostile uploads: stable codes, nothing stored, no rows ───────────────────


HOSTILE: list[tuple[str, bytes, str, int, str]] = [
    ("macro docx", good_docx({"word/vbaProject.bin": b"macro"}), "evil.docx", 422, "macro_enabled_file"),
    ("renamed exe", b"MZ\x90\x00" + b"\x00" * 64, "syllabus.pdf", 422, "disallowed_file_type"),
    ("wrong extension", make_pdf(), "syllabus.txt", 422, "file_type_mismatch"),
    ("csv", b"a,b\n1,2\n", "marks.csv", 422, "unsupported_format"),
    ("not utf-8", "Grüße".encode("latin-1"), "notes.txt", 422, "invalid_encoding"),
    ("empty", b"", "notes.txt", 422, "empty_file"),
    ("pdf with javascript", make_pdf(b"/JavaScript (x)"), "a.pdf", 422, "pdf_active_content"),
]


@pytest.mark.parametrize(("label", "data", "filename", "status", "code"), HOSTILE, ids=[h[0] for h in HOSTILE])
async def test_hostile_uploads_get_stable_codes_and_leave_nothing(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    label: str, data: bytes, filename: str, status: int, code: str,
) -> None:
    w = academic_world.a
    resp = await upload(client, w, w.t1, data=data, filename=filename)
    assert_error_code(resp, status, code)
    assert set(resp.json()["detail"]) == {"code", "message"}
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_file_over_the_tenants_own_limit_is_413_and_leaves_nothing(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, max_file_bytes=1000)
    resp = await upload(client, w, w.t1, data=b"a" * 1001)
    assert_error_code(resp, 413, "file_too_large")
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)
    # The same file is fine for a tenant on the default limit (the override is per tenant).
    b = academic_world.b
    assert (await upload(client, b, b.t1, data=b"a" * 1001)).status_code == 202


async def test_pdf_over_the_tenants_page_limit_is_too_many_pages(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, max_pdf_pages=2)
    resp = await upload(client, w, w.t1, data=make_pdf(pages=3), filename="s.pdf")
    assert_error_code(resp, 422, "too_many_pages")
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)
    assert (await upload(client, w, w.t1, data=make_pdf(pages=2), filename="s.pdf")).status_code == 202


async def test_a_failed_transaction_leaves_no_orphan_file(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import documents as documents_api

    async def broken_audit(**_: Any) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(documents_api, "record_audit_event", broken_audit)
    w = academic_world.a
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await upload(c, w, w.t1)
    assert resp.status_code == 500
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_storage_write_failure_is_503_and_writes_no_rows(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStore:
        async def put(self, *_: Any) -> str:
            raise OSError("disk full")

        async def delete(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(document_store, "get_document_store", lambda: BrokenStore())
    w = academic_world.a
    resp = await upload(client, w, w.t1)
    assert_error_code(resp, 503, "service_unavailable")
    assert await row_counts(raw_db, w.id) == NO_ROWS


# ── Duplicates and the one-in-flight rule ────────────────────────────────────


async def test_same_bytes_as_an_active_version_is_409_duplicate_version(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", data=TXT,
        store_root=document_store_root,
    )
    resp = await replace(client, w, w.t1, made.document.id, data=TXT)
    assert_error_code(resp, 409, "duplicate_version")
    assert resp.json()["detail"]["version_id"] == str(made.version.id)
    assert_store_matches_rows(document_store_root, await _keys(raw_db, w))


async def test_bytes_of_a_failed_version_can_be_uploaded_again(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="failed", data=TXT)
    resp = await replace(client, w, w.t1, made.document.id, data=TXT)
    assert resp.status_code == 202, resp.text
    assert resp.json()["version_no"] == 2 and resp.json()["document_id"] == str(made.document.id)
    audit = (
        await raw_db.execute(
            select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "document:version_upload")
        )
    ).scalar_one()
    assert audit.details["version_no"] == 2


async def test_database_unique_violation_maps_to_duplicate_version_and_removes_the_file(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A race the pre-check cannot see: the partial unique index answers, the stable code is the same."""
    from app.api.v1 import documents as documents_api

    async def no_duplicate(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(documents_api, "_active_duplicate", no_duplicate)
    w = academic_world.a
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", data=TXT,
        store_root=document_store_root,
    )
    resp = await replace(client, w, w.t1, made.document.id, data=TXT)
    assert_error_code(resp, 409, "duplicate_version")
    assert_store_matches_rows(document_store_root, await _keys(raw_db, w))


async def test_replace_while_a_version_is_in_flight_is_409(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="queued")
    for status in ("queued", "processing", "ready"):
        await raw_db.execute(update(DocumentVersion).where(DocumentVersion.id == made.version.id).values(status=status))
        await raw_db.commit()
        resp = await replace(client, w, w.t1, made.document.id, data=b"new bytes " + status.encode())
        assert_error_code(resp, 409, "document_processing_in_progress")
    assert_no_stored_files(document_store_root)


async def test_duplicate_content_in_another_visible_document_is_a_warning(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", data=TXT)
    resp = await upload(client, w, w.t1, subject="Math", classes=("10-B",), data=TXT)
    assert resp.status_code == 202 and resp.json()["warnings"] == ["duplicate_content"]
    # T2 cannot see T1's Math documents, so the same bytes stay silent for T2 (no leak).
    resp = await upload(client, w, w.t2, subject="Physics", classes=("10-B",), data=TXT)
    assert resp.status_code == 202 and resp.json()["warnings"] == []


# ── Replace: manage on ALL linked classes ────────────────────────────────────


async def test_replace_needs_manage_on_every_linked_class(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    shared = await make_document(raw_db, w, subject="Physics", classes=("10-A", "10-B"), uploader=w.admin)
    own = await make_document(raw_db, w, subject="Math", classes=("10-A", "10-B"), uploader=w.t1)
    # T1 teaches 10-A Physics only: visible, but not changeable.
    assert_error_code(await replace(client, w, w.t1, shared.document.id, data=b"v2"), 403,
                      "document_manage_requires_all_classes")
    # T2 has no Math pair at all: generic 404.
    assert_generic_404(await replace(client, w, w.t2, own.document.id, data=b"v2"))
    # Students never reach document routes.
    assert_error_code(await replace(client, w, w.s1, own.document.id, data=b"v2"), 403, "permission_denied")
    assert_no_stored_files(document_store_root)
    assert (await replace(client, w, w.t1, own.document.id, data=b"v2")).status_code == 202
    assert (await replace(client, w, w.admin, shared.document.id, data=b"v2")).status_code == 202


async def test_replace_of_a_deleted_or_foreign_document_is_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    foreign = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1)
    assert_generic_404(await replace(client, a, a.admin, foreign.document.id, data=b"x"))
    assert_generic_404(await replace(client, a, a.admin, uuid.uuid4(), data=b"x"))
    assert_no_stored_files(document_store_root)


# ── Quotas (A3 step 6) ───────────────────────────────────────────────────────


async def test_documents_quota(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, max_documents=2)
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, store_root=document_store_root)
    assert (await upload(client, w, w.t1, data=b"second")).status_code == 202  # exactly at the limit
    before = await row_counts(raw_db, w.id)
    resp = await upload(client, w, w.t1, data=b"third")
    assert_quota_refused(resp, "quota_documents_exceeded")
    assert await row_counts(raw_db, w.id) == before
    assert_store_matches_rows(document_store_root, await _keys(raw_db, w))


async def test_soft_deleted_documents_do_not_count_against_the_documents_quota(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    from datetime import UTC, datetime

    from app.db.models.document import Document

    w = academic_world.a
    await _set_limits(raw_db, w, max_documents=1)
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    await raw_db.execute(
        update(Document).where(Document.id == made.document.id).values(deleted_at=datetime.now(UTC), deleted_by=w.t1.id)
    )
    await raw_db.commit()
    assert (await upload(client, w, w.t1)).status_code == 202


async def test_storage_quota(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    existing = b"x" * 100
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, data=existing,
                        store_root=document_store_root)
    await _set_limits(raw_db, w, max_storage_bytes=100 + len(TXT))
    assert (await upload(client, w, w.t1)).status_code == 202  # exactly at the limit
    resp = await upload(client, w, w.t1, data=b"one more byte")
    assert_quota_refused(resp, "quota_storage_exceeded")
    assert_store_matches_rows(document_store_root, await _keys(raw_db, w))


async def test_pages_quota(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", page_count=5)
    await _set_limits(raw_db, w, max_pages=6)
    assert (await upload(client, w, w.t1, data=b"first")).status_code == 202  # 5 of 6 pages used
    await _set_limits(raw_db, w, max_pages=5)
    files_before = stored_files(document_store_root)
    rows_before = await row_counts(raw_db, w.id)
    resp = await upload(client, w, w.t1, data=b"second")
    assert_quota_refused(resp, "quota_pages_exceeded")
    assert stored_files(document_store_root) == files_before
    assert await row_counts(raw_db, w.id) == rows_before


# ── Rate limits (A3 step 2) ──────────────────────────────────────────────────


async def test_per_user_upload_rate_limit(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, uploads_per_user_per_hour=1)
    assert (await upload(client, w, w.t1, data=b"one")).status_code == 202
    resp = await upload(client, w, w.t1, data=b"two")
    assert_error_code(resp, 429, "rate_limit_exceeded")
    assert int(resp.headers["retry-after"]) > 0
    # Another user of the same tenant is not affected by T1's window.
    assert (await upload(client, w, w.admin, data=b"three")).status_code == 202


async def test_per_tenant_upload_rate_limit(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, uploads_per_tenant_per_hour=1)
    assert (await upload(client, w, w.t1, data=b"one")).status_code == 202
    resp = await upload(client, w, w.admin, data=b"two")
    assert_error_code(resp, 429, "tenant_rate_limit_exceeded")
    assert int(resp.headers["retry-after"]) > 0
    # Tenant B has its own window.
    b = academic_world.b
    assert (await upload(client, b, b.t1)).status_code == 202


async def test_rate_limit_fails_closed_without_redis_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.core import redis_client
    from app.core.config import settings
    from app.services.document_limits import default_limits
    from app.services.document_upload import check_upload_rate

    async def no_redis() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", no_redis)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(HTTPException) as exc_info:
        await check_upload_rate(uuid.uuid4(), uuid.uuid4(), default_limits())
    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.detail, dict) and exc_info.value.detail["code"] == "service_unavailable"


# ── Request validation ───────────────────────────────────────────────────────


async def test_blank_title_or_missing_classes_are_refused_before_anything_is_stored(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    resp = await upload(client, w, w.t1, title="   ")
    assert_error_code(resp, 422, "invalid_title")
    resp = await upload(client, w, w.t1, classes=())
    assert resp.status_code == 422
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)
