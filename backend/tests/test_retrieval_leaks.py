"""
tests/test_retrieval_leaks.py
─────────────────────────────
What retrieval must never return (step 5d-1): another tenant's chunks (RLS + tenant filter), versions that
are not published (queued, processing, ready for the read purpose, failed, archived), soft-deleted
documents, excluded chunks and vectors of another embedding model. Plus the atomic replace: while a new
version is being published, a reader sees the old version or the new one, never both and never neither.

Every search runs as app_tenant in a tenant transaction, exactly like the API.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.document import Document
from app.services import document_lifecycle
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import ChunkSpec, fake_embedder, seed_document
from tests.security.retrieval_assertions import (
    assert_canary_absent,
    assert_canary_present,
    assert_only_chunks,
    run_search,
)


def _canary() -> str:
    return f"zq{uuid.uuid4().hex[:10]}"


async def _doc(db: AsyncSession, w: WorldTenant, canary: str, *, status: str = "published", **kw: Any) -> Any:
    return await seed_document(
        db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=fake_embedder(), status=status,
        chunks=kw.pop("chunks", None) or [f"Chapter text with marker {canary} about fractions."], **kw,
    )


async def test_another_tenants_identical_sentence_is_never_returned(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    sentence = f"The shared sentence {_canary()} about prime numbers and divisors."
    b_only = _canary()
    mine = await _doc(raw_db, a, "", chunks=[sentence])
    await _doc(raw_db, b, "", chunks=[sentence, f"Tenant B only {b_only} text."])
    for user in (a.admin, a.t1):
        result = await run_search(a, user, f"{sentence} {b_only}", fake_embedder())
        assert_only_chunks(result, {c.id for c in mine.chunks}, "tenant A searched")
        assert_canary_absent(result, b_only, "tenant B's chunk")
        assert len(result.hits) == 1


async def test_only_published_versions_are_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    canaries = {s: _canary() for s in ("published", "queued", "processing", "ready", "failed", "archived")}
    for status_, canary in canaries.items():
        await _doc(raw_db, w, canary, status=status_)
    query = " ".join(canaries.values())

    for user, purpose in ((w.admin, "manage"), (w.t1, "manage"), (w.s1, "read")):
        result = await run_search(w, user, query, fake_embedder(), purpose=purpose)  # type: ignore[arg-type]
        assert_canary_present(result, canaries["published"], "published")
        for status_ in ("queued", "processing", "ready", "failed", "archived"):
            assert_canary_absent(result, canaries[status_], f"{status_} version, {purpose} purpose")
        assert {h.version_status for h in result.hits} == {"published"}

    drafts = await run_search(w, w.t1, query, fake_embedder(), include_drafts=True)
    assert_canary_present(drafts, canaries["ready"], "include_drafts shows ready versions")
    for status_ in ("queued", "processing", "failed", "archived"):
        assert_canary_absent(drafts, canaries[status_], f"{status_} version with include_drafts")
    assert {h.version_status for h in drafts.hits} == {"published", "ready"}


async def test_soft_deleted_documents_are_not_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    gone, kept = _canary(), _canary()
    deleted = await _doc(raw_db, w, gone)
    await _doc(raw_db, w, kept)
    await raw_db.execute(
        update(Document).where(Document.tenant_id == w.id, Document.id == deleted.document.id)
        .values(deleted_at=datetime.now(UTC), deleted_by=w.admin.id)
    )
    await raw_db.commit()
    result = await run_search(w, w.admin, f"{gone} {kept}", fake_embedder())
    assert_canary_absent(result, gone, "soft-deleted document")
    assert_canary_present(result, kept, "live document")


async def test_excluded_chunks_are_not_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    hidden, shown = _canary(), _canary()
    await _doc(raw_db, w, "", chunks=[ChunkSpec(f"Excluded {hidden} text.", excluded=True), ChunkSpec(f"Kept {shown} text.")])
    for drafts in (False, True):
        result = await run_search(w, w.t1, f"{hidden} {shown}", fake_embedder(), include_drafts=drafts)
        assert_canary_absent(result, hidden, "excluded chunk")
        assert_canary_present(result, shown, "chunk of the same version")


async def test_vectors_of_another_embedding_model_are_ignored(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    text_ = f"Vectors of another model {_canary()} here."
    other = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=fake_embedder(),
        model_key="other/model@0000", chunks=[text_],
    )
    mine = await _doc(raw_db, w, "", chunks=[f"Unrelated {_canary()} words."])
    # The same text as the query: under the active model this would be similarity 1.0
    result = await run_search(w, w.admin, text_, fake_embedder())
    by_id = {h.chunk_id: h for h in result.hits}
    assert other.chunk_id(0) in by_id, "the chunk is still found by the keyword side"
    assert by_id[other.chunk_id(0)].vector_rank is None and by_id[other.chunk_id(0)].vector_similarity is None
    assert result.vector_ranked == (mine.chunk_id(0),)


async def test_a_publish_swaps_versions_atomically_for_readers(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """A second connection searches while the publish transaction is held between "old version archived"
    and "new version published" (a barrier, no sleeps): it must see v1 before and in the middle, v2 after."""
    w = academic_world.a
    old, new = _canary(), _canary()
    v1 = await _doc(raw_db, w, old)
    v2 = await _doc(raw_db, w, new, status="ready", document=v1.document, version_no=2)
    query = f"{old} {new}"

    async def seen() -> tuple[bool, bool]:
        result = await run_search(w, w.s1, query, fake_embedder(), purpose="read")
        found = " ".join(h.content for h in result.hits)
        return old in found, new in found

    middle, observed = asyncio.Event(), asyncio.Event()
    real = document_lifecycle.mark_published

    async def held(*args: Any, **kwargs: Any) -> Any:
        middle.set()
        await asyncio.wait_for(observed.wait(), 30)
        return await real(*args, **kwargs)

    async def publish() -> None:
        async with tenant_session_context(str(w.id)) as db:
            await document_lifecycle.publish_version(
                db, tenant_id=w.id, document_id=v1.document.id, version_id=v2.version.id, actor_id=w.admin.id
            )

    document_lifecycle.mark_published = held  # type: ignore[assignment]
    try:
        before = await seen()
        task = asyncio.create_task(publish())
        await asyncio.wait_for(middle.wait(), 30)
        during = await seen()
        observed.set()
        await task
        after = await seen()
    finally:
        document_lifecycle.mark_published = real  # type: ignore[assignment]
    assert (before, during, after) == ((True, False), (True, False), (False, True))
