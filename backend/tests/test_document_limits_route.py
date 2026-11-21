"""
tests/test_document_limits_route.py
───────────────────────────────────
GET /api/v1/documents/limits (step 5e-1): the EFFECTIVE upload limits of the caller's own tenant, for the
frontend's upload form. Role matrix, exact response shape, per-tenant override beats the platform default,
and no leak between two tenants that have different overrides. Nothing about usage, quotas or tenant ids.
"""

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.upload_security import PROFILE_RULES, UploadProfile
from app.services import document_limits
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth

pytestmark = pytest.mark.asyncio

URL = "/api/v1/documents/limits"
LIMITS_KEYS = {
    "max_file_bytes",
    "max_pdf_pages",
    "max_pptx_slides",
    "uploads_per_user_per_hour",
    "uploads_per_tenant_per_hour",
    "accepted_extensions",
}
# What the response must never carry: usage, quotas, ids and platform internals.
FORBIDDEN_KEYS = {
    "max_documents", "max_storage_bytes", "max_pages", "documents", "storage_bytes", "pages", "usage",
    "tenant_id", "overrides", "ceilings", "effective", "limits", "tenant",
}


async def _set_overrides(raw_db: AsyncSession, w: WorldTenant, overrides: dict[str, int]) -> None:
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": json.dumps(overrides), "id": w.id},
    )
    await raw_db.commit()


def _expected(overrides: dict[str, int] | None = None) -> dict[str, object]:
    eff = document_limits.merge_limits(overrides or {})
    return {
        "max_file_bytes": eff.max_file_bytes,
        "max_pdf_pages": eff.max_pdf_pages,
        "max_pptx_slides": eff.max_pptx_slides,
        "uploads_per_user_per_hour": eff.uploads_per_user_per_hour,
        "uploads_per_tenant_per_hour": eff.uploads_per_tenant_per_hour,
        "accepted_extensions": sorted(PROFILE_RULES[UploadProfile.DOCUMENT].formats),
    }


@pytest.fixture
def small_defaults(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Platform defaults that differ from the ceilings and from every override used below."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DOC_MAX_FILE_BYTES", 7_000_000)
    monkeypatch.setattr(settings, "DOC_MAX_PDF_PAGES", 111)
    monkeypatch.setattr(settings, "DOC_MAX_PPTX_SLIDES", 55)
    monkeypatch.setattr(settings, "DOC_UPLOAD_RATE_USER", 13)
    monkeypatch.setattr(settings, "DOC_UPLOAD_RATE_TENANT", 130)
    yield


# ── Role matrix (the generated matrix in test_document_route_matrix.py covers every route; these are the
#    cases that one cannot express) ──────────────────────────────────────────────


async def test_student_gets_403_and_no_limits(client: AsyncClient, academic_world: AcademicWorld) -> None:
    w = academic_world.a
    resp = await client.get(URL, headers=auth(w, w.s1))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "permission_denied"
    assert "max_file_bytes" not in resp.text


async def test_unauthenticated_gets_401(client: AsyncClient, academic_world: AcademicWorld) -> None:
    resp = await client.get(URL, headers={"X-Tenant-Slug": academic_world.a.tenant.slug})
    assert resp.status_code == 401, resp.text
    assert "max_file_bytes" not in resp.text


