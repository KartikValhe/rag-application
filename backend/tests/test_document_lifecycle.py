"""
Publish and archive (step 5c-2, design A2 "Lifecycle" and A4): ONE function, document_lifecycle.publish_version,
used by POST /api/v1/documents/{id}/versions/{version_id}/publish and by the worker's auto-publish.

Real app, real database (_test), realistic academic world.
"""

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog
from app.db.models.document import DocumentVersion
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument, auth, make_document
from tests.fixtures.pipeline import add_version, count_rows, ready_version
from tests.security.document_assertions import assert_error_code, assert_generic_404

pytestmark = pytest.mark.asyncio


def publish_url(m: MadeDocument | uuid.UUID, version_id: uuid.UUID | None = None) -> str:
    doc_id = m.document.id if isinstance(m, MadeDocument) else m
    vid = version_id if version_id is not None else m.version.id  # type: ignore[union-attr]
    return f"/api/v1/documents/{doc_id}/versions/{vid}/publish"


async def publish(
    client: AsyncClient, w: WorldTenant, user: Any, m: MadeDocument, *, acknowledge: bool | None = None, body: dict[str, Any] | None = None
) -> Any:
    payload = body if body is not None else ({} if acknowledge is None else {"acknowledge_flags": acknowledge})
    return await client.post(publish_url(m), headers=auth(w, user), json=payload)


async def statuses(db: AsyncSession, document_id: uuid.UUID) -> dict[int, str]:
    rows = (await db.execute(text("SELECT version_no, status FROM app.document_versions WHERE document_id = :d ORDER BY version_no"), {"d": document_id})).all()
    return {r[0]: r[1] for r in rows}


async def base_document(
    raw_db: AsyncSession, w: WorldTenant, tmp_path: Path, *, subject: str = "Math", classes: tuple[str, ...] = ("10-A",)
) -> MadeDocument:
    """A document whose version 1 is ready with chunks, uploaded by teacher T1."""
    made = await make_document(raw_db, w, subject=subject, classes=classes, uploader=w.t1, status="failed", store_root=tmp_path)
    await raw_db.execute(text("DELETE FROM app.ingest_jobs WHERE version_id = :v"), {"v": made.version.id})
    await raw_db.execute(text("DELETE FROM app.document_versions WHERE id = :v"), {"v": made.version.id})
    await raw_db.commit()
    return made


async def ready(raw_db: AsyncSession, w: WorldTenant, tmp_path: Path, **kw: Any) -> MadeDocument:
    made = await base_document(raw_db, w, tmp_path, **{k: kw.pop(k) for k in ("subject", "classes") if k in kw})
    return await ready_version(raw_db, made, w, tmp_path, version_no=1, **kw)


# ── Publishing ───────────────────────────────────────────────────────────────


async def test_an_admin_publishes_a_ready_version(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path)
    r = await publish(client, w, w.admin, m)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["status"]) == (str(m.version.id), "published") and body["published_at"] is not None
    row = (await raw_db.execute(select(DocumentVersion).where(DocumentVersion.id == m.version.id).execution_options(populate_existing=True))).scalar_one()
    assert (row.status, row.published_by) == ("published", w.admin.id)
    assert await count_rows(raw_db, "chunks", m.version.id) == 2  # a published version keeps its chunks
    audit = (await raw_db.execute(select(AuditLog).where(AuditLog.action == "document:publish", AuditLog.target_id == str(m.document.id)))).scalars().all()
    assert len(audit) == 1 and audit[0].actor_id == w.admin.id and audit[0].details["auto"] is False


