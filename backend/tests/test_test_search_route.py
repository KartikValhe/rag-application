"""
POST /api/v1/documents/test-search (step 5d-2, docs/phase5d-plan.md section 6): who may search and what they
can reach. This file is in the gate tier (db, security); the response contract (422 matrix, k, 503s, logging,
headers, body limit, the embedding cap) is in tests/test_test_search_contract.py (db only).

The order of checks is permission -> scope (generic 404) -> rate limit (429, 503 without Redis) -> query (422)
-> embedder (503) -> search. The class and subject ids only NARROW the caller's own pairs. include_drafts adds
ready versions, for teachers and admins only.

The query embedder is the `query_embedder` fixture (the FakeEmbedder behind the route's dependency).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.academic import TeacherAssignment
from app.db.models.rbac import Role, RolePermission, UserRole
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth
from tests.fixtures.search_api import (
    URL,
    EmbedderSwitch,
    canary,
    doc,
    hit_ids,
    search,
)
from tests.security.document_assertions import (
    GENERIC_404_BODY,
    assert_error_code,
    assert_generic_404,
)
from tests.security.header_assertions import assert_security_headers
from tests.security.scanner_assertions import assert_no_material

# ── Who may search ───────────────────────────────────────────────────────────


async def test_student_gets_403_and_no_token_gets_401(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch
) -> None:
    w = academic_world.a
    resp = await search(client, w, w.s1, query="waves")
    assert resp.status_code == 403
    anon = await client.post(URL, headers={"X-Tenant-Slug": w.tenant.slug}, json={"query": "waves"})
    assert anon.status_code == 401
    assert query_embedder.loads == 0


async def _grant_document_view(db: AsyncSession, w: WorldTenant) -> None:
    """A custom role holding document:view, given to student S1 (enrolled in 10-A and 11-A)."""
    role = Role(tenant_id=w.id, name=f"student-doc-view-{uuid.uuid4().hex[:6]}", description="test", is_system=False)
    db.add(role)
    await db.flush()
    db.add(RolePermission(tenant_id=w.id, role_id=role.id, permission_id="document:view"))
    db.add(UserRole(tenant_id=w.id, user_id=w.s1.id, role_id=role.id))
    await db.commit()


async def test_a_student_given_document_view_by_a_custom_role_reaches_no_material(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    """Review of 5d-2: the permission alone gives a student nothing. Test search uses the MANAGE pairs, and a
    student has none (only teachers and admins do), so the answer is an empty scope, and narrowing to the
    student's own enrolled class is the generic 404. Nothing is embedded."""
    w = academic_world.a
    word = canary()
    await doc(raw_db, w, word, classes=("10-A",))  # published, in S1's enrolled class
    await doc(raw_db, w, word, classes=("10-A",), status="ready")
    await _grant_document_view(raw_db, w)

    plain = await search(client, w, w.s1, query=word)
    assert_no_material(plain, word)
    assert (plain.json()["reason"], plain.json()["hits"]) == ("empty_scope", [])
    drafts = await search(client, w, w.s1, query=word, include_drafts=True)
    assert_no_material(drafts, word)
    narrowed = await search(client, w, w.s1, query=word, class_id=str(w.classes["10-A"].id))
    assert_generic_404(narrowed)
    assert query_embedder.queries == []


# ── Scope: only narrowing, the generic 404 ───────────────────────────────────


async def test_a_foreign_pair_class_or_subject_gives_the_same_generic_404_as_other_document_routes(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    a, b = academic_world.a, academic_world.b
    word = canary()
    await doc(raw_db, a, word, subject="Physics", classes=("10-B",))  # T2's pair, not T1's
    reference = await client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=auth(a, a.t1))
    assert reference.json() == GENERIC_404_BODY
    cases = [
        {"class_id": str(a.classes["11-A"].id)},  # T1 teaches nothing in 11-A
        {"subject_id": str(a.subjects["Chemistry"].id)},
        {"class_id": str(a.classes["10-B"].id), "subject_id": str(a.subjects["Physics"].id)},  # T2's pair
        {"class_id": str(a.classes["10-A"].id), "subject_id": str(a.subjects["English"].id)},  # offered, not taught
        {"class_id": str(uuid.uuid4())},
        {"class_id": str(b.classes["10-A"].id)},  # tenant B's class
        {"subject_id": str(b.subjects["Math"].id)},
    ]
    for narrowing in cases:
        resp = await search(client, a, a.t1, query=word, **narrowing)
        assert_generic_404(resp)
        assert resp.json() == reference.json(), narrowing
        assert word not in resp.text
    assert query_embedder.loads == 0  # nothing was embedded for a refused scope


async def test_narrowing_never_widens_the_callers_pairs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    """T1 teaches 10-A Math, 10-A Physics and 10-B Math. 10-B Physics belongs to T2 and stays out whatever T1
    sends; a class or subject narrows to the matching pairs only."""
    w = academic_world.a
    word = canary()
    docs = {
        pair: await doc(raw_db, w, word, subject=pair[1], classes=(pair[0],))
        for pair in (("10-A", "Math"), ("10-A", "Physics"), ("10-B", "Math"), ("10-B", "Physics"))
    }
    ids = {pair: str(d.chunk_id(0)) for pair, d in docs.items()}
    narrowings: list[tuple[dict[str, str], set[tuple[str, str]]]] = [
        ({}, {("10-A", "Math"), ("10-A", "Physics"), ("10-B", "Math")}),
        ({"class_id": str(w.classes["10-A"].id)}, {("10-A", "Math"), ("10-A", "Physics")}),
        ({"class_id": str(w.classes["10-B"].id)}, {("10-B", "Math")}),
        ({"subject_id": str(w.subjects["Physics"].id)}, {("10-A", "Physics")}),
        ({"subject_id": str(w.subjects["Math"].id)}, {("10-A", "Math"), ("10-B", "Math")}),
        ({"class_id": str(w.classes["10-A"].id), "subject_id": str(w.subjects["Math"].id)}, {("10-A", "Math")}),
    ]
    for narrowing, allowed in narrowings:
        resp = await search(client, w, w.t1, query=word, **narrowing)
        assert resp.status_code == 200, (narrowing, resp.text)
        got = {pair for pair, cid in ids.items() if cid in hit_ids(resp)}
        assert got == allowed, narrowing
        assert ids[("10-B", "Physics")] not in hit_ids(resp)


