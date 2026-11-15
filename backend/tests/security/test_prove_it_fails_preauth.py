"""
Prove-it-fails for the fixes of the 5a-3 security review (findings 1-4).

Each protection is switched off with monkeypatch, one at a time, and the helper that the real
tests use must RAISE. That shows the helpers really check the protection.

TEST database only: the autouse fixture asserts that the app's database is a `*_test` database
before anything runs (the real-app requests below can reach the routes when a check is off).
"""

import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import body_limit, upload_security
from app.core.body_limit import BodyLimitMiddleware, UploadRoute
from app.core.security import create_access_token
from app.core.upload_security import UploadProfile
from app.db.engine import SessionLocal
from app.db.models.academic import Class
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import app
from tests.security.asgi_stream import Header
from tests.security.upload_assertions import (
    assert_anonymous_upload_not_consumed,
    assert_documents_registry_enforced,
    assert_duplicate_headers_rejected,
    assert_pdf_scan_fast,
)


@pytest.fixture(autouse=True)
async def _only_the_test_database() -> None:
    async with SessionLocal() as session:
        name = (await session.execute(text("SELECT current_database()"))).scalar_one()
    assert str(name).endswith("_test"), f"refusing to run prove-it-fails against {name!r}"


@pytest.fixture
async def import_target(tenant_a: Tenant, admin_a: User, raw_db: AsyncSession) -> AsyncIterator[tuple[str, list[Header]]]:
    c = Class(tenant_id=tenant_a.id, name="Grade 9")
    raw_db.add(c)
    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    headers = [("Host", "localhost"), ("X-Tenant-Slug", tenant_a.slug), ("Authorization", f"Bearer {token}")]
    yield f"/api/v1/classes/{c.id}/timetables/import-preview", headers


# ─── 1. Bounded PDF regexes ──────────────────────────────────────────────────

UNBOUNDED_OBJ_RE = re.compile(rb"\d+\s+\d+\s+obj\b")  # the pattern before the fix


def test_baseline_pdf_scan_helper_passes() -> None:
    assert_pdf_scan_fast("digits", UploadProfile.TIMETABLE)


def test_pdf_scan_helper_fails_with_the_unbounded_obj_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_security, "PDF_OBJ_RE", UNBOUNDED_OBJ_RE)
    with pytest.raises(AssertionError, match="still running|took"):
        assert_pdf_scan_fast("digits", UploadProfile.DOCUMENT, kill_after=10)


# ─── 2. Duplicate-header rejection ───────────────────────────────────────────

async def test_baseline_duplicate_header_helper_passes(import_target: tuple[str, list[Header]]) -> None:
    path, headers = import_target
    await assert_duplicate_headers_rejected(app, "POST", path, headers)


async def test_duplicate_header_helper_fails_without_the_check(
    monkeypatch: pytest.MonkeyPatch, import_target: tuple[str, list[Header]]
) -> None:
    monkeypatch.setattr(body_limit, "find_duplicate_header", lambda raw_headers: None)
    path, headers = import_target
    with pytest.raises(AssertionError, match="was not refused"):
        await assert_duplicate_headers_rejected(app, "POST", path, headers)


# ─── 3. Pre-body Bearer check ────────────────────────────────────────────────

async def test_baseline_anonymous_upload_helper_passes(import_target: tuple[str, list[Header]]) -> None:
    path, headers = import_target
    await assert_anonymous_upload_not_consumed(app, path, headers[:2])


async def test_anonymous_upload_helper_fails_without_the_bearer_check(
    monkeypatch: pytest.MonkeyPatch, import_target: tuple[str, list[Header]]
) -> None:
    monkeypatch.setattr(body_limit, "bearer_problem", lambda raw_headers: None)
    path, headers = import_target
    with pytest.raises(AssertionError, match="body chunks were read|spooled"):
        await assert_anonymous_upload_not_consumed(app, path, headers[:2])


# ─── 4. (method, path regex) registry ────────────────────────────────────────

@pytest.fixture
async def docs_probe() -> AsyncGenerator[AsyncClient, None]:
    probe_app = FastAPI()

    async def probe(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    probe_app.add_api_route("/api/v1/documents", probe, methods=methods)
    probe_app.add_api_route("/api/v1/documents/{rest:path}", probe, methods=methods)
    probe_app.add_middleware(BodyLimitMiddleware)
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://probe") as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(tenant_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()))}"}


async def test_baseline_registry_helper_passes(docs_probe: AsyncClient) -> None:
    await assert_documents_registry_enforced(docs_probe, _auth())


async def test_registry_helper_fails_without_the_documents_area_limit(
    monkeypatch: pytest.MonkeyPatch, docs_probe: AsyncClient
) -> None:
    monkeypatch.setattr(body_limit, "AREA_LIMITS", ())
    with pytest.raises(AssertionError, match="was accepted"):
        await assert_documents_registry_enforced(docs_probe, _auth())


async def test_registry_helper_fails_with_a_prefix_registry(
    monkeypatch: pytest.MonkeyPatch, docs_probe: AsyncClient
) -> None:
    """The old behaviour: one prefix for every method and every path below it."""

    def prefix_match(method: str, path: str, routes: tuple[UploadRoute, ...] | None = None) -> UploadRoute | None:
        if path == "/api/v1/documents" or path.startswith("/api/v1/documents/"):
            return UploadRoute(method, re.compile(".*"), body_limit.DOCUMENTS_LIMIT, "multipart")
        return None

    monkeypatch.setattr(body_limit, "match_upload_route", prefix_match)
    with pytest.raises(AssertionError):
        await assert_documents_registry_enforced(docs_probe, _auth())
