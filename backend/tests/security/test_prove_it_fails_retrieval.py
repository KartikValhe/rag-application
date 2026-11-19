"""
Prove-it-fails for the retrieval protections of step 5d-1 (design C2).

The leak and scope tests use the helpers in tests/security/retrieval_assertions.py. Here each protection
is switched off on its own with monkeypatch, the SAME scenario runs again, and the helper must RAISE. Every
test first runs the scenario with the protection ON (the helper passes), then OFF. Each scope also holds
in-scope distractors that fill the six final hits, so the helper must find a leak in the ranked lists.

  1. authorized_pairs returns every pair of the tenant        -> T2 finds T1's (10-A, Math) chunk
  2. the pair match becomes a class x subject cross product   -> T1 finds a (10-B, Physics) chunk
  3. the version status filter is removed                     -> queued/failed/archived chunks appear
  4. drafts are allowed for the read purpose                  -> a student finds a ready (unpublished) chunk
  5. the soft-delete filter is removed                        -> a deleted document's chunk appears
  6. the excluded filter is removed                           -> an excluded chunk appears
  7. tenant isolation has three layers; what each one adds:
       RLS (app_tenant, FORCE)  enforced by the database whatever SQL the application sends;
       the tenant filter        holds when a session is not under RLS (a superuser or owner session, a
                                policy bug); every table of the statement is filtered by :t;
       the pair scope           authorisation INSIDE the tenant; it also excludes other tenants, because
                                class and subject ids belong to one tenant.
     7a. tenant filter AND pair scope removed:
           as app_tenant                                      -> still no leak: RLS alone holds
           as the superuser (RLS bypassed)                    -> tenant B's chunk appears
     7b. ONLY the tenant filter removed, with RLS bypassed and a pair scope that names tenant B's pair
         (a broken scope builder): with the filter the chunk stays out, without it tenant B's chunk appears
  8. a leaked chunk ranked BELOW the final hits (rank 9 of the vector side, not in the hits) is still
     caught, because the helper reads the ranked lists too

TEST database only: the module fixture asserts current_database() ends in _test before anything runs.
"""

import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.tenant_db import tenant_session_context
from app.db.models.academic import ClassSubject
from app.db.models.document import Document
from app.services import document_scope, retrieval
from tests.conftest import _TestSession
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
    DISTRACTOR_COUNT,
    DISTRACTOR_WORD,
    ChunkSpec,
    ScriptedEmbedder,
    SeededDocument,
    axis_vector,
    fake_embedder,
    seed_distractors,
    seed_document,
)
from tests.security.retrieval_assertions import (
    assert_absent,
    contents,
    run_search,
    search_in_session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    finally:
        await engine.dispose()
    assert name.endswith("_test"), f"prove-it tests must run against the test database, not {name!r}"


def _canary() -> str:
    return f"zq{uuid.uuid4().hex[:10]}"


async def _doc(db: AsyncSession, w: WorldTenant, canary: str, *, subject: str = "Math",
               classes: tuple[str, ...] = ("10-A",), status: str = "published", **kw: Any) -> SeededDocument:
    chunks = kw.pop("chunks", None) or [f"Lesson with marker {canary}."]
    return await seed_document(db, w, subject=subject, classes=classes, uploader=w.admin, embedder=fake_embedder(),
                               status=status, chunks=chunks, **kw)


def _q(*words: str) -> str:
    return " ".join([*words, DISTRACTOR_WORD])


async def _on_then_off(check: Callable[[], Awaitable[None]], switch_off: Callable[[], None]) -> None:
    await check()  # protection ON: the helper passes
    switch_off()
    with pytest.raises(AssertionError, match="leaked"):
        await check()  # protection OFF: the same helper must notice the leak (not some other failure)


async def test_1_all_pairs_scope_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    doc = await _doc(raw_db, w, canary)
    await seed_distractors(raw_db, w, subject="Physics", classes=("10-B",))  # T2's pair

    async def check() -> None:
        result = await run_search(w, w.t2, _q(canary), fake_embedder())
        assert_absent(result, doc, "T2 and a 10-A Math chunk", canary=canary)

    async def all_pairs(db: Any, user: Any, *, purpose: Any) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
        rows = await db.execute(select(ClassSubject.class_id, ClassSubject.subject_id).where(ClassSubject.tenant_id == user.tenant_id))
        return frozenset((r[0], r[1]) for r in rows.all())

    await _on_then_off(check, lambda: monkeypatch.setattr(document_scope, "authorized_pairs", all_pairs))


async def test_2_cross_product_pair_match_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    doc = await _doc(raw_db, w, canary, subject="Physics", classes=("10-B",))
    await seed_distractors(raw_db, w)

    async def check() -> None:
        assert_absent(await run_search(w, w.t1, _q(canary), fake_embedder()), doc, "T1 and 10-B Physics", canary=canary)

    cross = "dc.class_id IN (SELECT class_id FROM scope) AND dc.subject_id IN (SELECT subject_id FROM scope)"
    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "PAIR_MATCH", cross))


async def test_3_status_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canaries = {s: _canary() for s in ("queued", "failed", "archived")}
    docs = {s: await _doc(raw_db, w, c, status=s) for s, c in canaries.items()}
    await seed_distractors(raw_db, w)

    async def check() -> None:
        result = await run_search(w, w.admin, _q(*canaries.values()), fake_embedder())
        for status_, canary in canaries.items():
            assert_absent(result, docs[status_], f"{status_} version", canary=canary)

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "STATUS_FILTER", "TRUE"))


