"""
tests/test_document_versions_list_security.py
─────────────────────────────────────────────
GET /api/v1/documents/{document_id}/versions: scope, roles and tenant isolation (docs/backend-small-plan.md
item 2, tests 2, 3 and 4).

  2. The same generic 404, byte for byte, for an unknown id, another tenant's document, a teacher outside the
     pair and a soft-deleted document; and it is the very body GET /documents/{document_id} answers.
  3. A student and a role without document:view get 403.
  4. Another tenant's versions never appear. The versions query filters tenant_id explicitly and runs on the
     app_tenant role; the prove-it run removes the explicit filter and the RLS tests must still pass.

The _test database only.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.documents import _list_versions
from app.core.tenant_db import SessionLocal, tenant_session_context
from app.db.models.document import Document
from app.db.models.rbac import RolePermission
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth, make_document
from tests.fixtures.pipeline import add_version
from tests.security.document_assertions import (
    GENERIC_404_BODY,
    assert_error_code,
    assert_generic_404,
)

pytestmark = pytest.mark.asyncio


def _url(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/versions"


def _detail(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}"


# ── 2. One generic 404 ───────────────────────────────────────────────────────


async def test_every_way_of_not_seeing_a_document_gives_the_same_404_body_byte_for_byte(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    own = await make_document(raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, status="published")
    foreign = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published")
    deleted = await make_document(raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, status="published")
    await raw_db.execute(update(Document).where(Document.id == deleted.document.id).values(deleted_at=datetime.now(UTC)))
    await raw_db.commit()

    # (caller headers, document id): unknown id, another tenant's document (tenant A's admin and teacher),
    # a teacher outside the pair, a soft-deleted document (admin and owner)
    cases = {
        "unknown id": (auth(a, a.admin), uuid.uuid4()),
        "another tenant's document, as an admin": (auth(a, a.admin), foreign.document.id),
        "another tenant's document, as a teacher": (auth(a, a.t1), foreign.document.id),
        "a teacher outside the pair": (auth(a, a.t2), own.document.id),
        "a soft-deleted document, as an admin": (auth(a, a.admin), deleted.document.id),
        "a soft-deleted document, as its teacher": (auth(a, a.t1), deleted.document.id),
    }
    bodies: dict[str, bytes] = {}
    detail_bodies: dict[str, bytes] = {}
    for label, (headers, document_id) in cases.items():
        resp = await client.get(_url(document_id), headers=headers)
        assert_generic_404(resp)
        bodies[label] = resp.content
        detail_resp = await client.get(_detail(document_id), headers=headers)
        assert_generic_404(detail_resp)
        detail_bodies[label] = detail_resp.content

    # Byte for byte: one single body for every case, and it is the body of the detail route too
    assert len(set(bodies.values())) == 1, bodies
    assert set(bodies.values()) == set(detail_bodies.values())
    assert json.loads(next(iter(bodies.values()))) == GENERIC_404_BODY
    # ... and the owner still gets the list, so the 404s above are about scope, not about the route
    assert (await client.get(_url(own.document.id), headers=auth(a, a.t1))).status_code == 200


# ── 3. Roles ─────────────────────────────────────────────────────────────────


async def test_a_student_gets_403_and_no_versions(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published")
    resp = await client.get(_url(made.document.id), headers=auth(w, w.s1))  # S1 is enrolled in 10-A
    assert_error_code(resp, 403, "permission_denied")
    assert "items" not in resp.text and str(made.version.id) not in resp.text


async def test_a_role_without_document_view_gets_403_and_anonymous_gets_401(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published")
    # The viewer's custom role holds only document:view; take it away and the role holds nothing
    await raw_db.execute(
        delete(RolePermission).where(
            RolePermission.tenant_id == w.id,
            RolePermission.role_id == w.viewer_role.id,
            RolePermission.permission_id == "document:view",
        )
    )
    await raw_db.commit()
    resp = await client.get(_url(made.document.id), headers=auth(w, w.viewer))
    assert_error_code(resp, 403, "permission_denied")
    assert str(made.version.id) not in resp.text

    anon = await client.get(_url(made.document.id), headers={"X-Tenant-Slug": w.tenant.slug})
    assert anon.status_code == 401, anon.text


# ── 4. Tenant isolation ──────────────────────────────────────────────────────


async def test_each_tenant_lists_only_its_own_versions(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    doc_a = await make_document(raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, status="published")
    extra_a = await add_version(raw_db, doc_a, a, tmp_path, status="failed")
    doc_b = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published")
    extra_b = await add_version(raw_db, doc_b, b, tmp_path, status="failed")

    a_ids = {str(doc_a.version.id), str(extra_a.version.id)}
    b_ids = {str(doc_b.version.id), str(extra_b.version.id)}
    got_a = (await client.get(_url(doc_a.document.id), headers=auth(a, a.admin))).json()
    got_b = (await client.get(_url(doc_b.document.id), headers=auth(b, b.admin))).json()
    assert {i["id"] for i in got_a["items"]} == a_ids
    assert {i["id"] for i in got_b["items"]} == b_ids
    assert not a_ids & {i["id"] for i in got_b["items"]}
    assert all(i["document_id"] == str(doc_a.document.id) for i in got_a["items"])

    # A's token naming B's document is the generic 404, never B's versions
    assert_generic_404(await client.get(_url(doc_b.document.id), headers=auth(a, a.admin)))


async def test_row_level_security_alone_hides_another_tenants_versions_from_the_query(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """The versions query by itself, past the route's scope check: tenant A's session asked for tenant B's
    document (with B's id and with A's id) reads nothing, and a session with no tenant context reads nothing.
    This holds with or without the explicit tenant_id filter in the query: it is row-level security."""
    a, b = academic_world.a, academic_world.b
    doc_b = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published")

    async with tenant_session_context(str(b.id)) as session:  # control: the owner tenant sees its version
        assert [v.id for v in await _list_versions(session, b.id, doc_b.document.id, 10)] == [doc_b.version.id]
    async with tenant_session_context(str(a.id)) as session:
        assert await _list_versions(session, b.id, doc_b.document.id, 10) == []
    async with SessionLocal() as session, session.begin():  # app_tenant, no app.tenant_id at all
        assert await _list_versions(session, b.id, doc_b.document.id, 10) == []


async def test_the_explicit_filter_is_a_second_layer_on_top_of_rls(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Defense in depth, NOT the RLS proof: tenant B's session asked for B's own document under tenant A's id
    gets nothing because of the explicit `tenant_id = :t` filter. The prove-it run removes the filter and THIS is
    the test that must then fail, while the RLS tests above keep passing."""
    a, b = academic_world.a, academic_world.b
    doc_b = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published")
    async with tenant_session_context(str(b.id)) as session:
        assert await _list_versions(session, a.id, doc_b.document.id, 10) == []
