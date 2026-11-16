"""
Review of 5b-1, finding 7: the compensating delete after a failed upload transaction must not delete the
file of a version whose COMMIT actually succeeded (a lost connection or a cancellation during COMMIT can
hide a successful commit). Before the stored file is deleted a FRESH session checks whether any version
references the storage key. Also: real parallel uploads end with consistent rows and no orphan file.
"""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api.v1 import documents as documents_api
from app.db.models.document import DocumentVersion
from app.services import document_limits
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import NO_ROWS, TXT, make_document, replace, row_counts, upload
from tests.security.document_assertions import (
    assert_error_code,
    assert_no_stored_files,
    assert_store_matches_rows,
)

pytestmark = pytest.mark.asyncio

REAL_SESSION_CONTEXT = documents_api.tenant_session_context  # before any test patches it


def fake_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/documents", "headers": [], "client": ("127.0.0.1", 1)})


def received(data: bytes = TXT) -> documents_api._ReceivedFile:
    return documents_api._ReceivedFile(
        data=data, filename="unit1.txt", kind="txt", sha256=hashlib.sha256(data).hexdigest()
    )


async def commit_new_document(w: WorldTenant, data: bytes = TXT) -> Any:
    return await documents_api._commit_version(
        fake_request(),
        w.t1,
        document_limits.default_limits(),
        received(data),
        new_document=documents_api._NewDocument(
            title="Unit 1", subject_id=w.subjects["Math"].id, class_ids=[w.classes["10-A"].id]
        ),
    )


