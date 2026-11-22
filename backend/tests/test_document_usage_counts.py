"""
tests/test_document_usage_counts.py
───────────────────────────────────
GET /api/v1/admin/documents/usage, the two additive fields (docs/backend-small-plan.md item 3):

  - versions_by_status: all six statuses, versions of LIVE documents only;
  - uploads_last_hour: upload ATTEMPTS in the rolling 60 minute windows the limiter enforces; a rejected
    upload still counts; null values (never zeros) when Redis cannot be read, the rest of the body intact.

The tenant isolation tests are in test_document_usage_isolation.py. The _test database only.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import slots
from app.db.models.document import DOCUMENT_VERSION_STATUSES, Document
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth, make_document, upload

pytestmark = pytest.mark.asyncio

URL = "/api/v1/admin/documents/usage"
ZERO_STATUSES = dict.fromkeys(DOCUMENT_VERSION_STATUSES, 0)


async def _usage(client: AsyncClient, w: WorldTenant) -> dict:  # type: ignore[type-arg]
    resp = await client.get(URL, headers=auth(w, w.admin))
    assert resp.status_code == 200, resp.text
    body: dict = resp.json()  # type: ignore[type-arg]
    return body


async def _soft_delete(raw_db: AsyncSession, document: Document) -> None:
    await raw_db.execute(update(Document).where(Document.id == document.id).values(deleted_at=datetime.now(UTC)))
    await raw_db.commit()


# ── versions_by_status ───────────────────────────────────────────────────────


async def test_versions_by_status_has_all_six_keys_with_the_right_counts_and_skips_deleted_documents(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    plan = {"queued": 1, "processing": 2, "ready": 3, "published": 4, "archived": 5, "failed": 6}
    for status, n in plan.items():
        for _ in range(n):
            await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status=status)
    # A soft delete archives every version: the archived version of a deleted document must NOT be counted,
    # and neither must any other version of it.
    gone_archived = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="archived")
    gone_failed = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="failed")
    await _soft_delete(raw_db, gone_archived.document)
    await _soft_delete(raw_db, gone_failed.document)

    body = await _usage(client, w)
    assert set(body["versions_by_status"]) == set(DOCUMENT_VERSION_STATUSES)
    assert len(DOCUMENT_VERSION_STATUSES) == 6
    assert body["versions_by_status"] == plan


async def test_a_tenant_without_documents_gets_all_six_statuses_as_zero(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    body = await _usage(client, academic_world.a)
    assert body["versions_by_status"] == ZERO_STATUSES


async def test_the_existing_fields_are_unchanged_and_the_two_new_ones_are_added(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published",
                        data=b"x" * 10, page_count=4)
    body = await _usage(client, w)
    assert set(body) == {
        "documents", "storage_bytes", "pages", "limits", "versions_by_status", "uploads_last_hour"
    }
    assert (body["documents"], body["storage_bytes"], body["pages"]) == (1, 10, 4)
    assert set(body["uploads_last_hour"]) == {"tenant", "caller", "window_seconds"}
    assert body["uploads_last_hour"]["window_seconds"] == 3600


# ── uploads_last_hour ────────────────────────────────────────────────────────


async def test_an_upload_rejected_for_its_format_still_uses_a_slot_and_counts(
    client: AsyncClient, academic_world: AcademicWorld, document_store_root: Path
) -> None:
    """The counter says ATTEMPTS: the limiter takes the slot before the file is read and never gives it back."""
    w = academic_world.a
    resp = await upload(client, w, w.admin, filename="program.exe", data=b"MZ\x90\x00 not a document")
    assert 400 <= resp.status_code < 500, resp.text
    body = await _usage(client, w)
    assert body["uploads_last_hour"] == {"tenant": 1, "caller": 1, "window_seconds": 3600}
    assert body["documents"] == 0  # nothing was stored, yet the attempt used its slot


# ── Redis down ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(status_code=503, detail={"code": "service_unavailable", "message": "down"}),
        ConnectionError("redis is gone"),
    ],
    ids=["the slot manager's own 503", "any other error"],
)
async def test_redis_down_gives_null_counts_and_the_rest_of_the_body_still_works(
    error: Exception,
    client: AsyncClient,
    academic_world: AcademicWorld,
    raw_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published",
                        data=b"x" * 10, page_count=4)

    async def broken_count(self: slots.SlotManager, key: str, **_: object) -> int:
        raise error

    monkeypatch.setattr(slots.SlotManager, "count", broken_count)
    body = await _usage(client, w)
    assert body["uploads_last_hour"] == {"tenant": None, "caller": None, "window_seconds": 3600}
    # Everything else renders as usual
    assert (body["documents"], body["storage_bytes"], body["pages"]) == (1, 10, 4)
    assert body["versions_by_status"] == {**ZERO_STATUSES, "published": 1}
    assert body["limits"]["max_documents"] == 200