async def test_the_request_has_no_tenant_and_no_unknown_field(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch
) -> None:
    a, b = academic_world.a, academic_world.b
    for extra in ({"tenant_id": str(b.id)}, {"pairs": []}, {"purpose": "read"}):
        resp = await search(client, a, a.admin, query="waves", **extra)
        assert resp.status_code == 422, extra


async def test_an_assignment_revoked_between_the_scope_and_the_search(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    """Accepted gap (review of 5d-2, finding 6; plan section 14): the scope is read in the route's first
    transaction and the search runs in a second one after the query is embedded. An assignment revoked in
    between does not change the request already past its scope check: that ONE answer still shows the pair's
    material (the same as a request that finished a moment earlier). The very next request is refused. The
    revocation runs inside the embedding (a barrier, no sleep), so it lands exactly in the gap."""
    w = academic_world.a
    word = canary()
    pair_class = w.classes["10-B"]
    seeded = await doc(raw_db, w, word, subject="Math", classes=("10-B",))

    async def revoke() -> None:
        query_embedder.on_embed = None
        await raw_db.execute(
            delete(TeacherAssignment).where(
                TeacherAssignment.tenant_id == w.id,
                TeacherAssignment.user_id == w.t1.id,
                TeacherAssignment.class_id == pair_class.id,
                TeacherAssignment.subject_id == w.subjects["Math"].id,
            )
        )
        await raw_db.commit()

    query_embedder.on_embed = revoke
    in_gap = await search(client, w, w.t1, query=word, class_id=str(pair_class.id))
    assert in_gap.status_code == 200, in_gap.text
    assert str(seeded.chunk_id(0)) in hit_ids(in_gap)  # the documented one-request window

    after = await search(client, w, w.t1, query=word, class_id=str(pair_class.id))
    assert_generic_404(after)
    unnarrowed = await search(client, w, w.t1, query=word)
    assert str(seeded.chunk_id(0)) not in hit_ids(unnarrowed)


# ── Drafts ───────────────────────────────────────────────────────────────────


async def test_include_drafts_adds_ready_versions_only(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    w = academic_world.a
    word = canary()
    docs = {s: await doc(raw_db, w, word, status=s) for s in ("published", "ready", "queued", "processing", "failed", "archived")}
    ids = {s: str(d.chunk_id(0)) for s, d in docs.items()}
    for user in (w.admin, w.t1):
        plain = await search(client, w, user, query=word)
        assert {s for s, cid in ids.items() if cid in hit_ids(plain)} == {"published"}
        drafts = await search(client, w, user, query=word, include_drafts=True)
        assert {s for s, cid in ids.items() if cid in hit_ids(drafts)} == {"published", "ready"}
        statuses = {h["chunk_id"]: h["version_status"] for h in drafts.json()["hits"]}
        assert statuses[ids["ready"]] == "ready"
    for bad in ("yes", 1, None):
        assert (await search(client, w, w.admin, query=word, include_drafts=bad)).status_code == 422


# ── Order of the checks ──────────────────────────────────────────────────────


async def test_query_problems_come_after_the_scope_check(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch
) -> None:
    w = academic_world.a
    resp = await search(client, w, w.t1, query="x" * 501, class_id=str(w.classes["11-A"].id))
    assert_generic_404(resp)


async def test_the_rate_limit_is_checked_after_the_404(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probing foreign ids never uses up the window (and never tells the prober anything)."""
    monkeypatch.setattr(settings, "DOC_TEST_SEARCH_RATE_USER", 1)
    w = academic_world.a
    for _ in range(3):
        assert_generic_404(await search(client, w, w.t1, query="waves", class_id=str(w.classes["11-A"].id)))
    assert (await search(client, w, w.t1, query="waves")).status_code == 200
    assert (await search(client, w, w.t1, query="waves")).status_code == 429


async def test_the_rate_limit_is_counted_before_the_query_is_validated(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 5d-2: an unusable query still uses up the window (the rate limit comes BEFORE the query check),
    so a caller cannot make unlimited lexeme statements with "???". Swapping the two checks fails this test."""
    monkeypatch.setattr(settings, "DOC_TEST_SEARCH_RATE_USER", 1)
    w = academic_world.a
    assert_error_code(await search(client, w, w.t1, query="???"), 422, "invalid_query")
    assert_error_code(await search(client, w, w.t1, query="waves"), 429, "test_search_rate_limit_exceeded")
    assert query_embedder.queries == []


async def test_without_redis_outside_development_the_search_fails_closed_with_503(
    client: AsyncClient, academic_world: AcademicWorld, query_embedder: EmbedderSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from app.core import redis_client

    async def no_redis() -> Any:
        raise ConnectionError("redis down")

    w = academic_world.a
    token_headers = auth(w, w.t1)
    monkeypatch.setattr(redis_client, "get_redis", no_redis)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.post(URL, headers=token_headers, json={"query": "waves"})
    assert_error_code(resp, 503, "service_unavailable")
    assert query_embedder.loads == 0
    assert_security_headers(resp.headers, label="503 redis", hsts=True)
