"""
Prove-it-fails for the retrieval protections of step 5d-1 (design C2).

The leak and scope tests use the helpers in tests/security/retrieval_assertions.py. Here each protection
is switched off on its own with monkeypatch, the SAME scenario runs again, and the helper must RAISE. Every
test first runs the scenario with the protection ON (the helper passes), then OFF.

  1. authorized_pairs returns every pair of the tenant        -> T2 finds T1's (10-A, Math) chunk
  2. the pair match becomes a class x subject cross product   -> T1 finds a (10-B, Physics) chunk
  3. the version status filter is removed                     -> queued/failed/archived chunks appear
  4. drafts are allowed for the read purpose                  -> a student finds a ready (unpublished) chunk
  5. the soft-delete filter is removed                        -> a deleted document's chunk appears
  6. the excluded filter is removed                           -> an excluded chunk appears
  7. tenant filter AND pair scope removed:
       as app_tenant                                          -> still no leak: RLS alone holds
       as the superuser (RLS bypassed)                        -> tenant B's chunk appears

TEST database only: the module fixture asserts current_database() ends in _test before anything runs.
"""

import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.tenant_db import tenant_session_context
from app.db.models.document import Document
from app.services import document_scope, retrieval
from tests.conftest import _TestSession
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import ChunkSpec, fake_embedder, seed_document
from tests.security.retrieval_assertions import assert_canary_absent, run_search, search_in_session

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
               classes: tuple[str, ...] = ("10-A",), status: str = "published", **kw: Any) -> Any:
    chunks = kw.pop("chunks", None) or [f"Lesson with marker {canary}."]
    return await seed_document(db, w, subject=subject, classes=classes, uploader=w.admin, embedder=fake_embedder(),
                               status=status, chunks=chunks, **kw)


async def _on_then_off(check: Callable[[], Awaitable[None]], switch_off: Callable[[], None]) -> None:
    await check()  # protection ON: the helper passes
    switch_off()
    with pytest.raises(AssertionError):
        await check()  # protection OFF: the same helper must notice


async def test_1_all_pairs_scope_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    await _doc(raw_db, w, canary)

    async def check() -> None:
        assert_canary_absent(await run_search(w, w.t2, canary, fake_embedder()), canary, "T2 and a 10-A Math chunk")

    async def all_pairs(db: Any, user: Any, *, purpose: Any) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
        from sqlalchemy import select

        from app.db.models.academic import ClassSubject

        rows = await db.execute(select(ClassSubject.class_id, ClassSubject.subject_id).where(ClassSubject.tenant_id == user.tenant_id))
        return frozenset((r[0], r[1]) for r in rows.all())

    await _on_then_off(check, lambda: monkeypatch.setattr(document_scope, "authorized_pairs", all_pairs))


async def test_2_cross_product_pair_match_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    await _doc(raw_db, w, canary, subject="Physics", classes=("10-B",))

    async def check() -> None:
        assert_canary_absent(await run_search(w, w.t1, canary, fake_embedder()), canary, "T1 and 10-B Physics")

    cross = "dc.class_id IN (SELECT class_id FROM scope) AND dc.subject_id IN (SELECT subject_id FROM scope)"
    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "PAIR_MATCH", cross))


async def test_3_status_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canaries = {s: _canary() for s in ("queued", "failed", "archived")}
    for status_, canary in canaries.items():
        await _doc(raw_db, w, canary, status=status_)

    async def check() -> None:
        result = await run_search(w, w.admin, " ".join(canaries.values()), fake_embedder())
        for status_, canary in canaries.items():
            assert_canary_absent(result, canary, f"{status_} version")

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "STATUS_FILTER", "TRUE"))


async def test_4_drafts_for_the_read_purpose_are_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    await _doc(raw_db, w, canary, status="ready")

    async def check() -> None:
        result = await run_search(w, w.s1, canary, fake_embedder(), purpose="read")
        assert_canary_absent(result, canary, "a student and a ready (unpublished) version")

    await _on_then_off(
        check, lambda: monkeypatch.setattr(retrieval, "statuses_for", lambda purpose, include_drafts: ("published", "ready"))
    )


async def test_5_soft_delete_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    made = await _doc(raw_db, w, canary)
    await raw_db.execute(
        update(Document).where(Document.tenant_id == w.id, Document.id == made.document.id)
        .values(deleted_at=datetime.now(UTC), deleted_by=w.admin.id)
    )
    await raw_db.commit()

    async def check() -> None:
        assert_canary_absent(await run_search(w, w.admin, canary, fake_embedder()), canary, "soft-deleted document")

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "DELETED_FILTER", "TRUE"))


async def test_6_excluded_filter_removed_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = _canary()
    await _doc(raw_db, w, canary, chunks=[ChunkSpec(f"Excluded {canary}.", excluded=True), ChunkSpec("Other text.")])

    async def check() -> None:
        assert_canary_absent(await run_search(w, w.admin, canary, fake_embedder()), canary, "excluded chunk")

    await _on_then_off(check, lambda: monkeypatch.setattr(retrieval, "EXCLUDED_FILTER", "TRUE"))


async def test_7_rls_alone_holds_and_bypassing_it_is_caught(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = academic_world.a, academic_world.b
    foreign = _canary()
    await _doc(raw_db, b, foreign)
    await _doc(raw_db, a, _canary())
    async with tenant_session_context(str(a.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, a.admin, purpose="manage")

    # Both application-level guards off: no tenant filter, no pair scope
    monkeypatch.setattr(retrieval, "TENANT_FILTER", "TRUE")
    monkeypatch.setattr(retrieval, "PAIR_MATCH", "TRUE")

    async with tenant_session_context(str(a.id)) as db:  # app_tenant, RLS on
        result = await search_in_session(db, scope, foreign, fake_embedder())
    assert_canary_absent(result, foreign, "RLS alone, tenant B's chunk")
    assert result.hits, "the search itself worked (tenant A's own chunk)"

    async with _TestSession() as privileged:  # superuser: RLS bypassed
        result = await search_in_session(privileged, scope, foreign, fake_embedder())
    with pytest.raises(AssertionError):
        assert_canary_absent(result, foreign, "RLS bypassed, tenant B's chunk")
