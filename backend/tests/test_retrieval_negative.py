"""
tests/test_retrieval_negative.py
────────────────────────────────
Negative cases from the review of step 5d-1 (5d-1b):

  * access revoked between two searches (a teacher assignment, an enrollment, an offered class_subjects
    row, which cascades to document_classes): the canary disappears, also for an admin narrowing to the pair;
  * hostile input into search(): NUL, a lone surrogate, C1 controls, a query without words ("???"),
    k <= 0 or too large, an embedder that returns a zero vector;
  * vectors with the active model key but another dimension are ignored on the exact and the HNSW path;
  * a published v1 and a ready v2 of one document: read sees v1 only, manage with include_drafts sees both;
  * a scope of tenant A used in a session of tenant B is refused.
"""

import dataclasses
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.academic import ClassSubject, Enrollment, TeacherAssignment
from app.db.models.document import DocumentClass
from app.ml.embedder import FakeEmbedder
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
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
    assert_present,
    run_search,
    search_config,
)


def _canary() -> str:
    return f"zq{uuid.uuid4().hex[:10]}"


async def _canary_doc(db: AsyncSession, w: WorldTenant) -> tuple[SeededDocument, str]:
    canary = _canary()
    doc = await seed_document(db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=fake_embedder(),
                              chunks=[f"Revocable lesson {canary}."])
    await seed_distractors(db, w, subject="Math", classes=("10-B",))  # still in T1's and the admin's scope
    return doc, canary


# ── access revoked between two searches ──────────────────────────────────────


