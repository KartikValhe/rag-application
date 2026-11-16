"""
Prove-it-fails for the document API protections of step 5b-1 (design C2).

The route tests use shared assertion helpers (tests/security/document_assertions.py). Here each
protection is switched off with monkeypatch, the SAME scenario runs again through the real app, and
the helper must RAISE. That shows the helper really checks the protection and does not pass by
accident. Every test first runs the scenario with the protection ON (the helper passes), then OFF.

  1. authorized_pairs returns every pair of the tenant -> a teacher reads another teacher's documents
     through ANY read route (list, detail, status, download), each checked on its own.
  2. the quota check is removed                       -> an upload over the documents quota passes.
  3. the store's tenant-prefix check is removed        -> another tenant's key is accepted.

TEST database only: the module fixture asserts current_database() ends in _test before anything runs.
"""

import os
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models.academic import ClassSubject
from app.services import document_scope, document_store, document_upload
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import MadeDocument, auth, make_document, upload
from tests.security.document_assertions import (
    DOCUMENT_READ_ROUTES,
    assert_key_refused_for_other_tenant,
    assert_list_hides_foreign_documents,
    assert_quota_refused,
    assert_read_route_refuses_foreign_document,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"], connect_args={"statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    finally:
        await engine.dispose()
    assert name.endswith("_test"), f"prove-it tests must run against the test database, not {name!r}"


# ── 1. Scope ─────────────────────────────────────────────────────────────────


ROUTES = ["list", *DOCUMENT_READ_ROUTES]


async def _check_route(client: AsyncClient, w: WorldTenant, route: str, made: MadeDocument, allowed: list[uuid.UUID]) -> None:
    """The caller is T2, the document is T1's (a pair T2 does not teach). Raises when the route leaks it."""
    if route == "list":
        await assert_list_hides_foreign_documents(client, auth(w, w.t2), allowed)
    else:
        await assert_read_route_refuses_foreign_document(client, auth(w, w.t2), route, made.document.id, made.version.id)


# Rewritten (review of 5b-1/5b-2): the first version covered the list route only, so a leak through the
# detail, status or download route would not have been noticed. Every read route is checked, and for
# each one the protection is switched off on its own and the helper must raise.
@pytest.mark.parametrize("route", ROUTES)
async def test_all_pairs_scope_is_caught_by_the_helper_of_every_read_route(
    route: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession,
    document_store_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    t1_math = await make_document(raw_db, w, subject="Math", classes=("10-A", "10-B"), uploader=w.t1,
                                  status="published", store_root=document_store_root)
    t2_chem = await make_document(raw_db, w, subject="Chemistry", classes=("11-A",), uploader=w.t2)
    allowed_for_t2 = [t2_chem.document.id]

    # Protection ON: T2 gets the generic 404 on T1's document through this route (and the list hides it).
    await _check_route(client, w, route, t1_math, allowed_for_t2)

    async def all_pairs(db: AsyncSession, user: object, *, purpose: str) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
        rows = await db.execute(select(ClassSubject.class_id, ClassSubject.subject_id))
        return frozenset((r[0], r[1]) for r in rows.all())

    monkeypatch.setattr(document_scope, "authorized_pairs", all_pairs)
    with pytest.raises(AssertionError, match="outside the caller's scope"):
        await _check_route(client, w, route, t1_math, allowed_for_t2)


# ── 2. Quota ─────────────────────────────────────────────────────────────────


async def test_removed_quota_check_is_caught_by_the_quota_helper(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    await raw_db.execute(
        text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": '{"max_documents": 1}', "id": w.id},
    )
    await raw_db.commit()
    await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1)

    # Protection ON: the second document is refused.
    assert_quota_refused(await upload(client, w, w.t1, data=b"over quota 1"), "quota_documents_exceeded")

    monkeypatch.setattr(document_upload, "check_quota", lambda *a, **k: None)
    resp = await upload(client, w, w.t1, data=b"over quota 2")
    assert resp.status_code == 202
    with pytest.raises(AssertionError):
        assert_quota_refused(resp, "quota_documents_exceeded")


# ── 3. Store tenant prefix ───────────────────────────────────────────────────


async def _try_foreign_key(store: document_store.LocalPrivateStore, tenant: uuid.UUID, key: str) -> BaseException | None:
    try:
        await store.stream(tenant, key)
    except BaseException as exc:  # noqa: BLE001 - the helper decides whether this is the right refusal
        return exc
    return None


async def test_removed_tenant_prefix_check_is_caught_by_the_store_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    store = document_store.LocalPrivateStore(tmp_path / "store")
    key_b = document_store.new_document_key(tenant_b)
    await store.put(tenant_b, key_b, b"tenant B's syllabus")

    # Protection ON: tenant A can never name tenant B's key.
    assert_key_refused_for_other_tenant(await _try_foreign_key(store, tenant_a, key_b))

    monkeypatch.setattr(document_store, "_check_tenant_prefix", lambda tenant_id, key: None)
    exc = await _try_foreign_key(store, tenant_a, key_b)
    assert exc is None  # the file of tenant B would now be served to tenant A
    with pytest.raises(AssertionError, match="foreign tenant key was not refused"):
        assert_key_refused_for_other_tenant(exc)
