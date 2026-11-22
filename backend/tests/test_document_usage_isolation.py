"""
tests/test_document_usage_isolation.py
──────────────────────────────────────
Tenant isolation of the two additive fields of GET /api/v1/admin/documents/usage (docs/backend-small-plan.md
item 3, tests 2 and 3).

  - versions_by_status is a database read on the app_tenant role: row-level security alone keeps tenants apart.
    The query ALSO filters tenant_id explicitly. The prove-it run removes that filter: the RLS tests must still
    pass, and only `..._the_explicit_filter_is_a_second_layer_on_top_of_rls` may fail (it notices the removal).
  - uploads_last_hour is read from Redis, which has no row-level security. The keys are built from the
    AUTHENTICATED user only; the caller sees their own count and the tenant window, nobody else's.

The _test database only.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import SessionLocal, tenant_session_context
from app.db.models.document import DOCUMENT_VERSION_STATUSES
from app.services import document_upload
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth, make_document, upload

pytestmark = pytest.mark.asyncio

URL = "/api/v1/admin/documents/usage"
ZERO = dict.fromkeys(DOCUMENT_VERSION_STATUSES, 0)


async def _usage(client: AsyncClient, w: WorldTenant) -> dict:  # type: ignore[type-arg]
    resp = await client.get(URL, headers=auth(w, w.admin))
    assert resp.status_code == 200, resp.text
    body: dict = resp.json()  # type: ignore[type-arg]
    return body


async def _fill(raw_db: AsyncSession, w: WorldTenant, plan: dict[str, int]) -> None:
    for status, n in plan.items():
        for _ in range(n):
            await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status=status)


# ── versions_by_status: row-level security ───────────────────────────────────


async def test_another_tenants_versions_never_change_this_tenants_counts(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    await _fill(raw_db, a, {"published": 1, "failed": 2})
    await _fill(raw_db, b, {"queued": 3, "processing": 1, "ready": 2, "published": 4, "archived": 5, "failed": 7})

    assert (await _usage(client, a))["versions_by_status"] == {**ZERO, "published": 1, "failed": 2}
    assert (await _usage(client, b))["versions_by_status"] == {
        "queued": 3, "processing": 1, "ready": 2, "published": 4, "archived": 5, "failed": 7
    }


async def test_a_session_without_tenant_context_counts_nothing(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """RLS alone: the app_tenant role with NO app.tenant_id sees no row, even when asked for a real tenant."""
    a = academic_world.a
    await _fill(raw_db, a, {"published": 2, "failed": 1})
    async with SessionLocal() as session, session.begin():
        assert await document_upload.version_status_counts(session, a.id) == ZERO


async def test_the_explicit_filter_is_a_second_layer_on_top_of_rls(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Defense in depth, NOT the RLS proof: tenant A's session (which has rows of its own) asked for tenant B's
    id gets zeros because of the explicit `tenant_id = :t` filter. Without that filter the same call would
    return A's own counts. The prove-it run removes the filter on purpose and THIS is the test that must then
    fail, while the two RLS tests above keep passing."""
    a, b = academic_world.a, academic_world.b
    await _fill(raw_db, a, {"published": 1, "ready": 2})
    await _fill(raw_db, b, {"failed": 3})
    async with tenant_session_context(str(a.id)) as session:
        assert await document_upload.version_status_counts(session, b.id) == ZERO
        assert await document_upload.version_status_counts(session, a.id) == {**ZERO, "published": 1, "ready": 2}


# ── uploads_last_hour: Redis keys are built from the authenticated user ──────


async def test_the_caller_sees_their_own_count_and_the_tenant_window_and_nobody_elses(
    client: AsyncClient, academic_world: AcademicWorld, document_store_root: Path
) -> None:
    a, b = academic_world.a, academic_world.b
    assert (await upload(client, a, a.t1, title="t1 one", data=b"t1 one")).status_code == 202
    assert (await upload(client, a, a.t1, title="t1 two", data=b"t1 two")).status_code == 202
    assert (
        await upload(client, a, a.t2, subject="Physics", classes=("10-B",), title="t2 one", data=b"t2 one")
    ).status_code == 202
    # Another tenant uploads too: it must never reach tenant A's numbers
    assert (await upload(client, b, b.t1, title="b one", data=b"b one")).status_code == 202
    assert (await upload(client, b, b.admin, title="b two", data=b"b two")).status_code == 202

    # Tenant A's admin: the tenant window holds the three uploads of A's teachers; the admin's own count is 0
    assert (await _usage(client, a))["uploads_last_hour"] == {"tenant": 3, "caller": 0, "window_seconds": 3600}

    # After one upload by this admin, the caller count is 1 and the tenant window 4
    assert (await upload(client, a, a.admin, title="admin one", data=b"admin one")).status_code == 202
    assert (await _usage(client, a))["uploads_last_hour"] == {"tenant": 4, "caller": 1, "window_seconds": 3600}

    # Tenant B's admin: 2 uploads in the tenant window, 1 of them its own; nothing of A
    assert (await _usage(client, b))["uploads_last_hour"] == {"tenant": 2, "caller": 1, "window_seconds": 3600}


async def test_no_request_input_can_select_another_users_or_tenants_window(
    client: AsyncClient, academic_world: AcademicWorld, document_store_root: Path
) -> None:
    """The keys come from the token. A query string or a header naming someone else changes nothing."""
    a, b = academic_world.a, academic_world.b
    assert (await upload(client, b, b.admin, title="b one", data=b"b one")).status_code == 202
    assert (await upload(client, a, a.t1, title="a one", data=b"a one")).status_code == 202
    extra = f"?user_id={a.t1.id}&tenant_id={b.id}&tenant={b.tenant.slug}&caller={a.t1.id}"
    resp = await client.get(URL + extra, headers=auth(a, a.admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["uploads_last_hour"] == {"tenant": 1, "caller": 0, "window_seconds": 3600}
    assert str(b.id) not in resp.text and str(a.t1.id) not in resp.text