async def test_removing_a_teacher_assignment_removes_the_canary(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    doc, canary = await _canary_doc(raw_db, w)
    query = f"{canary} {DISTRACTOR_WORD}"
    assert_present(await run_search(w, w.t1, query, fake_embedder()), doc.chunk_id(0), "before the revocation")
    class_id, subject_id = w.pair("10-A", "Math")
    await raw_db.execute(delete(TeacherAssignment).where(
        TeacherAssignment.tenant_id == w.id, TeacherAssignment.user_id == w.t1.id,
        TeacherAssignment.class_id == class_id, TeacherAssignment.subject_id == subject_id,
    ))
    await raw_db.commit()
    after = await run_search(w, w.t1, query, fake_embedder())
    assert_absent(after, doc, "T1 no longer teaches 10-A Math", canary=canary)
    assert after.hits, "T1 still searches the other pairs"
    with pytest.raises(HTTPException) as exc:
        await run_search(w, w.t1, query, fake_embedder(), class_id=class_id, subject_id=subject_id)
    assert exc.value.status_code == 404


async def test_removing_an_enrollment_removes_the_canary(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    doc, canary = await _canary_doc(raw_db, w)
    assert_present(await run_search(w, w.s1, canary, fake_embedder(), purpose="read"), doc.chunk_id(0), "enrolled")
    await raw_db.execute(delete(Enrollment).where(
        Enrollment.tenant_id == w.id, Enrollment.user_id == w.s1.id, Enrollment.class_id == w.classes["10-A"].id
    ))
    await raw_db.commit()
    after = await run_search(w, w.s1, canary, fake_embedder(), purpose="read")
    assert_absent(after, doc, "S1 left 10-A", canary=canary)
    with pytest.raises(HTTPException):
        await run_search(w, w.s1, canary, fake_embedder(), purpose="read", class_id=w.classes["10-A"].id)


async def test_removing_an_offered_pair_removes_the_canary_also_for_an_admin(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Deleting the class_subjects row cascades to document_classes (and teacher_assignments): the document
    has no link left to that pair, so nobody reaches it through it."""
    w = academic_world.a
    doc, canary = await _canary_doc(raw_db, w)
    class_id, subject_id = w.pair("10-A", "Math")
    for user in (w.admin, w.t1):
        assert_present(await run_search(w, user, canary, fake_embedder(), class_id=class_id, subject_id=subject_id),
                       doc.chunk_id(0), "before the pair is removed")
    await raw_db.execute(delete(ClassSubject).where(
        ClassSubject.tenant_id == w.id, ClassSubject.class_id == class_id, ClassSubject.subject_id == subject_id
    ))
    await raw_db.commit()
    links = (await raw_db.execute(DocumentClass.__table__.select().where(DocumentClass.document_id == doc.document.id))).all()
    assert links == [], "precondition: the cascade removed the document's link"
    for user in (w.admin, w.t1):
        assert_absent(await run_search(w, user, f"{canary} {DISTRACTOR_WORD}", fake_embedder()), doc,
                      "the pair is no longer offered", canary=canary)
        with pytest.raises(HTTPException) as exc:
            await run_search(w, user, canary, fake_embedder(), class_id=class_id, subject_id=subject_id)
        assert exc.value.status_code == 404


# ── hostile input into search() ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["frac\x00tions", "frac\ud800tions", "frac\x85tions\x9f", "\x00fractions\x00"],
    ids=["nul", "lone-surrogate", "c1-controls", "nul-around"],
)
async def test_control_characters_and_surrogates_are_cleaned_before_the_database(
    raw: str, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc = await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                              chunks=["Fractions are parts of a whole.", "Tions are not a word."])
    embedder = fake_embedder()
    result = await run_search(w, w.t1, raw, embedder)
    assert result.candidate_count == 2
    assert all("\x00" not in q and "\ud800" not in q and "\x85" not in q for q in embedder.received_queries)
    if raw.startswith("\x00"):
        assert result.hits[0].chunk_id == doc.chunk_id(0)


@pytest.mark.parametrize("raw", ["???", "!!! ...", "\x00\x00", "— – ·"])
async def test_a_query_without_words_is_invalid_and_never_embedded(
    raw: str, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                        chunks=["Some text."])
    embedder = fake_embedder()
    with pytest.raises(retrieval.QueryError) as exc:
        await run_search(w, w.t1, raw, embedder)
    assert exc.value.code == "invalid_query"
    assert embedder.received_queries == [], "no embedding for a query without words"


@pytest.mark.parametrize("k", [0, -3, 51])
async def test_k_outside_1_to_50_is_refused(k: int, academic_world: AcademicWorld) -> None:
    with pytest.raises(ValueError, match="k must"):
        await run_search(academic_world.a, academic_world.a.t1, "fractions", fake_embedder(), k=k)


class _ZeroEmbedder(FakeEmbedder):
    def embed_query(self, text: str) -> list[float]:
        self.received_queries.append(text)
        return [0.0] * self.dim


async def test_an_embedder_that_returns_a_zero_vector_is_search_unavailable(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                        chunks=["Fractions."])
    with pytest.raises(retrieval.SearchUnavailable) as exc:
        await run_search(w, w.t1, "fractions", _ZeroEmbedder(dim=E5_SMALL_DIM, model_id=E5_SMALL_MODEL_KEY))
    assert exc.value.code == "search_unavailable"


# ── same model key, other dimension ──────────────────────────────────────────


@pytest.mark.parametrize("hnsw", [False, True])
async def test_vectors_of_the_same_model_key_with_another_dimension_are_ignored(
    hnsw: bool, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """(1) A query embedder with the e5-small key but dimension 8 never compares with the stored 384-dim rows
    (the partial HNSW index makes an e5-small row of another dimension impossible to store). (2) Under a
    custom key with rows of two dimensions, each query dimension sees only its own rows."""
    w = academic_world.a
    cfg = dataclasses.replace(search_config(), exact_max_candidates=0 if hnsw else 50_000)
    e5 = await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                             chunks=["Dimension test fractions."])
    small = ScriptedEmbedder({}, dim=8, model_id=E5_SMALL_MODEL_KEY)
    result = await run_search(w, w.t1, "fractions", small, config=cfg)
    assert result.path == ("hnsw" if hnsw else "exact")
    assert result.vector_ranked == () and result.keyword_ranked == (e5.chunk_id(0),)

    key = f"custom/model@{uuid.uuid4().hex[:6]}"
    mixed = await seed_document(
        raw_db, w, subject="Physics", classes=("10-A",), uploader=w.t1, model_key=key,
        chunks=[ChunkSpec("Eight dims.", vector=axis_vector(0.9, 1, dim=8)),
                ChunkSpec("Full dims.", vector=axis_vector(0.9, 1, dim=E5_SMALL_DIM))],
    )
    for dim, expected in ((8, mixed.chunk_id(0)), (E5_SMALL_DIM, mixed.chunk_id(1))):
        result = await run_search(w, w.t1, "dims", ScriptedEmbedder({}, dim=dim, model_id=key), config=cfg,
                                  subject_id=w.subjects["Physics"].id)
        assert result.vector_ranked == (expected,), (dim, result.vector_ranked)


# ── published v1 and ready v2 ────────────────────────────────────────────────


async def test_read_sees_the_published_version_and_drafts_add_the_ready_one(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    old, new = _canary(), _canary()
    v1 = await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=fake_embedder(),
                             chunks=[f"Version one {old}."])
    v2 = await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, embedder=fake_embedder(),
                             chunks=[f"Version two {new}."], status="ready", document=v1.document, version_no=2)
    query = f"{old} {new}"
    for user, purpose in ((w.s1, "read"), (w.t1, "manage")):
        result = await run_search(w, user, query, fake_embedder(), purpose=purpose)  # type: ignore[arg-type]
        assert_present(result, v1.chunk_id(0), f"{purpose}: v1 is published")
        assert_absent(result, v2, f"{purpose} without drafts: v2 is only ready", canary=new)
    drafts = await run_search(w, w.t1, query, fake_embedder(), include_drafts=True)
    assert {(h.version_no, h.version_status) for h in drafts.hits} == {(1, "published"), (2, "ready")}
    assert {h.document_id for h in drafts.hits} == {v1.document.id}


# ── a scope used in the wrong tenant session ─────────────────────────────────


async def test_a_scope_of_tenant_a_in_a_session_of_tenant_b_is_refused(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    canary = _canary()
    await seed_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.admin, embedder=fake_embedder(),
                        chunks=[f"Tenant B {canary}."])
    async with tenant_session_context(str(a.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, a.admin, purpose="manage")
    async with tenant_session_context(str(b.id)) as db:
        with pytest.raises(RuntimeError, match="session tenant"):
            await retrieval.search(db, scope, canary, retrieval.QueryEmbedder.from_embedder(fake_embedder()),
                                   retrieval.RetrievalConfig.from_settings())