async def test_4_drafts_for_the_read_purpose_are_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    doc = await _doc(raw_db, w, canary, status="ready")
    await seed_distractors(raw_db, w)

    async def check() -> None:
        result = await run_search(w, w.s1, _q(canary), fake_embedder(), purpose="read")
        assert_absent(result, doc, "a student and a ready (unpublished) version", canary=canary)

    await _on_then_off(
        check, lambda: monkeypatch.setattr(retrieval, "statuses_for", lambda purpose, include_drafts: ("published", "ready"))
    )


async def test_5_soft_delete_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    made = await _doc(raw_db, w, canary)
    await seed_distractors(raw_db, w)
    await raw_db.execute(
        update(Document).where(Document.tenant_id == w.id, Document.id == made.document.id)
        .values(deleted_at=datetime.now(UTC), deleted_by=w.admin.id)
    )
    await raw_db.commit()

    async def check() -> None:
        assert_absent(await run_search(w, w.admin, _q(canary), fake_embedder()), made, "soft-deleted document", canary=canary)

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "DELETED_FILTER", "TRUE"))


async def test_6_excluded_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    doc = await _doc(raw_db, w, canary, chunks=[ChunkSpec(f"Excluded {canary}.", excluded=True), ChunkSpec("Other text.")])
    await seed_distractors(raw_db, w)

    async def check() -> None:
        result = await run_search(w, w.admin, _q(canary), fake_embedder())
        assert_absent(result, [doc.chunk_id(0)], "excluded chunk", canary=canary)

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "EXCLUDED_FILTER", "TRUE"))


async def _privileged_session_for(tenant_id: uuid.UUID) -> Any:
    """The superuser session (RLS bypassed) with app.tenant_id set, as a broken deployment would have it."""
    session = _TestSession()
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    return session


async def test_7a_rls_alone_holds_and_bypassing_it_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = academic_world.a, academic_world.b
    foreign = _canary()
    theirs = await _doc(raw_db, b, foreign)
    await _doc(raw_db, a, _canary())
    await seed_distractors(raw_db, a)
    async with tenant_session_context(str(a.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, a.admin, purpose="manage", class_id=a.classes["10-A"].id)

    # Both application-level guards off: no tenant filter, no pair scope
    monkeypatch.setattr(retrieval, "TENANT_FILTER", "TRUE")
    monkeypatch.setattr(retrieval, "PAIR_MATCH", "TRUE")

    async with tenant_session_context(str(a.id)) as db:  # app_tenant, RLS on
        result = await search_in_session(db, scope, _q(foreign), fake_embedder())
    assert_absent(result, theirs, "RLS alone, tenant B's chunk", canary=foreign)
    assert result.hits, "the search itself worked (tenant A's own chunks)"

    privileged = await _privileged_session_for(a.id)  # superuser: RLS bypassed
    try:
        result = await search_in_session(privileged, scope, _q(foreign), fake_embedder())
    finally:
        await privileged.rollback()
        await privileged.close()
    with pytest.raises(AssertionError, match="leaked"):
        assert_absent(result, theirs, "RLS bypassed, tenant B's chunk", canary=foreign)


async def test_7b_the_tenant_filter_alone_holds_when_rls_and_the_pair_scope_fail(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RLS is bypassed (superuser session) and the scope builder is broken so that tenant A's scope names
    tenant B's (10-A, Math) pair. Only the tenant filter stands between tenant A and tenant B's chunk:
    with it the chunk stays out, and switching off ONLY the tenant filter lets it in."""
    a, b = academic_world.a, academic_world.b
    foreign = _canary()
    theirs = await _doc(raw_db, b, foreign)
    await seed_distractors(raw_db, a)
    b_pair, a_pair = b.pair("10-A", "Math"), a.pair("10-A", "Math")

    async def broken_pairs(db: Any, user: Any, *, purpose: Any) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
        return frozenset({a_pair, b_pair})

    monkeypatch.setattr(document_scope, "authorized_pairs", broken_pairs)

    async def check() -> None:
        privileged = await _privileged_session_for(a.id)
        try:
            scope = await retrieval.build_retrieval_scope(privileged, a.admin, purpose="manage")
            assert b_pair in scope.pairs, "precondition: the broken builder put tenant B's pair in the scope"
            result = await search_in_session(privileged, scope, _q(foreign), fake_embedder())
        finally:
            await privileged.rollback()
            await privileged.close()
        assert result.hits, "the search itself worked (tenant A's distractors)"
        assert_absent(result, theirs, "tenant filter only, tenant B's chunk", canary=foreign)

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "TENANT_FILTER", "TRUE"))


async def test_8_a_leak_ranked_below_the_final_hits_is_still_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the status filter off, a queued chunk leaks at vector rank DISTRACTOR_COUNT + 1, below the six
    final hits. A check of the hits alone would pass; the helper reads the ranked lists and raises."""
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=embedder,
        chunks=[ChunkSpec(f"Distractor {i} {DISTRACTOR_WORD}.", vector=axis_vector(0.95 - i * 0.01, 1 + i))
                for i in range(DISTRACTOR_COUNT)],
    )
    canary = _canary()
    leaked = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=embedder, status="queued",
        chunks=[ChunkSpec(f"Queued {canary}.", vector=axis_vector(0.10, 100))],
    )

    async def check() -> None:
        assert_absent(await run_search(w, w.admin, DISTRACTOR_WORD, embedder), leaked, "queued version", canary=canary)

    await check()
    monkeypatch.setattr(retrieval, "STATUS_FILTER", "TRUE")
    result = await run_search(w, w.admin, DISTRACTOR_WORD, embedder)
    assert all(canary not in c for c in contents(result)), "precondition: the leak is not in the hits"
    assert leaked.chunk_id(0) in result.vector_ranked, "precondition: the leak is in the vector side's list"
    assert result.vector_ranked.index(leaked.chunk_id(0)) == DISTRACTOR_COUNT
    with pytest.raises(AssertionError, match="leaked"):
        await check()
