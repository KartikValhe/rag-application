"""
tests/test_document_retryable.py
────────────────────────────────
`retryable` on DocumentVersionResponse (step 5e-1). There is ONE rule, document_upload.is_retryable, used by
the status route, the retry route and the version response. These tests show that the version in the list
and in the detail agrees with the status route for every status and every failure code, and that it is null
unless the version is failed.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import DOCUMENT_VERSION_STATUSES, DocumentVersion
from app.services import document_upload
from app.workers import errors as worker_errors
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth, make_document

# No module-level asyncio mark: asyncio_mode=auto runs the async tests, and the sync test here must not carry it.

# Every code a failed version can carry: the API's transient codes, the worker's permanent and transient codes,
# and a code nobody knows (a future one must default to "not retryable").
FAILURE_CODES = sorted(
    document_upload.TRANSIENT_ERROR_CODES
    | worker_errors.PERMANENT_ERROR_CODES
    | worker_errors.TRANSIENT_ERROR_CODES
    | {"some_future_code"}
)


def test_the_failure_codes_cover_both_retryable_and_permanent_ones() -> None:
    retryable = {c for c in FAILURE_CODES if document_upload.is_retryable("failed", c)}
    assert retryable == set(document_upload.TRANSIENT_ERROR_CODES) == {"processing_failed", "worker_crash"}
    assert len(FAILURE_CODES) > 30


async def _status_retryable(client: AsyncClient, w, user, document_id, version_id) -> bool:  # type: ignore[no-untyped-def]
    resp = await client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/status", headers=auth(w, user)
    )
    assert resp.status_code == 200, resp.text
    value = resp.json()["retryable"]
    assert isinstance(value, bool)
    return value


async def _detail_version(client: AsyncClient, w, user, document_id) -> dict:  # type: ignore[no-untyped-def,type-arg]
    resp = await client.get(f"/api/v1/documents/{document_id}", headers=auth(w, user))
    assert resp.status_code == 200, resp.text
    return resp.json()["latest_version"]  # type: ignore[no-any-return]


async def test_failed_version_retryable_matches_the_status_route_for_every_failure_code(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    mismatches: list[str] = []
    seen_true = seen_false = 0
    for code in FAILURE_CODES:
        await raw_db.execute(
            update(DocumentVersion).where(DocumentVersion.id == made.version.id).values(error_code=code)
        )
        await raw_db.commit()
        from_status = await _status_retryable(client, w, w.t1, made.document.id, made.version.id)
        version = await _detail_version(client, w, w.t1, made.document.id)
        if version["retryable"] is not from_status or from_status is not document_upload.is_retryable("failed", code):
            mismatches.append(f"{code}: status={from_status} detail={version['retryable']!r}")
        seen_true += from_status
        seen_false += not from_status
    assert mismatches == []
    assert seen_true == 2 and seen_false == len(FAILURE_CODES) - 2


@pytest.mark.parametrize("status", [s for s in DOCUMENT_VERSION_STATUSES if s != "failed"])
async def test_a_version_that_is_not_failed_has_retryable_null_in_detail_and_list(
    status: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status=status, error_code=None
    )
    # The status route keeps its own contract: a plain boolean, false for a version that is not failed
    assert await _status_retryable(client, w, w.t1, made.document.id, made.version.id) is False
    detail = await client.get(f"/api/v1/documents/{made.document.id}", headers=auth(w, w.t1))
    listing = await client.get("/api/v1/documents", headers=auth(w, w.t1))
    assert detail.status_code == 200 and listing.status_code == 200
    from_detail = detail.json()
    from_list = next(d for d in listing.json()["items"] if d["id"] == str(made.document.id))
    for body in (from_detail, from_list):
        versions = [v for v in (body["latest_version"], body["published_version"]) if v is not None]
        assert versions, status
        for v in versions:
            assert "retryable" in v and v["retryable"] is None, (status, v)


async def test_failed_version_in_the_list_carries_the_same_value_as_the_status_route(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    retryable = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    permanent = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, error_code="scanned_pdf"
    )
    items = {
        d["id"]: d["latest_version"]
        for d in (await client.get("/api/v1/documents", headers=auth(w, w.t1))).json()["items"]
    }
    assert items[str(retryable.document.id)]["retryable"] is True
    assert items[str(permanent.document.id)]["retryable"] is False
    for made, expected in ((retryable, True), (permanent, False)):
        assert await _status_retryable(client, w, w.t1, made.document.id, made.version.id) is expected


async def test_the_retry_route_follows_the_same_rule(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """A version the response calls retryable can be retried; one it calls permanent answers 409."""
    w = academic_world.a
    permanent = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, error_code="scanned_pdf"
    )
    resp = await client.post(
        f"/api/v1/documents/{permanent.document.id}/versions/{permanent.version.id}/retry", headers=auth(w, w.t1)
    )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "version_not_retryable"
    retryable = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)
    resp = await client.post(
        f"/api/v1/documents/{retryable.document.id}/versions/{retryable.version.id}/retry", headers=auth(w, w.t1)
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["retryable"] is None  # queued again: not failed any more


async def test_the_upload_response_carries_retryable_null(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    from tests.fixtures.documents import upload

    w = academic_world.a
    resp = await upload(client, w, w.t1)
    assert resp.status_code == 202, resp.text
    assert "retryable" in resp.json() and resp.json()["retryable"] is None
