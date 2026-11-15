"""
tests/test_document_routes.py
─────────────────────────────
The document routes other than upload (design A2, A4; step 5b-1), through the REAL app with the
realistic academic world: list (scope, filters, cursor), detail, PATCH (title and class links),
soft delete with the storage outbox, admin purge, status, retry, version delete, download and usage.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.upload_security import attachment_header
from app.db.models.audit import AuditLog
from app.db.models.document import (
    Chunk,
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
    StorageDeletion,
)
from app.services import document_store
from tests.fixtures.academic_world import AcademicWorld, WorldTenant, create_document_graph
from tests.fixtures.documents import TXT, auth, make_document, upload
from tests.security.document_assertions import (
    assert_error_code,
    assert_generic_404,
    assert_manage_blocked,
    assert_no_foreign_documents,
    assert_no_stored_files,
)

pytestmark = pytest.mark.asyncio

DOCUMENT_KEYS = {
    "id", "title", "description", "subject", "classes", "created_by", "created_at", "updated_at",
    "latest_version", "published_version", "can_manage", "blocked_reason",
}
STATUS_KEYS = {"document_id", "version_id", "version_no", "status", "stage", "error_code", "retryable", "updated_at"}


def _doc_url(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}"


def _version_url(document_id: uuid.UUID, version_id: uuid.UUID, tail: str = "") -> str:
    return f"/api/v1/documents/{document_id}/versions/{version_id}{tail}"


async def _audits(raw_db: AsyncSession, w: WorldTenant, action: str) -> list[AuditLog]:
    return list(
        (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == action))).scalars()
    )


async def _list_all(client: AsyncClient, w: WorldTenant, user: object, query: str = "") -> list[dict]:  # type: ignore[type-arg]
    resp = await client.get(f"/api/v1/documents{query}", headers=auth(w, user))  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    return list(body["items"])


# ── List ─────────────────────────────────────────────────────────────────────


async def test_list_shows_each_caller_only_their_pairs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w, b = academic_world.a, academic_world.b
    t1_math = await make_document(raw_db, w, subject="Math", classes=("10-A", "10-B"), uploader=w.t1)
    shared_phys = await make_document(raw_db, w, subject="Physics", classes=("10-A", "10-B"), uploader=w.admin)
    t2_chem = await make_document(raw_db, w, subject="Chemistry", classes=("11-A",), uploader=w.t2)
    eng = await make_document(raw_db, w, subject="English", classes=("10-A",), uploader=w.admin)
    foreign = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1)

    t1_items = await _list_all(client, w, w.t1)
    assert_no_foreign_documents(t1_items, [t1_math.document.id, shared_phys.document.id])
    assert {i["id"] for i in t1_items} == {str(t1_math.document.id), str(shared_phys.document.id)}
    by_id = {i["id"]: i for i in t1_items}
    assert set(by_id[str(t1_math.document.id)]) == DOCUMENT_KEYS
    assert by_id[str(t1_math.document.id)]["can_manage"] is True
    assert by_id[str(shared_phys.document.id)]["can_manage"] is False
    assert by_id[str(shared_phys.document.id)]["blocked_reason"] == "document_manage_requires_all_classes"

    t2_items = await _list_all(client, w, w.t2)
    assert_no_foreign_documents(t2_items, [shared_phys.document.id, t2_chem.document.id])

    admin_items = await _list_all(client, w, w.admin)
    assert {i["id"] for i in admin_items} == {
        str(d.document.id) for d in (t1_math, shared_phys, t2_chem, eng)
    }
    assert str(foreign.document.id) not in {i["id"] for i in admin_items}
    assert all(i["can_manage"] for i in admin_items)

    # A view-only custom role has no class pairs: an empty list, not an error.
    assert await _list_all(client, w, w.viewer) == []
    # Students never reach document routes.
    resp = await client.get("/api/v1/documents", headers=auth(w, w.s1))
    assert_error_code(resp, 403, "permission_denied")


async def test_list_filters_and_cursor_pagination(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = [
        await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published"),
        await make_document(raw_db, w, subject="Math", classes=("10-B",), uploader=w.t1, status="failed"),
        await make_document(raw_db, w, subject="Physics", classes=("10-A",), uploader=w.t1, status="failed"),
    ]
    ids = {str(m.document.id) for m in made}
    deleted = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    await raw_db.execute(update(Document).where(Document.id == deleted.document.id).values(deleted_at=datetime.now(UTC)))
    await raw_db.commit()

    # Cursor pagination: one item per page, every document exactly once, newest first.
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        q = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
        resp = await client.get(f"/api/v1/documents{q}", headers=auth(w, w.t1))
        assert resp.status_code == 200, resp.text
        seen += [i["id"] for i in resp.json()["items"]]
        cursor = resp.json()["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == 3
    assert str(deleted.document.id) not in seen

    math = await _list_all(client, w, w.t1, f"?subject_id={w.subjects['Math'].id}")
    assert {i["id"] for i in math} == {str(made[0].document.id), str(made[1].document.id)}
    in_10b = await _list_all(client, w, w.t1, f"?class_id={w.classes['10-B'].id}")
    assert {i["id"] for i in in_10b} == {str(made[1].document.id)}
    failed = await _list_all(client, w, w.t1, "?status=failed")
    assert {i["id"] for i in failed} == {str(made[1].document.id), str(made[2].document.id)}
    # A class outside T1's pairs gives an empty list, never another teacher's documents.
    assert await _list_all(client, w, w.t1, f"?class_id={w.classes['11-A'].id}") == []

    bad = await client.get("/api/v1/documents?cursor=not-a-cursor", headers=auth(w, w.t1))
    assert_error_code(bad, 422, "invalid_cursor")
    assert (await client.get("/api/v1/documents?status=bogus", headers=auth(w, w.t1))).status_code == 422


# ── Detail ───────────────────────────────────────────────────────────────────


async def test_detail_has_every_key_and_hides_out_of_scope_documents(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A", "10-B"), uploader=w.t1, status="published")
    resp = await client.get(_doc_url(made.document.id), headers=auth(w, w.t1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == DOCUMENT_KEYS
    assert body["subject"] == {"id": str(w.subjects["Math"].id), "name": "Math"}
    assert sorted(c["name"] for c in body["classes"]) == ["10-A", "10-B"]
    assert body["latest_version"]["id"] == str(made.version.id)
    assert body["published_version"]["id"] == str(made.version.id)
    assert body["can_manage"] is True and body["blocked_reason"] is None
    assert "storage_key" not in resp.text and made.version.storage_key not in resp.text
    for user in (w.t2, w.viewer):
        assert_generic_404(await client.get(_doc_url(made.document.id), headers=auth(w, user)))


# ── PATCH ────────────────────────────────────────────────────────────────────


async def test_patch_title_and_links(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    own = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    url = _doc_url(own.document.id)
    resp = await client.patch(url, headers=auth(w, w.t1), json={"title": "Algebra", "class_ids": [
        str(w.classes["10-A"].id), str(w.classes["10-B"].id)]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Algebra"
    assert sorted(c["name"] for c in resp.json()["classes"]) == ["10-A", "10-B"]
    audit = (await _audits(raw_db, w, "document:update"))[0]
    assert audit.details["added_class_ids"] == [str(w.classes["10-B"].id)]

    # Linking a class outside the teacher's pairs (11-A Math is not his) is the generic 404.
    resp = await client.patch(url, headers=auth(w, w.t1), json={"class_ids": [str(w.classes["11-A"].id)]})
    assert_generic_404(resp)
    # The subject never changes, and an empty class list is refused.
    assert (await client.patch(url, headers=auth(w, w.t1), json={"subject_id": str(w.subjects["Physics"].id)})).status_code == 422
    assert (await client.patch(url, headers=auth(w, w.t1), json={"class_ids": []})).status_code == 422
    links = (await raw_db.execute(select(func.count()).select_from(DocumentClass).where(
        DocumentClass.document_id == own.document.id))).scalar_one()
    assert links == 2


async def test_patch_on_a_shared_document_needs_all_classes_except_for_adding_own_pairs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    shared = await make_document(raw_db, w, subject="Physics", classes=("10-A", "10-B"), uploader=w.admin)
    url = _doc_url(shared.document.id)
    assert_manage_blocked(await client.patch(url, headers=auth(w, w.t1), json={"title": "Mine now"}))
    # Unlinking 10-B (T2's class) needs manage on all linked classes.
    assert_manage_blocked(
        await client.patch(url, headers=auth(w, w.t1), json={"class_ids": [str(w.classes["10-A"].id)]})
    )
    # The admin can do both.
    resp = await client.patch(url, headers=auth(w, w.admin), json={"title": "Physics 10", "class_ids": [
        str(w.classes["10-A"].id)]})
    assert resp.status_code == 200, resp.text
    # T2 now has no linked pair left: generic 404.
    assert_generic_404(await client.get(url, headers=auth(w, w.t2)))
    # Students never reach it.
    assert_error_code(await client.patch(url, headers=auth(w, w.s1), json={"title": "x"}), 403, "permission_denied")


# ── DELETE (soft) and purge ──────────────────────────────────────────────────


async def test_delete_is_one_transaction_with_outbox_rows_and_subject_stays_blocked_until_purge(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    graph = await create_document_graph(raw_db, w, subject_name="English", class_names=("10-A",), uploader=w.admin)
    doc_id = graph.document.id
    # A second, in-flight version of the same document, with its file in the store.
    key2 = document_store.new_document_key(w.id)
    v2 = DocumentVersion(
        tenant_id=w.id, document_id=doc_id, version_no=2, status="queued", file_kind="txt",
        original_filename="v2.txt", size_bytes=len(TXT), sha256=hashlib.sha256(TXT).hexdigest(),
        storage_key=key2, uploaded_by=w.admin.id,
    )
    raw_db.add(v2)
    await raw_db.flush()
    raw_db.add(IngestJob(tenant_id=w.id, version_id=v2.id, status="queued"))
    await raw_db.commit()
    await document_store.LocalPrivateStore(document_store_root).put(w.id, key2, TXT)

    resp = await client.delete(_doc_url(doc_id), headers=auth(w, w.admin))
    assert resp.status_code == 204, resp.text

    doc = (await raw_db.execute(select(Document).where(Document.id == doc_id))).scalar_one()
    assert doc.deleted_at is not None and doc.deleted_by == w.admin.id
    statuses = (await raw_db.execute(select(DocumentVersion.status).where(DocumentVersion.document_id == doc_id))).scalars()
    assert set(statuses) == {"archived"}
    assert (await raw_db.execute(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id))).scalar_one() == 0
    outbox = (await raw_db.execute(select(StorageDeletion).where(StorageDeletion.tenant_id == w.id))).scalars().all()
    assert {o.storage_key for o in outbox} == {graph.version.storage_key, key2}
    assert all(o.not_before <= datetime.now(UTC) + timedelta(seconds=5) for o in outbox)
    job = (await raw_db.execute(select(IngestJob).where(IngestJob.version_id == v2.id))).scalar_one()
    assert job.status == "failed" and job.last_error_code == "document_deleted"
    assert len(await _audits(raw_db, w, "document:delete")) == 1
    assert_generic_404(await client.get(_doc_url(doc_id), headers=auth(w, w.admin)))
    assert_generic_404(await client.delete(_doc_url(doc_id), headers=auth(w, w.admin)))

    # The subject cannot be deleted while the tombstone exists.
    subject_url = f"/api/v1/subjects/{w.subjects['English'].id}"
    blocked = await client.delete(subject_url, headers=auth(w, w.admin))
    assert_error_code(blocked, 409, "subject_has_documents")

    # Purge-now: the tombstone, versions and files are gone; then the subject can go.
    other = await make_document(raw_db, w, subject="English", classes=("10-B",), uploader=w.admin)
    assert_error_code(
        await client.post(f"/api/v1/admin/documents/{other.document.id}/purge", headers=auth(w, w.admin)),
        409, "document_not_deleted",
    )
    await raw_db.execute(update(Document).where(Document.id == other.document.id).values(deleted_at=datetime.now(UTC)))
    await raw_db.commit()
    for d in (doc_id, other.document.id):
        resp = await client.post(f"/api/v1/admin/documents/{d}/purge", headers=auth(w, w.admin))
        assert resp.status_code == 204, resp.text
    assert (await raw_db.execute(select(func.count()).select_from(Document).where(Document.tenant_id == w.id))).scalar_one() == 0
    assert (await raw_db.execute(select(func.count()).select_from(StorageDeletion).where(
        StorageDeletion.tenant_id == w.id))).scalar_one() == 0
    assert_no_stored_files(document_store_root)
    assert len(await _audits(raw_db, w, "document:purge")) == 2
    resp = await client.delete(subject_url, headers=auth(w, w.admin))
    assert resp.status_code == 200, resp.text


async def test_delete_needs_manage_on_all_classes_and_purge_needs_settings_manage(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    shared = await make_document(raw_db, w, subject="Physics", classes=("10-A", "10-B"), uploader=w.admin)
    own = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    assert_manage_blocked(await client.delete(_doc_url(shared.document.id), headers=auth(w, w.t1)))
    assert_generic_404(await client.delete(_doc_url(own.document.id), headers=auth(w, w.t2)))
    assert (await client.delete(_doc_url(own.document.id), headers=auth(w, w.t1))).status_code == 204
    resp = await client.post(f"/api/v1/admin/documents/{own.document.id}/purge", headers=auth(w, w.t1))
    assert_error_code(resp, 403, "permission_denied")
    assert_generic_404(await client.post(f"/api/v1/admin/documents/{uuid.uuid4()}/purge", headers=auth(w, w.admin)))


# ── Status, retry, version delete ────────────────────────────────────────────


async def test_status_retry_and_version_delete(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="failed",
                               error_code="processing_failed", store_root=document_store_root)
    d, v = made.document.id, made.version.id
    resp = await client.get(_version_url(d, v, "/status"), headers=auth(w, w.t1))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == STATUS_KEYS
    assert resp.json()["status"] == "failed" and resp.json()["retryable"] is True

    # A version id under the wrong document is the generic 404.
    other = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    assert_generic_404(await client.get(_version_url(other.document.id, v, "/status"), headers=auth(w, w.t1)))

    resp = await client.post(_version_url(d, v, "/retry"), headers=auth(w, w.t1))
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "queued" and resp.json()["error_code"] is None
    job = (
        await raw_db.execute(
            select(IngestJob.status, IngestJob.attempts, IngestJob.last_error_code, IngestJob.run_after).where(
                IngestJob.version_id == v
            )
        )
    ).one()
    assert (job.status, job.attempts, job.last_error_code) == ("queued", 0, None)
    assert job.run_after <= datetime.now(UTC) + timedelta(seconds=5)
    assert len(await _audits(raw_db, w, "document:retry")) == 1

    # Retrying a version that is not failed, or failed for a permanent reason, is refused.
    assert_error_code(await client.post(_version_url(d, v, "/retry"), headers=auth(w, w.t1)), 409, "version_not_retryable")
    perm = await make_document(raw_db, w, subject="Math", classes=("10-B",), uploader=w.t1, status="failed",
                               error_code="scanned_pdf")
    assert_error_code(
        await client.post(_version_url(perm.document.id, perm.version.id, "/retry"), headers=auth(w, w.t1)),
        409, "version_not_retryable",
    )

    # Deleting a version: only draft (ready) or failed ones.
    assert_error_code(await client.delete(_version_url(d, v), headers=auth(w, w.t1)), 409, "version_not_deletable")
    resp = await client.delete(_version_url(perm.document.id, perm.version.id), headers=auth(w, w.t1))
    assert resp.status_code == 204, resp.text
    assert (await raw_db.execute(select(DocumentVersion).where(DocumentVersion.id == perm.version.id))).first() is None
    outbox = (await raw_db.execute(select(StorageDeletion.storage_key).where(StorageDeletion.tenant_id == w.id))).scalars()
    assert list(outbox) == [perm.version.storage_key]
    assert len(await _audits(raw_db, w, "document:version_delete")) == 1


# ── Download ─────────────────────────────────────────────────────────────────


async def test_download_streams_the_original_with_safe_headers(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published",
                               data=TXT, store_root=document_store_root)
    url = _version_url(made.document.id, made.version.id, "/download")
    for user in (w.t1, w.admin):
        resp = await client.get(url, headers=auth(w, user))
        assert resp.status_code == 200, resp.text
        assert resp.content == TXT
        assert resp.headers["content-disposition"] == attachment_header("notes", "txt")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in resp.headers["content-security-policy"].split(";")[0]
        assert resp.headers["cache-control"] == "private, no-store"
        assert resp.headers["content-type"] == "application/octet-stream"
    assert len(await _audits(raw_db, w, "document:download")) == 2
    assert_error_code(await client.get(url, headers=auth(w, w.s1)), 403, "permission_denied")
    assert_generic_404(await client.get(url, headers=auth(w, w.t2)))


async def test_download_is_tenant_isolated_and_a_missing_file_is_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    made = await make_document(raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, status="published",
                               data=TXT, store_root=document_store_root)
    url = _version_url(made.document.id, made.version.id, "/download")
    # Tenant B's admin, with B's own token and host, never gets tenant A's file.
    assert_generic_404(await client.get(url, headers=auth(b, b.admin)))
    # A's token on B's host is refused before any lookup.
    resp = await client.get(
        url, headers={"Authorization": f"Bearer {a.token(a.admin)}", "Host": f"{b.tenant.slug}.localhost"}
    )
    assert_error_code(resp, 403, "tenant_mismatch")

    # The database refuses a row in tenant A that points at a key under tenant B's prefix ...
    from sqlalchemy.exc import IntegrityError

    a_id, version_id, own_key = a.id, made.version.id, made.version.storage_key
    a_t1_headers = auth(a, a.t1)  # built now: the rollback below expires the fixture objects
    foreign_key = document_store.new_document_key(b.id)
    with pytest.raises(IntegrityError):
        await raw_db.execute(update(DocumentVersion).where(DocumentVersion.id == version_id).values(
            storage_key=foreign_key))
    await raw_db.rollback()
    # ... and the store refuses to serve one to tenant A anyway.
    from app.services.document_store import InvalidStorageKey

    with pytest.raises(InvalidStorageKey):
        await document_store.get_document_store().stream(a_id, foreign_key)

    (document_store_root / own_key).unlink()
    assert_generic_404(await client.get(url, headers=a_t1_headers))


# ── Usage ────────────────────────────────────────────────────────────────────


async def test_usage_counts_live_documents_against_the_limits(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published",
                        data=b"x" * 10, page_count=4)
    gone = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, data=b"y" * 7, page_count=9)
    await raw_db.execute(update(Document).where(Document.id == gone.document.id).values(deleted_at=datetime.now(UTC)))
    await raw_db.commit()
    assert (await upload(client, w, w.t1, data=b"z" * 5)).status_code == 202
    resp = await client.get("/api/v1/admin/documents/usage", headers=auth(w, w.admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"documents", "storage_bytes", "pages", "limits"}
    assert (body["documents"], body["storage_bytes"], body["pages"]) == (2, 15, 4)
    assert body["limits"]["max_documents"] == 200
    for user in (w.t1, w.s1):
        assert_error_code(await client.get("/api/v1/admin/documents/usage", headers=auth(w, user)), 403,
                          "permission_denied")
