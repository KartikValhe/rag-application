"""
tests/test_retrieval_leaks.py
─────────────────────────────
What retrieval must never return (step 5d-1): another tenant's chunks (RLS + tenant filter), versions that
are not published (queued, processing, ready for the read purpose, failed, archived), soft-deleted
documents, excluded chunks and vectors of another embedding model.

Every leak check looks at the hits AND both ranked lists (tests/security/retrieval_assertions.py), and
every scope has DISTRACTOR_COUNT in-scope distractors that fill the six final hits, so a leaked chunk
ranked below the hits is still caught.

Publishing and readers:
  * an UNCOMMITTED publish is invisible: while the publish transaction is held between "old version
    archived" and "new version published", a reader still sees v1; after the commit it sees v2;
  * a publish COMMITTED between the count statement and the search statement of one search: the search
    statement reads one snapshot, so the result shows the old version or the new one, never neither.

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
from app.services import document_lifecycle, retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
    DISTRACTOR_WORD,
    ChunkSpec,
    SeededDocument,
    fake_embedder,
    seed_distractors,
    seed_document,
)
from tests.security.retrieval_assertions import (
    assert_absent,
    assert_canary_present,
    assert_only_chunks,
    run_search,
    search_config,
)

FINAL_K = retrieval.RetrievalConfig.from_settings().final_k


def _canary() -> str:
    return f"zq{uuid.uuid4().hex[:10]}"


async def _doc(db: AsyncSession, w: WorldTenant, canary: str, *, status: str = "published", **kw: Any) -> SeededDocument:
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
    noise = await seed_distractors(raw_db, a)
    theirs = await _doc(raw_db, b, "", chunks=[sentence, f"Tenant B only {b_only} text."])
    await seed_distractors(raw_db, b)
    allowed = {c.id for c in mine.chunks} | {c.id for c in noise.chunks}
    for user in (a.admin, a.t1):
        result = await run_search(a, user, f"{sentence} {b_only} {DISTRACTOR_WORD}", fake_embedder())
        assert_only_chunks(result, allowed, "tenant A searched")
        assert_absent(result, theirs, "tenant B's chunks", canary=b_only)
        assert len(result.hits) == FINAL_K, "the distractors fill the hits"
        assert result.hits[0].chunk_id == mine.chunk_id(0)


async def test_only_published_versions_are_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    canaries = {s: _canary() for s in ("published", "queued", "processing", "ready", "failed", "archived")}
    docs = {s: await _doc(raw_db, w, c, status=s) for s, c in canaries.items()}
    await seed_distractors(raw_db, w)
    query = " ".join([*canaries.values(), DISTRACTOR_WORD])

    for user, purpose in ((w.admin, "manage"), (w.t1, "manage"), (w.s1, "read")):
        result = await run_search(w, user, query, fake_embedder(), purpose=purpose)  # type: ignore[arg-type]
        assert_canary_present(result, canaries["published"], "published")
        for status_ in ("queued", "processing", "ready", "failed", "archived"):
            assert_absent(result, docs[status_], f"{status_} version, {purpose} purpose", canary=canaries[status_])
        assert {h.version_status for h in result.hits} == {"published"}

    drafts = await run_search(w, w.t1, query, fake_embedder(), include_drafts=True)
    assert docs["ready"].chunk_id(0) in drafts.vector_ranked, "include_drafts reaches ready versions"
    for status_ in ("queued", "processing", "failed", "archived"):
        assert_absent(drafts, docs[status_], f"{status_} version with include_drafts", canary=canaries[status_])


async def test_soft_deleted_documents_are_not_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    gone, kept = _canary(), _canary()
    deleted = await _doc(raw_db, w, gone)
    await _doc(raw_db, w, kept)
    await seed_distractors(raw_db, w)
    await raw_db.execute(
        update(Document).where(Document.tenant_id == w.id, Document.id == deleted.document.id)
        .values(deleted_at=datetime.now(UTC), deleted_by=w.admin.id)
    )
    await raw_db.commit()
    result = await run_search(w, w.admin, f"{gone} {kept} {DISTRACTOR_WORD}", fake_embedder())
    assert_absent(result, deleted, "soft-deleted document", canary=gone)
    assert_canary_present(result, kept, "live document")


async def test_excluded_chunks_are_not_returned(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    hidden, shown = _canary(), _canary()
    doc = await _doc(raw_db, w, "", chunks=[ChunkSpec(f"Excluded {hidden} text.", excluded=True), ChunkSpec(f"Kept {shown} text.")])
    await seed_distractors(raw_db, w)
    for drafts in (False, True):
        result = await run_search(w, w.t1, f"{hidden} {shown} {DISTRACTOR_WORD}", fake_embedder(), include_drafts=drafts)
        assert_absent(result, [doc.chunk_id(0)], "excluded chunk", canary=hidden)
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


async def test_an_uncommitted_publish_is_invisible_to_readers(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """A second connection searches while the publish transaction is held between "old version archived"
    and "new version published" (a barrier, no sleeps): it sees v1 before and in the middle, v2 after the
    commit. This shows that an uncommitted publish is invisible; the race with a COMMITTED publish inside
    one search is the next test."""
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


async def test_a_publish_committed_between_the_count_and_the_search_shows_one_version(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """The publish of v2 COMMITS after the count statement (which saw v1) and before the search statement.
    The search statement reads ONE snapshot (READ COMMITTED: a new snapshot per statement), so it shows
    the new version only: old or new, never neither and never both."""
    w = academic_world.a
    old, new = _canary(), _canary()
    v1 = await _doc(raw_db, w, old)
    v2 = await _doc(raw_db, w, new, status="ready", document=v1.document, version_no=2)
    published: list[bool] = []

    async def publish_and_commit() -> None:
        async with tenant_session_context(str(w.id)) as other:
            await document_lifecycle.publish_version(
                other, tenant_id=w.id, document_id=v1.document.id, version_id=v2.version.id, actor_id=w.admin.id
            )
        published.append(True)

    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.s1, purpose="read")
        real_execute = db.execute

        async def execute_then_publish(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = await real_execute(statement, *args, **kwargs)
            if "SELECT count(*) FROM cand" in str(statement) and not published:
                await publish_and_commit()
            return result

        db.execute = execute_then_publish  # type: ignore[method-assign]
        result = await retrieval.search(
            db, scope, f"{old} {new}", retrieval.QueryEmbedder.from_embedder(fake_embedder()),
            search_config(),
        )
    assert published == [True], "the publish ran between the two statements"
    assert result.candidate_count == 1, "the count statement saw v1 (one chunk)"
    found = " ".join(h.content for h in result.hits)
    old_seen, new_seen = old in found, new in found
    assert old_seen or new_seen, "a reader saw neither version"
    assert (old_seen, new_seen) == (False, True)
    assert {h.version_id for h in result.hits} == {v2.version.id}
