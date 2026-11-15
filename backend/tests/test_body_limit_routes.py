"""
Step 5a-3: per-route body limits (registry in app/core/body_limit.py).

The dummy routes exist ONLY in the app built here; production code has no test backdoor.
"""

from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core import body_limit
from app.core.body_limit import (
    BODY_LIMIT_PREFIXES,
    DEFAULT_LIMIT,
    DOCUMENTS_LIMIT,
    MAX_REQUEST_BODY_BYTES,
    BodyLimitMiddleware,
    limit_for_path,
)
from app.core.upload_security import DOC_UPLOAD_CEILING_BYTES
from tests.security.upload_assertions import (
    MB,
    MULTIPART,
    assert_default_limit_enforced,
    assert_documents_limits_enforced,
)

DOCS_URL = "/api/v1/documents/_probe"
OTHER_URL = "/api/v1/elsewhere/_probe"


def build_app() -> FastAPI:
    app = FastAPI()

    async def probe(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_api_route(DOCS_URL, probe, methods=["POST"])
    app.add_api_route(OTHER_URL, probe, methods=["POST"])
    app.add_middleware(BodyLimitMiddleware)
    return app


@pytest.fixture
async def probe_client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url="http://probe") as c:
        yield c


def test_registry_values() -> None:
    assert DEFAULT_LIMIT.max_body_bytes == 2 * MB + 64 * 1024 == MAX_REQUEST_BODY_BYTES
    assert DEFAULT_LIMIT.max_parts == 5
    assert DOCUMENTS_LIMIT.max_body_bytes == DOC_UPLOAD_CEILING_BYTES + 64 * 1024
    assert DOCUMENTS_LIMIT.max_parts == 6
    assert BODY_LIMIT_PREFIXES["/api/v1/documents"] is DOCUMENTS_LIMIT


def test_prefix_matches_on_path_boundaries() -> None:
    assert limit_for_path("/api/v1/documents") is DOCUMENTS_LIMIT
    assert limit_for_path("/api/v1/documents/") is DOCUMENTS_LIMIT
    assert limit_for_path("/api/v1/documents/abc/versions") is DOCUMENTS_LIMIT
    for other in (
        "/api/v1/documentsX",
        "/api/v1/admin/documents/usage",
        "/api/v1/classes/1/timetables/import-preview",
        "/api/v1/Documents",
        "/documents",
        "",
    ):
        assert limit_for_path(other) is None, other


async def test_documents_prefix_limits(probe_client: AsyncClient) -> None:
    await assert_documents_limits_enforced(probe_client, DOCS_URL)


async def test_default_prefix_limits(probe_client: AsyncClient) -> None:
    await assert_default_limit_enforced(probe_client, OTHER_URL)


async def test_default_prefix_rejects_just_over_2mb_plus_overhead(probe_client: AsyncClient) -> None:
    res = await probe_client.post(OTHER_URL, headers=MULTIPART, content=b"x" * (2 * MB + 64 * 1024 + 1))
    assert res.status_code == 413


async def test_documents_limit_also_covers_non_multipart_requests(probe_client: AsyncClient) -> None:
    """Under a registered prefix every request type is limited; elsewhere JSON stays unlimited as before."""
    json_headers = {"Content-Type": "application/json"}
    too_big = DOC_UPLOAD_CEILING_BYTES + 64 * 1024 + 1
    res = await probe_client.post(DOCS_URL, headers={**json_headers, "Content-Length": str(too_big)}, content=b"{}")
    assert res.status_code == 413
    ok = await probe_client.post(OTHER_URL, headers=json_headers, content=b"x" * (3 * MB))
    assert ok.status_code == 200


async def test_413_body_shape_and_no_internals(probe_client: AsyncClient) -> None:
    res = await probe_client.post(
        OTHER_URL, headers={**MULTIPART, "Content-Length": str(50 * MB)}, content=b"x"
    )
    assert res.status_code == 413
    assert res.json() == {
        "detail": {"code": "file_too_large", "message": "The request body exceeds the maximum allowed size."}
    }


async def test_registry_is_read_per_request(monkeypatch: pytest.MonkeyPatch, probe_client: AsyncClient) -> None:
    """The registry is consulted on every request, so a prefix change takes effect without a rebuild."""
    monkeypatch.setattr(body_limit, "BODY_LIMIT_PREFIXES", {})
    res = await probe_client.post(DOCS_URL, headers=MULTIPART, content=b"x" * (3 * MB))
    assert res.status_code == 413
