"""
GET /api/v1/documents/{document_id}/versions/{version_id}/flags (step 5d-2, plan Q4): who may read it. Gate tier
(db, security). The response contract (keys, excerpts, spans per code, paging, title flags, read-only) is in
tests/test_document_flags_contract.py (full tier).

Read-only, document:view, the same scope and generic-404 rules as the other version routes. The excerpts and
spans are UNTRUSTED text.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.rbac import Role, RolePermission, UserRole
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth
from tests.fixtures.flags_api import flagged_document, flags_url
from tests.security.document_assertions import GENERIC_404_BODY, assert_generic_404
from tests.security.header_assertions import assert_security_headers


async def test_out_of_scope_foreign_and_unknown_versions_get_the_generic_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    doc = await flagged_document(raw_db, a)
    other = await flagged_document(raw_db, a)
    theirs = await flagged_document(raw_db, b)
    cases = [
        (a.t2, flags_url(doc)),  # T2 has no pair of this document
        (a.t1, flags_url(doc, other.version.id)),  # a version of ANOTHER document
        (a.t1, flags_url(doc, uuid.uuid4())),
        (a.t1, f"/api/v1/documents/{uuid.uuid4()}/versions/{doc.version.id}/flags"),
        (a.admin, flags_url(theirs)),  # tenant B's document
    ]
    for user, url in cases:
        resp = await client.get(url, headers=auth(a, user))
        assert_generic_404(resp)
        assert resp.json() == GENERIC_404_BODY
        assert "Ignore all previous" not in resp.text
        assert_security_headers(resp.headers, label="flags 404")


async def test_a_student_given_document_view_by_a_custom_role_gets_the_generic_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Review of 5d-2: S1 is enrolled in 10-A, the document is linked to 10-A, and S1 holds document:view
    through a custom role. Document routes use the manage pairs, and a student has none: the generic 404."""
    w = academic_world.a
    doc = await flagged_document(raw_db, w, status="published")
    role = Role(tenant_id=w.id, name=f"student-doc-view-{uuid.uuid4().hex[:6]}", description="test", is_system=False)
    raw_db.add(role)
    await raw_db.flush()
    raw_db.add(RolePermission(tenant_id=w.id, role_id=role.id, permission_id="document:view"))
    raw_db.add(UserRole(tenant_id=w.id, user_id=w.s1.id, role_id=role.id))
    await raw_db.commit()
    resp = await client.get(flags_url(doc), headers=auth(w, w.s1))
    assert_generic_404(resp)
    assert "Ignore all previous" not in resp.text


async def test_student_403_and_no_token_401(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    doc = await flagged_document(raw_db, w, status="published")
    assert (await client.get(flags_url(doc), headers=auth(w, w.s1))).status_code == 403
    assert (await client.get(flags_url(doc), headers={"X-Tenant-Slug": w.tenant.slug})).status_code == 401