class Recorder:
    """Stands in for the structlog logger: remembers event names."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def _log(self, event: str, **_: object) -> None:
        self.events.append(event)

    error = warning = info = _log


def break_the_upload_transaction(
    monkeypatch: pytest.MonkeyPatch, mode: str, *, check_fails: bool = False
) -> Recorder:
    """
    Wraps documents.tenant_session_context. Its FIRST call is the upload's own transaction; later calls are
    the fresh session of the cleanup check. Modes:
      rolled_back   the transaction fails: nothing is committed
      lost          the COMMIT succeeded, then the connection is reported lost (an error)
      cancelled     the COMMIT succeeded, then the task is cancelled
      cancelled_before_commit   the task is cancelled before the COMMIT: rolled back
    """
    real = REAL_SESSION_CONTEXT
    calls = {"n": 0}
    recorder = Recorder()
    monkeypatch.setattr(documents_api, "logger", recorder)

    @asynccontextmanager
    async def wrapper(tenant_id: str) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:  # the cleanup's fresh session
            if check_fails:
                raise OperationalError("SELECT 1", {}, Exception("database unavailable"))
            async with real(tenant_id) as db:
                yield db
            return
        if mode == "rolled_back":
            async with real(tenant_id) as db:
                yield db
                raise ConnectionError("the transaction failed")  # inside: rolls back
        elif mode == "cancelled_before_commit":
            async with real(tenant_id) as db:
                yield db
                asyncio.current_task().cancel()  # type: ignore[union-attr]
                await asyncio.sleep(0)
        else:
            async with real(tenant_id) as db:
                yield db
            # the COMMIT is done
            if mode == "cancelled":
                asyncio.current_task().cancel()  # type: ignore[union-attr]
                await asyncio.sleep(0)
            raise ConnectionError("connection lost while waiting for the COMMIT result")

    monkeypatch.setattr(documents_api, "tenant_session_context", wrapper)
    return recorder


async def _versions_keys(raw_db: AsyncSession, w: WorldTenant) -> list[str]:
    return list(
        (await raw_db.execute(select(DocumentVersion.storage_key).where(DocumentVersion.tenant_id == w.id))).scalars()
    )


async def run_lost_commit_scenario(
    monkeypatch: pytest.MonkeyPatch, w: WorldTenant, raw_db: AsyncSession, root: Path
) -> None:
    """
    The shared scenario (also used by the prove-it-fails test): the COMMIT succeeded but the caller got an
    error. The version exists, so its file must still be there.
    """
    break_the_upload_transaction(monkeypatch, "lost")
    with pytest.raises(ConnectionError):
        await commit_new_document(w)
    keys = await _versions_keys(raw_db, w)
    assert len(keys) == 1
    assert_store_matches_rows(root, keys)  # raises when the committed version lost its file


# ── The three cases of finding 7 ─────────────────────────────────────────────


async def test_when_the_commit_fails_the_file_is_removed(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    break_the_upload_transaction(monkeypatch, "rolled_back")
    with pytest.raises(ConnectionError):
        await commit_new_document(w)
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_when_the_commit_succeeded_but_the_response_was_lost_the_file_is_kept(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_lost_commit_scenario(monkeypatch, academic_world.a, raw_db, document_store_root)


async def test_when_the_commit_succeeded_but_the_request_was_cancelled_the_file_is_kept_and_the_cancellation_is_not_swallowed(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    break_the_upload_transaction(monkeypatch, "cancelled")
    task = asyncio.create_task(commit_new_document(w))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    keys = await _versions_keys(raw_db, w)
    assert len(keys) == 1
    assert_store_matches_rows(document_store_root, keys)


async def test_a_cancellation_before_the_commit_removes_the_file_and_is_not_swallowed(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    break_the_upload_transaction(monkeypatch, "cancelled_before_commit")
    task = asyncio.create_task(commit_new_document(w))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert await row_counts(raw_db, w.id) == NO_ROWS
    assert_no_stored_files(document_store_root)


async def test_when_the_check_itself_fails_the_file_is_kept_and_logged_for_orphan_cleanup(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    # The commit succeeded and the check fails: the version needs its file
    recorder = break_the_upload_transaction(monkeypatch, "lost", check_fails=True)
    with pytest.raises(ConnectionError):
        await commit_new_document(w)
    keys = await _versions_keys(raw_db, w)
    assert len(keys) == 1
    assert_store_matches_rows(document_store_root, keys)
    assert "document_orphan_check_failed" in recorder.events

    # The transaction failed and the check fails: nothing can tell, so the file stays (an orphan, backlog B41)
    recorder = break_the_upload_transaction(monkeypatch, "rolled_back", check_fails=True)
    with pytest.raises(ConnectionError):
        await commit_new_document(w, data=b"Unit 2: Geometry\n")
    assert len(await _versions_keys(raw_db, w)) == 1  # still only the first version
    on_disk = [p for p in document_store_root.rglob("*") if p.is_file()]
    assert len(on_disk) == 2  # the first version's file and the unreferenced one that was kept
    assert "document_orphan_check_failed" in recorder.events


# ── Real parallel uploads (through the real app) ─────────────────────────────


async def _set_limits(raw_db: AsyncSession, w: WorldTenant, **limits: int) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": json.dumps(limits), "id": w.id},
    )
    await raw_db.commit()


def _codes(responses: list[Any]) -> list[Any]:
    return sorted(r.status_code for r in responses)


async def test_two_parallel_versions_of_one_document_leave_one_new_version_and_no_orphan(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", data=TXT,
        store_root=document_store_root,
    )
    first, second = await asyncio.gather(
        replace(client, w, w.t1, made.document.id, data=b"Version two, first draft\n"),
        replace(client, w, w.t1, made.document.id, data=b"Version two, second draft\n"),
    )
    assert _codes([first, second]) == [202, 409]
    loser = first if first.status_code == 409 else second
    assert_error_code(loser, 409, "document_processing_in_progress")
    versions = (
        await raw_db.execute(select(DocumentVersion).where(DocumentVersion.document_id == made.document.id))
    ).scalars().all()
    assert sorted(v.version_no for v in versions) == [1, 2]
    jobs = (await raw_db.execute(text("SELECT count(*) FROM app.ingest_jobs WHERE tenant_id = :t"), {"t": w.id})).scalar_one()
    assert jobs == 2
    assert_store_matches_rows(document_store_root, [v.storage_key for v in versions])


async def test_the_same_bytes_uploaded_twice_in_parallel_leave_one_version_and_no_orphan(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published", data=TXT,
        store_root=document_store_root,
    )
    same = b"Identical replacement bytes\n"
    results = await asyncio.gather(
        replace(client, w, w.t1, made.document.id, data=same), replace(client, w, w.t1, made.document.id, data=same)
    )
    assert _codes(list(results)) == [202, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["code"] in ("duplicate_version", "document_processing_in_progress")
    versions = (
        await raw_db.execute(select(DocumentVersion).where(DocumentVersion.document_id == made.document.id))
    ).scalars().all()
    assert len(versions) == 2 and len({v.sha256 for v in versions}) == 2  # never two active rows with the same bytes
    assert_store_matches_rows(document_store_root, [v.storage_key for v in versions])


async def test_two_uploads_racing_at_the_documents_quota_admit_exactly_one(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    await _set_limits(raw_db, w, max_documents=1)
    first, second = await asyncio.gather(
        upload(client, w, w.t1, data=b"Algebra notes\n", title="Algebra"),
        upload(client, w, w.t1, data=b"Geometry notes\n", title="Geometry"),
    )
    assert _codes([first, second]) == [202, 422]
    assert_error_code(first if first.status_code == 422 else second, 422, "quota_documents_exceeded")
    counts = await row_counts(raw_db, w.id)
    assert counts["documents"] == 1 and counts["document_versions"] == 1 and counts["ingest_jobs"] == 1
    assert_store_matches_rows(document_store_root, await _versions_keys(raw_db, w))


async def test_two_uploads_racing_at_the_storage_quota_admit_exactly_one(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    body_a, body_b = b"A" * 600 + b"\n", b"B" * 600 + b"\n"
    await _set_limits(raw_db, w, max_storage_bytes=len(body_a) + 100)  # room for one of them
    first, second = await asyncio.gather(
        upload(client, w, w.t1, data=body_a, title="First"), upload(client, w, w.t1, data=body_b, title="Second")
    )
    assert _codes([first, second]) == [202, 422]
    assert_error_code(first if first.status_code == 422 else second, 422, "quota_storage_exceeded")
    assert_store_matches_rows(document_store_root, await _versions_keys(raw_db, w))

