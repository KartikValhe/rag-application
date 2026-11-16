"""
Limits on the manual retry route (finding 6 of the review of 5b-1/5b-2).

`POST /documents/{id}/versions/{vid}/retry` used to reset the job to attempts = 0 with no limit at all.
Now it is rate-limited per user and per version (SlotManager, hourly windows, fail closed outside
development) and the total number of manual retries of one version is capped (`retry_limit_reached`).
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument, auth, make_document
from tests.security.document_assertions import assert_error_code


def _retry_url(made: MadeDocument) -> str:
    return f"/api/v1/documents/{made.document.id}/versions/{made.version.id}/retry"


async def _fail_again(raw_db: AsyncSession, made: MadeDocument) -> None:
    """Put the version back to its failed, retryable state (as if the worker failed it again)."""
    await raw_db.execute(
        text("UPDATE app.document_versions SET status = 'failed', error_code = 'processing_failed' WHERE id = :v"),
        {"v": made.version.id},
    )
    await raw_db.execute(
        text("UPDATE app.ingest_jobs SET status = 'failed', attempts = 3, last_error_code = 'processing_failed' WHERE version_id = :v"),
        {"v": made.version.id},
    )
    await raw_db.commit()


async def _failed(raw_db: AsyncSession, w: WorldTenant, store_root: Path) -> MadeDocument:
    return await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="failed",
        error_code="processing_failed", store_root=store_root,
    )


def _limits(monkeypatch: pytest.MonkeyPatch, *, user: int = 1000, version: int = 1000, total: int = 1000) -> None:
    monkeypatch.setattr(settings, "DOC_RETRY_RATE_USER", user)
    monkeypatch.setattr(settings, "DOC_RETRY_RATE_VERSION", version)
    monkeypatch.setattr(settings, "DOC_RETRY_MAX_PER_VERSION", total)


async def test_retries_of_one_version_are_rate_limited(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limits(monkeypatch, version=2)
    w = academic_world.a
    made = await _failed(raw_db, w, document_store_root)
    for _ in range(2):
        assert (await client.post(_retry_url(made), headers=auth(w, w.t1))).status_code == 202
        await _fail_again(raw_db, made)
    resp = await client.post(_retry_url(made), headers=auth(w, w.t1))
    assert_error_code(resp, 429, "retry_version_rate_limit_exceeded")
    assert int(resp.headers["retry-after"]) > 0
    status = (await raw_db.execute(text("SELECT status FROM app.document_versions WHERE id = :v"), {"v": made.version.id})).scalar_one()
    assert status == "failed"  # the refused retry changed nothing


async def test_retries_by_one_user_are_rate_limited_across_versions(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limits(monkeypatch, user=2)
    w = academic_world.a
    docs = [await _failed(raw_db, w, document_store_root) for _ in range(3)]
    assert (await client.post(_retry_url(docs[0]), headers=auth(w, w.t1))).status_code == 202
    assert (await client.post(_retry_url(docs[1]), headers=auth(w, w.t1))).status_code == 202
    assert_error_code(await client.post(_retry_url(docs[2]), headers=auth(w, w.t1)), 429, "retry_rate_limit_exceeded")
    # Another user has their own window
    assert (await client.post(_retry_url(docs[2]), headers=auth(w, w.admin))).status_code == 202


async def test_the_total_number_of_manual_retries_of_a_version_is_capped(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limits(monkeypatch, total=3)
    w = academic_world.a
    made = await _failed(raw_db, w, document_store_root)
    for _ in range(3):
        assert (await client.post(_retry_url(made), headers=auth(w, w.t1))).status_code == 202
        await _fail_again(raw_db, made)
    resp = await client.post(_retry_url(made), headers=auth(w, w.t1))
    assert_error_code(resp, 409, "retry_limit_reached")
    row = (await raw_db.execute(text("SELECT status, error_code FROM app.document_versions WHERE id = :v"), {"v": made.version.id})).one()
    assert tuple(row) == ("failed", "processing_failed")
    job = (await raw_db.execute(text("SELECT status, attempts FROM app.ingest_jobs WHERE version_id = :v"), {"v": made.version.id})).one()
    assert tuple(job) == ("failed", 3)  # the job was not reset
    # Another version of the same user is not affected by this one's cap
    other = await _failed(raw_db, w, document_store_root)
    assert (await client.post(_retry_url(other), headers=auth(w, w.t1))).status_code == 202


async def test_retry_rate_limit_fails_closed_without_redis_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    from fastapi import HTTPException

    from app.core import redis_client
    from app.services import document_upload

    async def no_redis() -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", no_redis)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    check: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]] = document_upload.check_retry_rate
    with pytest.raises(HTTPException) as exc_info:
        await check(uuid.uuid4(), uuid.uuid4())
    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.detail, dict) and exc_info.value.detail["code"] == "service_unavailable"
