"""
tests/test_academic_world.py
────────────────────────────
Smoke test for the reusable academic-world fixture (tests/fixtures/academic_world.py).
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from tests.fixtures.academic_world import (
    CLASS_NAMES,
    OFFERED_PAIRS,
    S1_CLASSES,
    S2_CLASSES,
    SUBJECT_NAMES,
    T1_PAIRS,
    T2_PAIRS,
    AcademicWorld,
    WorldTenant,
    create_document_graph,
)

pytestmark = pytest.mark.asyncio


async def _assignments(db: AsyncSession, w: WorldTenant, user_id: object) -> set[tuple[str, str]]:
    rows = (
        await db.execute(
            select(Class.name, Subject.name)
            .select_from(TeacherAssignment)
            .join(
                Class,
                (Class.tenant_id == TeacherAssignment.tenant_id) & (Class.id == TeacherAssignment.class_id),
            )
            .join(
                Subject,
                (Subject.tenant_id == TeacherAssignment.tenant_id)
                & (Subject.id == TeacherAssignment.subject_id),
            )
            .where(TeacherAssignment.tenant_id == w.id, TeacherAssignment.user_id == user_id)
        )
    ).all()
    return {(c, s) for c, s in rows}


async def _enrolled(db: AsyncSession, w: WorldTenant, user_id: object) -> set[str]:
    rows = (
        await db.execute(
            select(Class.name)
            .select_from(Enrollment)
            .join(
                Class,
                (Class.tenant_id == Enrollment.tenant_id) & (Class.id == Enrollment.class_id),
            )
            .where(Enrollment.tenant_id == w.id, Enrollment.user_id == user_id)
        )
    ).all()
    return {c for (c,) in rows}


async def test_world_builds_two_isolated_tenants(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    a, b = academic_world.a, academic_world.b
    assert a.id != b.id
    # Full-length random slugs and emails (32 hex characters), unique across tenants.
    assert a.tenant.slug != b.tenant.slug
    for w in (a, b):
        assert len(w.tenant.slug.rsplit("-", 1)[1]) == 32
        for user in (w.admin, w.t1, w.t2, w.s1, w.s2, w.viewer):
            assert len(user.email.split("@")[0].rsplit("-", 1)[1]) == 32
    assert len({u.email for u in (a.admin, a.t1, a.t2, a.s1, a.s2, a.viewer, b.admin, b.t1)}) == 8

    for w in (a, b):
        assert set(w.classes) == set(CLASS_NAMES)
        assert set(w.subjects) == set(SUBJECT_NAMES)
        n_classes = (
            await raw_db.execute(select(func.count()).select_from(Class).where(Class.tenant_id == w.id))
        ).scalar_one()
        n_subjects = (
            await raw_db.execute(select(func.count()).select_from(Subject).where(Subject.tenant_id == w.id))
        ).scalar_one()
        n_offered = (
            await raw_db.execute(
                select(func.count()).select_from(ClassSubject).where(ClassSubject.tenant_id == w.id)
            )
        ).scalar_one()
        assert (n_classes, n_subjects, n_offered) == (3, 4, len(OFFERED_PAIRS))


async def test_world_people_match_the_design(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    for w in (academic_world.a, academic_world.b):
        assert await _assignments(raw_db, w, w.t1.id) == set(T1_PAIRS)
        assert await _assignments(raw_db, w, w.t2.id) == set(T2_PAIRS)
        assert await _enrolled(raw_db, w, w.s1.id) == set(S1_CLASSES) == {"10-A", "11-A"}
        assert await _enrolled(raw_db, w, w.s2.id) == set(S2_CLASSES) == {"10-B"}
        assert await _assignments(raw_db, w, w.s1.id) == set()

        role_names = dict(
            (
                await raw_db.execute(
                    select(UserRole.user_id, Role.name)
                    .join(Role, (Role.tenant_id == UserRole.tenant_id) & (Role.id == UserRole.role_id))
                    .where(UserRole.tenant_id == w.id)
                )
            ).all()
        )
        assert role_names[w.admin.id] == "tenant_admin"
        assert role_names[w.t1.id] == role_names[w.t2.id] == "teacher"
        assert role_names[w.s1.id] == role_names[w.s2.id] == "student"
        assert role_names[w.viewer.id] == "doc-viewer"


async def test_document_graph_helper_builds_linked_rows(
    raw_db: AsyncSession, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    graph = await create_document_graph(
        raw_db, w, subject_name="Physics", class_names=("10-A", "10-B"), uploader=w.t1
    )
    assert graph.canary in graph.chunk.content
    assert graph.canary.startswith("CANARY-a-PHY-")
    assert graph.version.status == "published"
    assert graph.job.status == "queued"
    # The helper refuses nothing the schema forbids: 11-A does not offer Physics.
    with pytest.raises(Exception, match="fk_document_classes_class_subject"):  # noqa: PT012
        async with raw_db.begin_nested():
            await create_document_graph(
                raw_db, w, subject_name="Physics", class_names=("11-A",), uploader=w.t1
            )
