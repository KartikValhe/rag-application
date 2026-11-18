"""
tests/test_document_scope.py
────────────────────────────
The ONE shared document scope function (design A4, app/services/document_scope.py), tested with the
realistic academic world (tests/fixtures/academic_world.py):

  T1 teaches 10-A Math, 10-A Physics, 10-B Math.   T2 teaches 10-B Physics, 11-A Chemistry.
  S1 is enrolled in 10-A and 11-A.                 S2 in 10-B.

Scope is always a set of (class, subject) PAIRS, never a cross product: T1 teaches Physics (in 10-A)
and teaches in 10-B (Math), but must never reach 10-B Physics.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.document import Document
from app.services import document_scope
from tests.fixtures.academic_world import (
    OFFERED_PAIRS,
    T1_PAIRS,
    T2_PAIRS,
    AcademicWorld,
    WorldTenant,
    create_document_graph,
)
from tests.security.document_assertions import assert_generic_404, assert_manage_blocked

# No module-level asyncio mark: asyncio_mode=auto already runs the async tests, and the sync tests here must not carry it.


def _pairs(w: WorldTenant, named: tuple[tuple[str, str], ...]) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
    return frozenset(w.pair(c, s) for c, s in named)


async def _pairs_for(w: WorldTenant, user: object, purpose: str) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
    async with tenant_session_context(str(w.id)) as db:
        return await document_scope.authorized_pairs(db, user, purpose=purpose)  # type: ignore[arg-type]


# ── authorized_pairs ─────────────────────────────────────────────────────────


async def test_admin_gets_every_offered_pair(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    for purpose in ("manage", "read"):
        assert await _pairs_for(w, w.admin, purpose) == _pairs(w, OFFERED_PAIRS)


async def test_teachers_get_exactly_their_assignment_pairs(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    for purpose in ("manage", "read"):
        t1 = await _pairs_for(w, w.t1, purpose)
        assert t1 == _pairs(w, T1_PAIRS)
        assert w.pair("10-B", "Physics") not in t1, "cross product: T1 must not reach 10-B Physics"
        assert await _pairs_for(w, w.t2, purpose) == _pairs(w, T2_PAIRS)


async def test_student_reads_enrolled_classes_times_that_class_subjects_and_manages_nothing(
    academic_world: AcademicWorld,
) -> None:
    w = academic_world.a
    s1_read = await _pairs_for(w, w.s1, "read")
    expected = {p for p in OFFERED_PAIRS if p[0] in ("10-A", "11-A")}
    assert s1_read == _pairs(w, tuple(expected))
    # 11-A does not offer Physics, so S1 never gets (11-A, Physics) although S1 has Physics in 10-A.
    assert w.pair("11-A", "Physics") not in s1_read
    assert await _pairs_for(w, w.s1, "manage") == frozenset()
    assert await _pairs_for(w, w.s2, "read") == _pairs(w, tuple(p for p in OFFERED_PAIRS if p[0] == "10-B"))


async def test_other_roles_get_nothing(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    for purpose in ("manage", "read"):
        assert await _pairs_for(w, w.viewer, purpose) == frozenset()


async def test_pairs_never_cross_tenants(academic_world: AcademicWorld) -> None:
    a, b = academic_world.a, academic_world.b
    # Even with the wrong tenant context, tenant B's admin sees nothing of tenant A and vice versa.
    async with tenant_session_context(str(a.id)) as db:
        assert await document_scope.authorized_pairs(db, b.admin, purpose="manage") == frozenset()
    all_b = await _pairs_for(b, b.admin, "manage")
    assert not (all_b & _pairs(a, OFFERED_PAIRS))


# ── authorize_document_scope (upload / link) ─────────────────────────────────


async def _authorize(w: WorldTenant, user: object, subject: str, classes: list[str]) -> None:
    async with tenant_session_context(str(w.id)) as db:
        await document_scope.authorize_document_scope(
            db, user, w.subjects[subject].id, [w.classes[c].id for c in classes]  # type: ignore[arg-type]
        )


async def test_t1_may_link_math_to_both_of_his_math_classes(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    await _authorize(w, w.t1, "Math", ["10-A", "10-B"])
    await _authorize(w, w.t1, "Physics", ["10-A"])


@pytest.mark.parametrize(
    ("subject", "classes"),
    [
        ("Physics", ["10-B"]),  # the cross-product trap
        ("Physics", ["10-A", "10-B"]),  # one bad pair spoils the request
        ("Math", ["11-A"]),  # a class outside his pairs
        ("Chemistry", ["11-A"]),  # T2's pair
        ("Math", []),  # no class at all
    ],
)
async def test_t1_is_refused_with_the_generic_404_outside_his_pairs(
    academic_world: AcademicWorld, subject: str, classes: list[str]
) -> None:
    w = academic_world.a
    with pytest.raises(HTTPException) as exc_info:
        await _authorize(w, w.t1, subject, classes)
    assert_generic_404(exc_info.value)


async def test_a_class_or_subject_of_another_tenant_is_a_generic_404(academic_world: AcademicWorld) -> None:
    a, b = academic_world.a, academic_world.b
    async with tenant_session_context(str(a.id)) as db:
        for subject_id, class_ids in (
            (a.subjects["Math"].id, [b.classes["10-A"].id]),
            (b.subjects["Math"].id, [a.classes["10-A"].id]),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await document_scope.authorize_document_scope(db, a.admin, subject_id, class_ids)
            assert_generic_404(exc_info.value)


async def test_admin_may_link_any_offered_pair_but_not_an_unoffered_one(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    await _authorize(w, w.admin, "Physics", ["10-A", "10-B"])
    with pytest.raises(HTTPException) as exc_info:
        await _authorize(w, w.admin, "Physics", ["11-A"])  # 11-A does not offer Physics
    assert_generic_404(exc_info.value)


async def test_students_and_viewers_can_never_link(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    for user in (w.s1, w.viewer):
        with pytest.raises(HTTPException) as exc_info:
            await _authorize(w, user, "Math", ["10-A"])
        assert_generic_404(exc_info.value)


# ── load_document_for ────────────────────────────────────────────────────────


async def _load(w: WorldTenant, user: object, document_id: uuid.UUID, action: str) -> document_scope.ScopedDocument:
    async with tenant_session_context(str(w.id)) as db:
        return await document_scope.load_document_for(db, user, document_id, action=action)  # type: ignore[arg-type]


async def test_shared_document_is_visible_but_not_manageable_for_a_partial_teacher(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    g = await create_document_graph(raw_db, w, subject_name="Physics", class_names=("10-A", "10-B"), uploader=w.admin)
    await raw_db.commit()
    # T1 teaches 10-A Physics only; T2 teaches 10-B Physics only.
    for teacher in (w.t1, w.t2):
        view = await _load(w, teacher, g.document.id, "view")
        assert view.document.id == g.document.id
        assert view.can_manage is False
        assert view.blocked_reason == "document_manage_requires_all_classes"
        assert view.linked == _pairs(w, (("10-A", "Physics"), ("10-B", "Physics")))
        for action in ("manage", "publish", "delete"):
            with pytest.raises(HTTPException) as exc_info:
                await _load(w, teacher, g.document.id, action)
            assert_manage_blocked(exc_info.value)
    # The admin can always manage.
    for action in ("view", "manage", "publish", "delete"):
        admin_view = await _load(w, w.admin, g.document.id, action)
        assert admin_view.can_manage is True and admin_view.blocked_reason is None


async def test_teacher_with_all_linked_pairs_manages_and_others_get_404(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    g = await create_document_graph(raw_db, w, subject_name="Math", class_names=("10-A", "10-B"), uploader=w.t1)
    await raw_db.commit()
    managed = await _load(w, w.t1, g.document.id, "manage")
    assert managed.can_manage is True and managed.blocked_reason is None
    # T2 has no linked pair at all: generic 404 for every action (not 403, nothing leaks).
    for user in (w.t2, w.viewer, w.s1):
        for action in ("view", "manage", "delete"):
            with pytest.raises(HTTPException) as exc_info:
                await _load(w, user, g.document.id, action)
            assert_generic_404(exc_info.value)


async def test_soft_deleted_unknown_and_foreign_documents_are_404_for_everyone(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    deleted = await create_document_graph(raw_db, a, subject_name="Math", class_names=("10-A",), uploader=a.t1)
    foreign = await create_document_graph(raw_db, b, subject_name="Math", class_names=("10-A",), uploader=b.t1)
    await raw_db.execute(
        update(Document)
        .where(Document.tenant_id == a.id, Document.id == deleted.document.id)
        .values(deleted_at=datetime.now(UTC), deleted_by=a.admin.id)
    )
    await raw_db.commit()
    for doc_id in (deleted.document.id, foreign.document.id, uuid.uuid4()):
        for user in (a.admin, a.t1):
            with pytest.raises(HTTPException) as exc_info:
                await _load(a, user, doc_id, "view")
            assert_generic_404(exc_info.value)


async def test_admin_still_sees_a_document_that_lost_all_its_classes(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """A document whose class links all cascaded away (backlog B19) stays reachable for the admin only."""
    w = academic_world.a
    g = await create_document_graph(raw_db, w, subject_name="Math", class_names=(), uploader=w.t1)
    await raw_db.commit()
    assert (await _load(w, w.admin, g.document.id, "manage")).can_manage is True
    with pytest.raises(HTTPException) as exc_info:
        await _load(w, w.t1, g.document.id, "view")
    assert_generic_404(exc_info.value)


def test_access_rule_is_one_pure_function() -> None:
    """The rule the list route and load_document_for share (no second copy of the logic)."""
    p1, p2, p3 = (uuid.uuid4(), uuid.uuid4()), (uuid.uuid4(), uuid.uuid4()), (uuid.uuid4(), uuid.uuid4())
    scope = document_scope.CallerScope(is_admin=False, pairs=frozenset({p1, p2}))
    assert scope.access(frozenset({p1, p2})) == (True, True)
    assert scope.access(frozenset({p1, p3})) == (True, False)
    assert scope.access(frozenset({p3})) == (False, False)
    assert scope.access(frozenset()) == (False, False)
    admin = document_scope.CallerScope(is_admin=True, pairs=frozenset())
    assert admin.access(frozenset()) == (True, True)
    assert admin.access(frozenset({p3})) == (True, True)
