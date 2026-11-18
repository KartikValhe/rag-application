"""
tests/test_document_route_matrix.py
───────────────────────────────────
Route x role matrix for every document route, GENERATED from the real route table (design A4):
a new route under /api/v1/documents or /api/v1/admin/documents that is missing from EXPECTED fails
the guard test, and every route must declare its permission through require(...).

Roles: A = tenant admin, T = teacher in scope (T1 on a 10-A Math document), T' = teacher out of
scope (T2), S = student, anon = no token. Every (route, role) call gets its own fresh document,
so a successful delete or retry never changes what the next role sees.

Also: the two upload routes are picked up by the OpenAPI-derived multipart list of the body-limit
registry, and an anonymous upload to either is refused before one body byte is read.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import get_route_declared_permission
from app.core.body_limit import DOCUMENTS_LIMIT, match_upload_route
from app.db.models.document import Document
from app.main import app
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth, make_document
from tests.security.upload_assertions import assert_anonymous_upload_not_consumed
from tests.test_document_store import all_app_routes

# No module-level asyncio mark: asyncio_mode=auto already runs the async tests, and the sync tests here must not carry it.

ROLES = ("admin", "teacher", "teacher_out", "student", "anonymous")

# (method, path) -> (permission, expected status for A, T, T', S, anon)
EXPECTED: dict[tuple[str, str], tuple[str, tuple[int, int, int, int, int]]] = {
    ("POST", "/api/v1/documents"): ("document:upload", (202, 202, 404, 403, 401)),
    ("GET", "/api/v1/documents"): ("document:view", (200, 200, 200, 403, 401)),
    ("GET", "/api/v1/documents/{document_id}"): ("document:view", (200, 200, 404, 403, 401)),
    ("PATCH", "/api/v1/documents/{document_id}"): ("document:upload", (200, 200, 404, 403, 401)),
    ("DELETE", "/api/v1/documents/{document_id}"): ("document:delete", (204, 204, 404, 403, 401)),
    ("POST", "/api/v1/documents/{document_id}/versions"): ("document:upload", (202, 202, 404, 403, 401)),
    ("GET", "/api/v1/documents/{document_id}/versions/{version_id}/status"): (
        "document:view", (200, 200, 404, 403, 401)),
    ("POST", "/api/v1/documents/{document_id}/versions/{version_id}/retry"): (
        "document:upload", (202, 202, 404, 403, 401)),
    ("POST", "/api/v1/documents/{document_id}/versions/{version_id}/publish"): (
        "document:publish", (200, 200, 404, 403, 401)),
    ("DELETE", "/api/v1/documents/{document_id}/versions/{version_id}"): (
        "document:upload", (204, 204, 404, 403, 401)),
    ("GET", "/api/v1/documents/{document_id}/versions/{version_id}/download"): (
        "document:view", (200, 200, 404, 403, 401)),
    ("GET", "/api/v1/admin/documents/usage"): ("settings:manage", (200, 403, 403, 403, 401)),
    ("POST", "/api/v1/admin/documents/{document_id}/purge"): ("settings:manage", (204, 403, 403, 403, 401)),
}


def _document_routes() -> dict[tuple[str, str], Any]:
    found: dict[tuple[str, str], Any] = {}
    for route in all_app_routes(app):
        path = getattr(route, "path", "")
        if not re.match(r"^/api/v1/(admin/)?documents(/|$)", path):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            found[(method, path)] = route
    return found


def test_every_document_route_is_in_the_matrix_and_declares_its_permission() -> None:
    routes = _document_routes()
    assert set(routes) == set(EXPECTED), (
        f"routes without a matrix row: {sorted(set(routes) - set(EXPECTED))}; "
        f"matrix rows without a route: {sorted(set(EXPECTED) - set(routes))}"
    )
    for key, route in routes.items():
        assert get_route_declared_permission(route) == EXPECTED[key][0], key


Prepared = tuple[str, dict[str, Any]]


async def _prepare(
    key: tuple[str, str], w: WorldTenant, raw_db: AsyncSession, store_root: Path
) -> Prepared:
    """A fresh state for one call: the concrete path and the request body."""
    method, path = key
    if key == ("POST", "/api/v1/documents"):
        files = {"file": ("m.txt", uuid.uuid4().hex.encode(), "text/plain")}
        data = {"title": "Matrix", "subject_id": str(w.subjects["Math"].id), "class_ids": [str(w.classes["10-A"].id)]}
        return path, {"files": files, "data": data}
    if "{document_id}" not in path:
        return path, {}
    status, error = "failed", "processing_failed"
    if path.endswith("/download"):
        status, error = "published", None
    if path.endswith("/publish"):
        status, error = "ready", None
    made = await make_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status=status, error_code=error,
        store_root=store_root,
    )
    if path.endswith("/purge"):
        await raw_db.execute(update(Document).where(Document.id == made.document.id).values(deleted_at=datetime.now(UTC)))
        await raw_db.commit()
    concrete = path.replace("{document_id}", str(made.document.id)).replace("{version_id}", str(made.version.id))
    kwargs: dict[str, Any] = {}
    if method == "PATCH":
        kwargs["json"] = {"title": "Renamed"}
    if path.endswith("/publish"):
        kwargs["json"] = {}
    if path.endswith("/versions") and method == "POST":
        kwargs["files"] = {"file": ("v.txt", uuid.uuid4().hex.encode(), "text/plain")}
    return concrete, kwargs


@pytest.mark.parametrize("key", list(EXPECTED), ids=[f"{m} {p}" for m, p in EXPECTED])
async def test_route_role_matrix(
    key: tuple[str, str], client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession,
    document_store_root: Path,
) -> None:
    w = academic_world.a
    users = {"admin": w.admin, "teacher": w.t1, "teacher_out": w.t2, "student": w.s1}
    method = key[0]
    got: dict[str, int] = {}
    for role, expected in zip(ROLES, EXPECTED[key][1], strict=True):
        path, kwargs = await _prepare(key, w, raw_db, document_store_root)
        headers = auth(w, users[role]) if role in users else {"X-Tenant-Slug": w.tenant.slug}
        resp = await client.request(method, path, headers=headers, **kwargs)
        got[role] = resp.status_code
        assert resp.status_code == expected, f"{method} {key[1]} as {role}: {resp.status_code} {resp.text}"
    assert tuple(got[r] for r in ROLES) == EXPECTED[key][1]


# ── Pre-auth checks of the two upload routes ─────────────────────────────────


FORM_MEDIA_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


def test_document_upload_routes_are_in_the_openapi_derived_multipart_list() -> None:
    form_routes: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        for method, op in operations.items():
            content = op.get("requestBody", {}).get("content", {})
            if any(media in content for media in FORM_MEDIA_TYPES):
                form_routes.add((method.upper(), path))
    upload_routes = {("POST", "/api/v1/documents"), ("POST", "/api/v1/documents/{document_id}/versions")}
    assert upload_routes <= form_routes, sorted(form_routes)
    # No other document route parses a form.
    assert {r for r in form_routes if "/documents" in r[1]} == upload_routes
    for method, path in upload_routes:
        rule = match_upload_route(method, re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path))
        assert rule is not None and rule.kind == "multipart" and rule.limit == DOCUMENTS_LIMIT, path


@pytest.mark.parametrize("path", ["/api/v1/documents", f"/api/v1/documents/{uuid.uuid4()}/versions"])
async def test_anonymous_document_upload_is_refused_before_the_body_is_read(
    academic_world: AcademicWorld, path: str
) -> None:
    w = academic_world.a
    await assert_anonymous_upload_not_consumed(app, path, [("Host", "localhost"), ("X-Tenant-Slug", w.tenant.slug)])


_UNUSED: tuple[Callable[..., Awaitable[Any]], ...] = ()
