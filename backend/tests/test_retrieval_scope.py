"""
tests/test_retrieval_scope.py
─────────────────────────────
The retrieval scope (step 5d-1, design A4/A5): built on the server from document_scope.authorized_pairs,
always (class, subject) PAIRS, never a cross product. Client-supplied class and subject ids can only
narrow the caller's own pairs; anything outside them is the generic 404.

World (tests/fixtures/academic_world.py):
  T1 teaches 10-A Math, 10-A Physics, 10-B Math.   T2 teaches 10-B Physics, 11-A Chemistry.
  S1 is enrolled in 10-A and 11-A.                 S2 in 10-B.

The vector side returns every in-scope chunk (up to 20), so "absent" really means "out of scope", not
"ranked too low".
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.core.tenant_db import tenant_session_context
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import SeededDocument, fake_embedder, seed_document
from tests.security.retrieval_assertions import (
    assert_canary_absent,
    assert_canary_present,
    run_search,
)


async def _doc(
    db: AsyncSession, w: WorldTenant, subject: str, classes: tuple[str, ...], status: str = "published"
) -> tuple[SeededDocument, str]:
    canary = f"zq{uuid.uuid4().hex[:10]}"
    made = await seed_document(
        db, w, subject=subject, classes=classes, uploader=w.admin, embedder=fake_embedder(), status=status,
        chunks=[f"Lesson text for {subject} with marker {canary} and ordinary words."],
    )
    return made, canary


async def _search(w: WorldTenant, user: object, query: str, **kw: object) -> retrieval.RetrievalResult:
    return await run_search(w, user, query, fake_embedder(), **kw)  # type: ignore[arg-type]


# ── pairs, not a cross product ───────────────────────────────────────────────


async def test_pair_level_scope_document_of_one_pair_is_not_returned_for_its_neighbours(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """THE pair test: a (10-A, Math) document is not returned for (10-A, Physics) nor for (10-B, Math)."""
    w = academic_world.a
    _, canary = await _doc(raw_db, w, "Math", ("10-A",))
    for cls, subj in (("10-A", "Physics"), ("10-B", "Math")):
        class_id, subject_id = w.pair(cls, subj)
        for user in (w.admin, w.t1):
            result = await _search(w, user, canary, class_id=class_id, subject_id=subject_id)
            assert_canary_absent(result, canary, f"narrowed to ({cls}, {subj})")
    class_id, subject_id = w.pair("10-A", "Math")
    assert_canary_present(await _search(w, w.t1, canary, class_id=class_id, subject_id=subject_id), canary, "own pair")


async def test_teacher_never_reaches_the_cross_product_pair(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    """T1 teaches Physics (in 10-A) and teaches in 10-B (Math), but never 10-B Physics."""
    w = academic_world.a
    _, canary = await _doc(raw_db, w, "Physics", ("10-B",))
    assert_canary_absent(await _search(w, w.t1, canary), canary, "T1 and a 10-B Physics document")
    assert_canary_present(await _search(w, w.t2, canary), canary, "T2 teaches 10-B Physics")


async def test_a_shared_document_is_found_from_each_linked_class_only(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    _, canary = await _doc(raw_db, w, "Math", ("10-A", "10-B"))
    for cls in ("10-A", "10-B"):
        class_id, subject_id = w.pair(cls, "Math")
        assert_canary_present(await _search(w, w.t1, canary, class_id=class_id, subject_id=subject_id), canary, cls)
        assert_canary_present(await _search(w, w.admin, canary, class_id=class_id), canary, f"admin, class {cls}")
    assert_canary_absent(await _search(w, w.admin, canary, class_id=w.classes["11-A"].id), canary, "11-A")
    assert_canary_absent(await _search(w, w.t2, canary), canary, "T2 does not teach Math")


async def test_students_read_only_their_enrolled_classes(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    _, only_10a = await _doc(raw_db, w, "Math", ("10-A",))
    _, only_10b = await _doc(raw_db, w, "Math", ("10-B",))
    s1 = await _search(w, w.s1, f"{only_10a} {only_10b}", purpose="read")
    assert_canary_present(s1, only_10a, "S1 is in 10-A")
    assert_canary_absent(s1, only_10b, "S1 is not in 10-B")
    s2 = await _search(w, w.s2, f"{only_10a} {only_10b}", purpose="read")
    assert_canary_present(s2, only_10b, "S2 is in 10-B")
    assert_canary_absent(s2, only_10a, "S2 is not in 10-A")


async def test_a_student_manages_nothing_and_a_viewer_without_pairs_gets_an_empty_scope(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    _, canary = await _doc(raw_db, w, "Math", ("10-A",))
    for user in (w.s1, w.viewer):
        result = await _search(w, user, canary)
        assert result.hits == () and result.reason == "empty_scope" and result.sufficient is False


# ── narrowing ────────────────────────────────────────────────────────────────


async def test_narrowing_outside_the_callers_pairs_is_the_generic_404(academic_world: AcademicWorld) -> None:
    w, other = academic_world.a, academic_world.b
    cases = [
        {"class_id": w.classes["11-A"].id},  # T1 has nothing in 11-A
        {"subject_id": w.subjects["Chemistry"].id},  # T1 does not teach Chemistry
        {"class_id": w.classes["10-B"].id, "subject_id": w.subjects["Physics"].id},  # both owned, the pair is not
        {"class_id": uuid.uuid4()},
        {"subject_id": uuid.uuid4()},
        {"class_id": other.classes["10-A"].id, "subject_id": other.subjects["Math"].id},  # another tenant's ids
    ]
    for narrowing in cases:
        with pytest.raises(HTTPException) as exc:
            await _search(w, w.t1, "anything", **narrowing)
        assert exc.value.status_code == 404 and exc.value.detail == GENERIC_NOT_FOUND, narrowing


async def test_narrowing_only_ever_shrinks_the_scope(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    async with tenant_session_context(str(w.id)) as db:
        full = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
        by_class = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage", class_id=w.classes["10-A"].id)
        by_subject = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage", subject_id=w.subjects["Math"].id)
    assert full.pairs == frozenset({w.pair("10-A", "Math"), w.pair("10-A", "Physics"), w.pair("10-B", "Math")})
    assert by_class.pairs == frozenset({w.pair("10-A", "Math"), w.pair("10-A", "Physics")})
    assert by_subject.pairs == frozenset({w.pair("10-A", "Math"), w.pair("10-B", "Math")})
    assert full.tenant_id == by_class.tenant_id == w.id
    assert full.statuses == ("published",)


async def test_drafts_only_for_the_manage_purpose(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage", include_drafts=True)
        assert scope.statuses == ("published", "ready")
        with pytest.raises(ValueError):
            await retrieval.build_retrieval_scope(db, w.s1, purpose="read", include_drafts=True)


async def test_the_scope_refuses_a_session_of_another_tenant(academic_world: AcademicWorld) -> None:
    """The tenant comes from the authenticated user; a session opened for another tenant is a bug."""
    a, b = academic_world.a, academic_world.b
    async with tenant_session_context(str(b.id)) as db:
        with pytest.raises(RuntimeError, match="session tenant"):
            await retrieval.build_retrieval_scope(db, a.t1, purpose="manage")


async def test_operator_scope_needs_an_offered_pair_of_that_tenant(academic_world: AcademicWorld) -> None:
    w, other = academic_world.a, academic_world.b
    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.RetrievalScope.for_operator(db, w.id, *w.pair("10-A", "Math"))
        assert scope.pairs == frozenset({w.pair("10-A", "Math")}) and scope.statuses == ("published",)
        for class_id, subject_id in (w.pair("11-A", "Math"), other.pair("10-A", "Math")):
            with pytest.raises(HTTPException) as exc:
                await retrieval.RetrievalScope.for_operator(db, w.id, class_id, subject_id)
            assert exc.value.status_code == 404