@pytest.mark.parametrize("status", ["queued", "processing", "failed", "published", "archived"])
async def test_only_a_ready_version_can_be_published(
    status: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    made = await add_version(raw_db, base, w, tmp_path, status=status, version_no=1)
    r = await publish(client, w, w.admin, made)
    assert_error_code(r, 409, "version_not_ready")
    assert (await statuses(raw_db, made.document.id))[1] == status  # nothing changed


async def test_publishing_replaces_the_published_version_and_deletes_its_chunks_and_vectors(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    v1 = await ready_version(raw_db, base, w, tmp_path, status="published", chunks=3, version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", chunks=2, version_no=2)
    assert await count_rows(raw_db, "chunk_embeddings", v1.version.id) == 3

    r = await publish(client, w, w.t1, v2)
    assert r.status_code == 200, r.text
    assert await statuses(raw_db, v1.document.id) == {1: "archived", 2: "published"}
    assert await count_rows(raw_db, "chunks", v1.version.id) == 0
    assert await count_rows(raw_db, "chunk_embeddings", v1.version.id) == 0  # embeddings cascade with their chunks
    assert await count_rows(raw_db, "chunks", v2.version.id) == 2
    # the archived version's file waits 30 days in the storage outbox (the same transaction time as published_at)
    published_at = (await raw_db.execute(text("SELECT published_at FROM app.document_versions WHERE id = :v"), {"v": v2.version.id})).scalar_one()
    rows = (await raw_db.execute(text("SELECT storage_key, reason, not_before FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()
    assert [(r_[0], r_[1]) for r_ in rows] == [(v1.version.storage_key, "version_archived")]
    assert rows[0][2] == published_at + timedelta(days=30)
    actions = {a.action for a in (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id))).scalars()}
    assert {"document:publish", "document:archive"} <= actions


async def test_the_retention_period_comes_from_the_setting(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "DOC_ARCHIVE_FILE_RETENTION_DAYS", 7)
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=2)
    assert (await publish(client, w, w.admin, v2)).status_code == 200
    published_at = (await raw_db.execute(text("SELECT published_at FROM app.document_versions WHERE id = :v"), {"v": v2.version.id})).scalar_one()
    not_before = (await raw_db.execute(text("SELECT not_before FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).scalar_one()
    assert not_before == published_at + timedelta(days=7)


async def test_archived_versions_beyond_five_are_pruned_oldest_first_and_their_files_queued(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    old_keys = {}
    for n in range(1, 8):  # versions 1-7 archived
        old = await add_version(raw_db, base, w, tmp_path, status="archived", version_no=n)
        old_keys[n] = old.version.storage_key
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=8)
    v9 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=9)

    assert (await publish(client, w, w.admin, v9)).status_code == 200
    # 8 archived (1-8) -> the newest five (4-8) stay, 1-3 are pruned
    assert await statuses(raw_db, base.document.id) == {4: "archived", 5: "archived", 6: "archived", 7: "archived", 8: "archived", 9: "published"}
    queued = {r[0]: r[1:] for r in (await raw_db.execute(text("SELECT storage_key, reason, not_before <= now() FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()}
    for n in (1, 2, 3):
        assert queued[old_keys[n]] == ("version_pruned", True)  # due now: nothing can restore a pruned row
    assert queued[(await raw_db.execute(text("SELECT storage_key FROM app.document_versions WHERE document_id = :d AND version_no = 8"), {"d": base.document.id})).scalar_one()] == ("version_archived", False)
    pruned = (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "document:prune_archived"))).scalars().all()
    assert len(pruned) == 1 and pruned[0].details["count"] == 3
    assert sorted(item["version_no"] for item in pruned[0].details["pruned"]) == [1, 2, 3]


async def test_nothing_is_pruned_while_there_are_five_or_fewer_archived_versions(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    for n in range(1, 5):
        await add_version(raw_db, base, w, tmp_path, status="archived", version_no=n)
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=5)
    v6 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=6)
    assert (await publish(client, w, w.admin, v6)).status_code == 200
    assert len(await statuses(raw_db, base.document.id)) == 6
    assert not (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "document:prune_archived"))).scalars().all()


# ── Flags ────────────────────────────────────────────────────────────────────


async def test_a_flagged_version_needs_an_explicit_acknowledgement(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path, flagged=2)
    for body in ({}, {"acknowledge_flags": False}):
        assert_error_code(await publish(client, w, w.t1, m, body=body), 409, "flags_acknowledgement_required")
    assert (await statuses(raw_db, m.document.id)) == {1: "ready"}
    r = await publish(client, w, w.t1, m, acknowledge=True)
    assert r.status_code == 200, r.text
    row = (await raw_db.execute(text("SELECT status, flags_acknowledged_by FROM app.document_versions WHERE id = :v"), {"v": m.version.id})).one()
    assert tuple(row) == ("published", w.t1.id)
    assert (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "document:flags_acknowledged"))).scalars().first() is not None


async def test_acknowledging_is_harmless_on_an_unflagged_version_and_records_nothing(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path)
    assert (await publish(client, w, w.admin, m, acknowledge=True)).status_code == 200
    assert (await raw_db.execute(text("SELECT flags_acknowledged_by FROM app.document_versions WHERE id = :v"), {"v": m.version.id})).scalar_one() is None


async def test_the_request_body_is_strict(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path)
    for body in ({"acknowledge_flags": "yes"}, {"acknowledge_flags": True, "extra": 1}, {"acknowledge_flags": None}):
        r = await publish(client, w, w.admin, m, body=body)
        assert r.status_code == 422, (body, r.text)
    assert (await statuses(raw_db, m.document.id)) == {1: "ready"}


# ── Who may publish ──────────────────────────────────────────────────────────


async def test_a_teacher_in_all_linked_classes_can_publish(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a  # T1 teaches Math in 10-A and 10-B
    m = await ready(raw_db, w, tmp_path, subject="Math", classes=("10-A", "10-B"))
    assert (await publish(client, w, w.t1, m)).status_code == 200


async def test_a_teacher_in_only_some_linked_classes_is_refused_with_403(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a  # T1 teaches Physics in 10-A only; the document is linked to 10-A and 10-B
    m = await ready(raw_db, w, tmp_path, subject="Physics", classes=("10-A", "10-B"))
    assert_error_code(await publish(client, w, w.t1, m), 403, "document_manage_requires_all_classes")
    assert (await statuses(raw_db, m.document.id)) == {1: "ready"}


async def test_a_teacher_out_of_scope_gets_the_generic_404_and_a_student_is_refused_outright(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path, subject="Math", classes=("10-A",))
    assert_generic_404(await publish(client, w, w.t2, m))  # T2 has no Math pair at all
    assert (await publish(client, w, w.s1, m)).status_code == 403  # students hold no document permission
    assert (await publish(client, w, w.viewer, m)).status_code == 403  # document:view alone is not publish
    assert (await statuses(raw_db, m.document.id)) == {1: "ready"}


async def test_another_tenants_document_and_an_unknown_version_are_a_generic_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    foreign = await ready(raw_db, b, tmp_path)
    assert_generic_404(await publish(client, a, a.admin, foreign))
    mine = await ready(raw_db, a, tmp_path)
    r = await client.post(publish_url(mine.document.id, uuid.uuid4()), headers=auth(a, a.admin), json={})
    assert_generic_404(r)
    other_doc = await ready(raw_db, a, tmp_path, subject="Physics")
    # a version id that belongs to another document of the same tenant
    assert_generic_404(await client.post(publish_url(mine.document.id, other_doc.version.id), headers=auth(a, a.admin), json={}))
    assert (await statuses(raw_db, mine.document.id)) == {1: "ready"}


async def test_a_deleted_document_cannot_be_published(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    m = await ready(raw_db, w, tmp_path)
    await raw_db.execute(text("UPDATE app.documents SET deleted_at = now() WHERE id = :d"), {"d": m.document.id})
    await raw_db.commit()
    assert_generic_404(await publish(client, w, w.admin, m))


# ── Versioning ───────────────────────────────────────────────────────────────


async def test_a_replacement_leaves_the_old_version_published_until_the_new_one_is_published(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, document_store_root: Path
) -> None:
    from tests.fixtures.documents import replace as replace_route

    w = academic_world.a
    base = await base_document(raw_db, w, document_store_root)
    v1 = await ready_version(raw_db, base, w, document_store_root, status="published", chunks=3, version_no=1)
    r = await replace_route(client, w, w.t1, v1.document.id, data=b"Unit 2: a new syllabus text\n")
    assert r.status_code == 202, r.text
    assert await statuses(raw_db, v1.document.id) == {1: "published", 2: "queued"}
    assert await count_rows(raw_db, "chunks", v1.version.id) == 3  # students keep reading v1

    # As the worker leaves it: ready and scanned (a NULL count would mean "never scanned", 5d-2b)
    await raw_db.execute(text("UPDATE app.document_versions SET status = 'ready', flagged_chunk_count = 0 WHERE document_id = :d AND version_no = 2"), {"d": v1.document.id})
    await raw_db.commit()
    assert await statuses(raw_db, v1.document.id) == {1: "published", 2: "ready"}  # ready alone changes nothing for students
    assert await count_rows(raw_db, "chunks", v1.version.id) == 3
    v2_id = (await raw_db.execute(text("SELECT id FROM app.document_versions WHERE document_id = :d AND version_no = 2"), {"d": v1.document.id})).scalar_one()
    r = await client.post(publish_url(v1.document.id, v2_id), headers=auth(w, w.t1), json={})
    assert r.status_code == 200, r.text
    assert await statuses(raw_db, v1.document.id) == {1: "archived", 2: "published"}
    assert await count_rows(raw_db, "chunks", v1.version.id) == 0


# ── Concurrency ──────────────────────────────────────────────────────────────


async def test_two_concurrent_publishes_end_with_exactly_one_published_version(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=2)
    results = await asyncio.gather(*(publish(client, w, w.admin, v2) for _ in range(4)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409], [r.text for r in results]
    assert all(r.json()["detail"]["code"] == "version_not_ready" for r in results if r.status_code == 409)
    assert await statuses(raw_db, base.document.id) == {1: "archived", 2: "published"}
    assert (await raw_db.execute(text("SELECT count(*) FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).scalar_one() == 1  # archived once


async def test_a_unique_violation_on_the_published_index_is_a_stable_409_code(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a second published version would ever appear (here: the archive step is skipped), the partial unique
    index refuses it and the route answers with a stable code, never a 500."""
    from app.services import document_lifecycle

    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=2)

    async def skip(*_a: object, **_k: object) -> list[object]:
        return []

    monkeypatch.setattr(document_lifecycle, "archive_published", skip)
    assert_error_code(await publish(client, w, w.admin, v2), 409, "publish_conflict")
    assert await statuses(raw_db, base.document.id) == {1: "published", 2: "ready"}  # rolled back as a whole


async def test_a_reader_never_sees_zero_or_two_published_versions_while_a_publish_runs(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    """A second connection polls inside a barrier while the publish transaction is open between its steps."""
    from tests.security.lifecycle_assertions import (
        assert_exactly_one_published,
        observe_published_counts_during_publish,
    )

    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await ready_version(raw_db, base, w, tmp_path, status="published", version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=2)
    counts = await observe_published_counts_during_publish(w, base.document.id, v2.version.id)
    assert len(counts) == 3  # before, in the middle of the transaction, after the commit
    assert_exactly_one_published(counts)
    assert await statuses(raw_db, base.document.id) == {1: "archived", 2: "published"}


# ── Delete (the existing route) with the new data ────────────────────────────


async def test_deleting_a_published_document_removes_chunks_and_vectors_and_queues_every_file(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    old = await ready_version(raw_db, base, w, tmp_path, status="archived", chunks=0, version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="published", chunks=3, version_no=2)
    r = await client.delete(f"/api/v1/documents/{base.document.id}", headers=auth(w, w.admin))
    assert r.status_code == 204
    assert await count_rows(raw_db, "chunks", v2.version.id) == 0
    assert await count_rows(raw_db, "chunk_embeddings", v2.version.id) == 0
    keys = {r_[0] for r_ in (await raw_db.execute(text("SELECT storage_key FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()}
    assert keys == {old.version.storage_key, v2.version.storage_key}
