"""
tests/fixtures/documents.py
───────────────────────────
Helpers for the document route tests (step 5b-1):

  - `document_store_root`: points the app's private document store at a fresh temp directory, so
    every test can list exactly which files exist.
  - `auth(w, user)`: headers for one user of the academic world.
  - `upload(...)` / `replace(...)`: the real multipart routes through the real app.
  - `make_document(...)`: rows (document, links, one version, its job) plus an optional stored
    file, written through the privileged session, for tests that start from an existing state.
  - `row_counts(...)`: how many document rows a tenant has (to prove "no rows were written").
"""

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import (
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
    StorageDeletion,
)
from app.db.models.user import User
from app.services import document_store
from tests.fixtures.academic_world import WorldTenant

TXT = b"Unit 1: Algebra\n\nLinear equations in one variable.\n"


@pytest.fixture
def document_store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "document_store"
    store = document_store.LocalPrivateStore(root)
    monkeypatch.setattr(document_store, "get_document_store", lambda: store)
    yield root


def auth(w: WorldTenant, user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {w.token(user)}", "X-Tenant-Slug": w.tenant.slug}


async def upload(
    client: AsyncClient,
    w: WorldTenant,
    user: User,
    *,
    subject: str = "Math",
    classes: tuple[str, ...] = ("10-A",),
    data: bytes = TXT,
    filename: str = "unit1.txt",
    title: str = "Unit 1",
    extra_class_ids: tuple[uuid.UUID, ...] = (),
) -> Response:
    class_ids = [str(w.classes[c].id) for c in classes] + [str(c) for c in extra_class_ids]
    return await client.post(
        "/api/v1/documents",
        headers=auth(w, user),
        data={"title": title, "subject_id": str(w.subjects[subject].id), "class_ids": class_ids},
        files={"file": (filename, data, "application/octet-stream")},
    )


async def replace(
    client: AsyncClient, w: WorldTenant, user: User, document_id: uuid.UUID, *, data: bytes, filename: str = "v.txt"
) -> Response:
    return await client.post(
        f"/api/v1/documents/{document_id}/versions",
        headers=auth(w, user),
        files={"file": (filename, data, "application/octet-stream")},
    )


@dataclass
class MadeDocument:
    document: Document
    version: DocumentVersion
    job: IngestJob


async def make_document(
    db: AsyncSession,
    w: WorldTenant,
    *,
    subject: str,
    classes: tuple[str, ...],
    uploader: User,
    status: str = "failed",
    error_code: str | None = "processing_failed",
    data: bytes | None = None,
    store_root: Path | None = None,
    page_count: int | None = None,
    title: str | None = None,
) -> MadeDocument:
    """A document with one version (default: failed, retryable) and its job. Commits."""
    body = data if data is not None else uuid.uuid4().hex.encode()
    doc = Document(
        tenant_id=w.id, subject_id=w.subjects[subject].id, title=title or f"{subject} notes", created_by=uploader.id
    )
    db.add(doc)
    await db.flush()
    db.add_all(
        DocumentClass(tenant_id=w.id, document_id=doc.id, class_id=w.classes[c].id, subject_id=w.subjects[subject].id)
        for c in classes
    )
    key = document_store.new_document_key(w.id)
    published = status == "published"
    version = DocumentVersion(
        tenant_id=w.id,
        document_id=doc.id,
        version_no=1,
        status=status,
        error_code=error_code if status == "failed" else None,
        file_kind="txt",
        original_filename="notes.txt",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        storage_key=key,
        page_count=page_count,
        # Ready and published versions were scanned by the worker (NULL would mean "never scanned", 5d-2b)
        flagged_chunk_count=0 if status in ("ready", "published") else None,
        uploaded_by=uploader.id,
        published_at=datetime.now(UTC) if published else None,
        published_by=uploader.id if published else None,
    )
    db.add(version)
    await db.flush()
    job_status = {"failed": "failed", "published": "done", "ready": "done"}.get(status, "queued")
    job = IngestJob(
        tenant_id=w.id,
        version_id=version.id,
        status=job_status,
        attempts=3 if status == "failed" else 0,
        last_error_code=error_code if status == "failed" else None,
    )
    db.add(job)
    await db.commit()
    if store_root is not None:
        await document_store.LocalPrivateStore(store_root).put(w.id, key, body)
    return MadeDocument(doc, version, job)


async def row_counts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, model in (
        ("documents", Document),
        ("document_classes", DocumentClass),
        ("document_versions", DocumentVersion),
        ("ingest_jobs", IngestJob),
        ("storage_deletions", StorageDeletion),
    ):
        counts[name] = (
            await db.execute(select(func.count()).select_from(model).where(model.tenant_id == tenant_id))  # type: ignore[attr-defined]
        ).scalar_one()
    return counts


NO_ROWS = {"documents": 0, "document_classes": 0, "document_versions": 0, "ingest_jobs": 0, "storage_deletions": 0}