@pytest.mark.parametrize("role", ["admin", "t1", "t2"])
async def test_admin_and_teachers_get_the_limits(
    role: str, client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    resp = await client.get(URL, headers=auth(w, getattr(w, role)))
    assert resp.status_code == 200, resp.text
    assert resp.json() == _expected()


async def test_the_route_is_not_swallowed_by_the_document_id_route(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    """'limits' must reach this route, not GET /documents/{document_id} (which would answer 422)."""
    w = academic_world.a
    resp = await client.get(URL, headers=auth(w, w.admin))
    assert resp.status_code == 200
    assert "title" not in resp.json()


# ── Response shape ───────────────────────────────────────────────────────────


async def test_response_has_exactly_the_documented_keys_and_nothing_else(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    body = (await client.get(URL, headers=auth(w, w.t1))).json()
    assert set(body) == LIMITS_KEYS
    assert not FORBIDDEN_KEYS & set(body)
    for key in LIMITS_KEYS - {"accepted_extensions"}:
        assert type(body[key]) is int and body[key] >= 1, key
    assert body["accepted_extensions"] == ["docx", "md", "pdf", "pptx", "txt"]
    assert str(w.id) not in json.dumps(body)


async def test_the_platform_default_applies_without_an_override(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, small_defaults: None
) -> None:
    w = academic_world.a
    await _set_overrides(raw_db, w, {})
    body = (await client.get(URL, headers=auth(w, w.t1))).json()
    assert body["max_file_bytes"] == 7_000_000
    assert body["max_pdf_pages"] == 111
    assert body["max_pptx_slides"] == 55
    assert body["uploads_per_user_per_hour"] == 13
    assert body["uploads_per_tenant_per_hour"] == 130


async def test_a_tenant_override_beats_the_platform_default(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, small_defaults: None
) -> None:
    w = academic_world.a
    overrides = {"max_file_bytes": 1_234_567, "max_pdf_pages": 9, "uploads_per_user_per_hour": 4}
    await _set_overrides(raw_db, w, overrides)
    body = (await client.get(URL, headers=auth(w, w.admin))).json()
    assert body["max_file_bytes"] == 1_234_567
    assert body["max_pdf_pages"] == 9
    assert body["uploads_per_user_per_hour"] == 4
    # Keys without an override keep the platform default
    assert body["max_pptx_slides"] == 55
    assert body["uploads_per_tenant_per_hour"] == 130


async def test_quota_overrides_are_not_exposed(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """max_documents, max_storage_bytes and max_pages may be overridden, but are not part of this response."""
    w = academic_world.a
    await _set_overrides(raw_db, w, {"max_documents": 3, "max_storage_bytes": 4096, "max_pages": 17})
    resp = await client.get(URL, headers=auth(w, w.t1))
    assert set(resp.json()) == LIMITS_KEYS
    for secret in ("4096", '"max_documents"', '"max_pages"'):
        assert secret not in resp.text


# ── No leak between tenants ──────────────────────────────────────────────────


async def test_two_tenants_with_different_overrides_each_see_only_their_own(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, small_defaults: None
) -> None:
    a, b = academic_world.a, academic_world.b
    await _set_overrides(raw_db, a, {"max_file_bytes": 1_111_111, "max_pptx_slides": 21})
    await _set_overrides(raw_db, b, {"max_file_bytes": 2_222_222, "uploads_per_tenant_per_hour": 77})
    body_a = (await client.get(URL, headers=auth(a, a.t1))).json()
    body_b = (await client.get(URL, headers=auth(b, b.admin))).json()
    assert body_a["max_file_bytes"] == 1_111_111 and body_a["max_pptx_slides"] == 21
    assert body_a["uploads_per_tenant_per_hour"] == 130  # B's override never reaches A
    assert body_b["max_file_bytes"] == 2_222_222 and body_b["uploads_per_tenant_per_hour"] == 77
    assert body_b["max_pptx_slides"] == 55  # A's override never reaches B
    for text_of, foreign in ((json.dumps(body_a), ("2222222", "77")), (json.dumps(body_b), ("1111111", "21"))):
        for number in foreign:
            assert f": {number}" not in text_of


async def test_a_token_of_one_tenant_never_reads_another_tenants_limits(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """The tenant comes from the token, so naming the other tenant in the request cannot select its limits:
    the call is refused or answers with the token's OWN limits, never the named tenant's."""
    a, b = academic_world.a, academic_world.b
    await _set_overrides(raw_db, a, {"max_file_bytes": 1_111_111})
    await _set_overrides(raw_db, b, {"max_file_bytes": 3_333_333})
    headers = {"Authorization": f"Bearer {a.token(a.admin)}", "X-Tenant-Slug": b.tenant.slug}
    resp = await client.get(URL, headers=headers)
    assert "3333333" not in resp.text
    assert resp.status_code in (401, 403, 404) or resp.json()["max_file_bytes"] == 1_111_111, resp.text
    # ?tenant= (the development switch) is no way in either
    resp = await client.get(f"{URL}?tenant={b.tenant.slug}", headers=auth(a, a.admin))
    assert "3333333" not in resp.text
    assert resp.status_code in (401, 403, 404) or resp.json()["max_file_bytes"] == 1_111_111, resp.text
