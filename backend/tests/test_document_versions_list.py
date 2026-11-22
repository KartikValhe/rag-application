"""
tests/test_document_versions_list.py
────────────────────────────────────
GET /api/v1/documents/{document_id}/versions (docs/backend-small-plan.md item 2): every version of one document,
newest first, so the replaced (archived) version can be reached after a reload. Who may call it, the generic
404 and the tenant isolation are in test_document_versions_list_security.py. The _test database only.
"""

import hashlib
import uuid
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.body_limit import match_upload_route
from app.db.models.document import DocumentVersion
from app.schemas.document import DocumentVersionResponse
from app.services import document_store
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument, auth
from tests.fixtures.pipeline import add_version, ready_version
from tests.test_document_lifecycle import base_document, publish

VERSION_KEYS = set(DocumentVersionResponse.model_fields)
MAX_LISTED = 100


def _url(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/versions"


async def _bulk_archived(raw_db: AsyncSession, w: WorldTenant, made: MadeDocument, numbers: range) -> None:
    raw_db.add_all(
        DocumentVersion(
            tenant_id=w.id,
            document_id=made.document.id,
            version_no=n,
            status="archived",
            file_kind="txt",
            original_filename="notes.txt",
            size_bytes=1,
            sha256=hashlib.sha256(f"bulk {made.document.id} {n}".encode()).hexdigest(),
            storage_key=document_store.new_document_key(w.id),
            uploaded_by=w.t1.id,
        )
        for n in numbers
    )
    await raw_db.commit()


async def test_after_two_replacements_the_list_has_every_version_newest_first(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await ready_version(raw_db, base, w, tmp_path, status="published", chunks=1, version_no=1)
    v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", chunks=1, version_no=2)
    assert (await publish(client, w, w.t1, v2)).status_code == 200  # v1 is archived
    v3 = await ready_version(raw_db, base, w, tmp_path, status="ready", chunks=1, version_no=3)
    assert (await publish(client, w, w.t1, v3)).status_code == 200  # v2 is archived
    v4 = await add_version(raw_db, base, w, tmp_path, status="failed", version_no=4)
    await raw_db.execute(
        text("UPDATE app.document_versions SET error_code = 'processing_failed' WHERE id = :v"), {"v": v4.version.id}
    )
    await raw_db.commit()

    resp = await client.get(_url(base.document.id), headers=auth(w, w.t1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "has_more"} and body["has_more"] is False
    assert [(i["version_no"], i["status"]) for i in body["items"]] == [
        (4, "failed"), (3, "published"), (2, "archived"), (1, "archived")
    ]
    assert all(set(i) == VERSION_KEYS for i in body["items"])
    assert [i["retryable"] for i in body["items"]] == [True, None, None, None]
    # Nothing that locates or identifies the stored file
    for secret in ("storage_key", "sha256", v3.version.storage_key):
        assert secret not in resp.text

    # The same objects as on the detail route (one response builder), so the UI can trust either
    detail = (await client.get(f"/api/v1/documents/{base.document.id}", headers=auth(w, w.t1))).json()
    assert body["items"][0] == detail["latest_version"]
    assert body["items"][1] == detail["published_version"]


async def test_a_document_without_versions_gives_an_empty_list(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    """A document whose only version was deleted shows latest_version null; its list is empty, not an error."""
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    resp = await client.get(_url(base.document.id), headers=auth(w, w.t1))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "has_more": False}


async def test_the_list_is_cut_at_100_and_says_so(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w = academic_world.a
    base = await base_document(raw_db, w, tmp_path)
    await _bulk_archived(raw_db, w, base, range(1, MAX_LISTED + 1))

    exactly = (await client.get(_url(base.document.id), headers=auth(w, w.t1))).json()
    assert len(exactly["items"]) == MAX_LISTED and exactly["has_more"] is False

    await _bulk_archived(raw_db, w, base, range(MAX_LISTED + 1, MAX_LISTED + 2))
    cut = (await client.get(_url(base.document.id), headers=auth(w, w.t1))).json()
    assert len(cut["items"]) == MAX_LISTED and cut["has_more"] is True
    # Newest first: the oldest version (1) is the one that is cut
    assert [i["version_no"] for i in cut["items"]] == list(range(MAX_LISTED + 1, 1, -1))


def test_the_new_get_is_not_an_upload_route_and_needs_no_registry_entry() -> None:
    path = f"/api/v1/documents/{uuid.uuid4()}/versions"
    assert match_upload_route("GET", path) is None
    assert match_upload_route("POST", path) is not None  # the replace upload is still registered
