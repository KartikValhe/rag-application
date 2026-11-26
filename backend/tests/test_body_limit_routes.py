"""
Per-route body limits (app/core/body_limit.py), finding 4 of the 5a-3 security review.

The registry is keyed by (method, full-path regex): only POST /api/v1/documents and
POST /api/v1/documents/{uuid}/versions get the document ceiling (50 MB + 64 KB, 6 parts); every
other request under /api/v1/documents is limited to 64 KB; other multipart requests keep
2 MB + 64 KB and 5 parts.

The probe routes exist ONLY in the app built here; production code has no test backdoor.
"""

import re
import uuid
from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core import body_limit
from app.core.body_limit import MAX_REQUEST_BODY_BYTES, BodyLimitMiddleware
from app.core.security import create_access_token
from app.core.upload_security import DOC_UPLOAD_CEILING_BYTES
from tests.security.asgi_stream import call_asgi, multipart_body, split
from tests.security.upload_assertions import (
    MB,
    MULTIPART,
    SMALL_LIMIT_BYTES,
    assert_default_limit_enforced,
    assert_documents_limits_enforced,
    assert_documents_registry_enforced,
)

DOCS_URL = "/api/v1/documents"
OTHER_URL = "/api/v1/elsewhere/_probe"
FORM_PROBE_URL = "/api/v1/_probe/form"
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def auth_headers() -> dict[str, str]:
    """A token with a valid signature and expiry. The middleware never looks the user up."""
    token = create_access_token(tenant_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()))
    return {"Authorization": f"Bearer {token}"}


def build_app() -> FastAPI:
    app = FastAPI()

    async def probe(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    async def form_probe(request: Request) -> dict[str, int]:
        form = await request.form()  # the real Starlette form parser
        return {"fields": len(form)}

    app.add_api_route(DOCS_URL, probe, methods=ALL_METHODS)
    app.add_api_route(DOCS_URL + "/{rest:path}", probe, methods=ALL_METHODS)
    app.add_api_route(OTHER_URL, probe, methods=["POST"])
    app.add_api_route(FORM_PROBE_URL, form_probe, methods=["POST", "PUT"])
    app.add_middleware(BodyLimitMiddleware)
    return app


@pytest.fixture
async def probe_client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url="http://probe") as c:
        yield c


def test_registry_values() -> None:
    from app.core.body_limit import DEFAULT_LIMIT, DOCUMENTS_LIMIT, SMALL_LIMIT, UPLOAD_ROUTES

    assert DEFAULT_LIMIT.max_body_bytes == 2 * MB + 64 * 1024 == MAX_REQUEST_BODY_BYTES
    assert DEFAULT_LIMIT.max_parts == 5
    assert DOCUMENTS_LIMIT.max_body_bytes == DOC_UPLOAD_CEILING_BYTES + 64 * 1024
    assert DOCUMENTS_LIMIT.max_parts == 6
    assert SMALL_LIMIT.max_body_bytes == SMALL_LIMIT_BYTES == 64 * 1024
    doc_rules = [r for r in UPLOAD_ROUTES if r.limit is DOCUMENTS_LIMIT]
    assert sorted((r.method, r.pattern.pattern) for r in doc_rules) == sorted(
        [
            ("POST", r"^/api/v1/documents$"),
            ("POST", r"^/api/v1/documents/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/versions$"),
        ]
    )
    assert all(r.kind == "multipart" for r in doc_rules)


def test_chat_requests_are_limited_to_32kb_without_the_upload_checks() -> None:
    """6a-9b (plan 8.5): every request under /api/v1/chat is a small JSON body: an area limit of 32 KB, never an upload
    route (no Bearer pre-check, no per-IP in-flight cap of 4, which a school behind one address would hit)."""
    from app.core.body_limit import CHAT_LIMIT, limit_for, match_upload_route

    assert CHAT_LIMIT.max_body_bytes == 32 * 1024
    conv = uuid.uuid4()
    for method, path in [("POST", f"/api/v1/chat/conversations/{conv}/messages"), ("POST", "/api/v1/chat/conversations"),
                         ("DELETE", f"/api/v1/chat/conversations/{conv}"), ("GET", "/api/v1/chat")]:
        assert limit_for(method, path) is CHAT_LIMIT, (method, path)
        assert match_upload_route(method, path) is None, (method, path)
    assert limit_for("POST", "/api/v1/chatter") is None


def test_feedback_and_the_admin_chat_settings_are_limited_to_32kb_too() -> None:
    """6a-12a (plan 8.5): the feedback routes sit under /api/v1/chat; the admin chat settings route is a small JSON
    body as well and gets its own area (/api/v1/admin/chat), never an upload route."""
    from app.core.body_limit import CHAT_LIMIT, limit_for, match_upload_route

    message = uuid.uuid4()
    for method, path in [("PUT", f"/api/v1/chat/messages/{message}/feedback"),
                         ("DELETE", f"/api/v1/chat/messages/{message}/feedback"),
                         ("PATCH", "/api/v1/admin/chat/settings"), ("GET", "/api/v1/admin/chat/settings")]:
        assert limit_for(method, path) is CHAT_LIMIT, (method, path)
        assert match_upload_route(method, path) is None, (method, path)
    assert limit_for("PATCH", "/api/v1/admin/chatter") is None
    assert limit_for("POST", "/api/v1/admin/branding/logo") is not CHAT_LIMIT


def test_registry_matches_method_and_whole_path() -> None:
    from app.core.body_limit import DOCUMENTS_LIMIT, SMALL_LIMIT, limit_for, match_upload_route

    doc = uuid.uuid4()
    assert limit_for("POST", "/api/v1/documents") is DOCUMENTS_LIMIT
    assert limit_for("POST", f"/api/v1/documents/{doc}/versions") is DOCUMENTS_LIMIT
    assert limit_for("POST", f"/api/v1/documents/{str(doc).upper()}/versions") is DOCUMENTS_LIMIT
    for method, path in [
        ("PUT", "/api/v1/documents"),
        ("GET", "/api/v1/documents"),
        ("POST", "/api/v1/documents/"),
        ("POST", f"/api/v1/documents/{doc}"),
        ("PATCH", f"/api/v1/documents/{doc}/versions"),
        ("POST", f"/api/v1/documents/{doc}/versions/"),
        ("POST", f"/api/v1/documents/{doc}/versions/x"),
        ("POST", "/api/v1/documents/abc/versions"),
        ("POST", f"/api/v1/documents/{doc}/retry"),
    ]:
        assert match_upload_route(method, path) is None, (method, path)
        assert limit_for(method, path) is SMALL_LIMIT, (method, path)
    for method, path in [
        ("POST", "/api/v1/documentsX"),
        ("POST", "/api/v1/admin/documents/usage"),
        ("POST", "/api/v1/Documents"),
        ("POST", "/documents"),
        ("POST", ""),
    ]:
        assert limit_for(method, path) is None, (method, path)


async def test_document_upload_routes_get_the_ceiling(probe_client: AsyncClient) -> None:
    await assert_documents_limits_enforced(probe_client, DOCS_URL, auth_headers())
    await assert_documents_limits_enforced(probe_client, f"{DOCS_URL}/{uuid.uuid4()}/versions", auth_headers())


async def test_every_other_documents_request_is_limited_to_64kb(probe_client: AsyncClient) -> None:
    await assert_documents_registry_enforced(probe_client, auth_headers())


async def test_default_limits_outside_the_registry(probe_client: AsyncClient) -> None:
    await assert_default_limit_enforced(probe_client, OTHER_URL)


async def test_default_rejects_just_over_2mb_plus_overhead(probe_client: AsyncClient) -> None:
    res = await probe_client.post(OTHER_URL, headers=MULTIPART, content=b"x" * (2 * MB + 64 * 1024 + 1))
    assert res.status_code == 413


async def test_json_to_a_documents_route_is_not_a_gap(probe_client: AsyncClient) -> None:
    """
    Replaces the old "3 MB of JSON is accepted elsewhere" check: under /api/v1/documents JSON is
    capped at 64 KB, and JSON to an upload route is refused (it must be multipart).
    """
    json_h = {"Content-Type": "application/json", **auth_headers()}
    res = await probe_client.patch(f"{DOCS_URL}/{uuid.uuid4()}", headers=json_h, content=b"x" * (3 * MB))
    assert res.status_code == 413
    declared = await probe_client.post(
        f"{DOCS_URL}/search", headers={**json_h, "Content-Length": str(DOC_UPLOAD_CEILING_BYTES)}, content=b"{}"
    )
    assert declared.status_code == 413
    upload_as_json = await probe_client.post(DOCS_URL, headers=json_h, content=b"{}")
    assert upload_as_json.status_code == 415


async def test_413_body_shape_and_no_internals(probe_client: AsyncClient) -> None:
    res = await probe_client.post(
        OTHER_URL, headers={**MULTIPART, "Content-Length": str(50 * MB)}, content=b"x"
    )
    assert res.status_code == 413
    assert res.json() == {
        "detail": {"code": "file_too_large", "message": "The request body exceeds the maximum allowed size."}
    }


async def test_registry_is_read_per_request(monkeypatch: pytest.MonkeyPatch, probe_client: AsyncClient) -> None:
    """The registry is consulted on every request, so a change takes effect without a rebuild."""
    monkeypatch.setattr(body_limit, "UPLOAD_ROUTES", ())
    res = await probe_client.post(DOCS_URL, headers=MULTIPART, content=b"x" * (3 * MB))
    assert res.status_code == 413  # now only the 64 KB documents area limit applies


async def test_registered_probe_route_with_a_real_form_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe rule registered only in this test: auth, limit and part cap apply to POST only."""
    from app.core.body_limit import BodyLimit, UploadRoute

    rule = UploadRoute("POST", re.compile(r"^/api/v1/_probe/form$"), BodyLimit(256 * 1024, 3), "multipart")
    monkeypatch.setattr(body_limit, "UPLOAD_ROUTES", (*body_limit.UPLOAD_ROUTES, rule))
    test_app = build_app()
    auth = [("Authorization", auth_headers()["Authorization"])]

    ok_body = multipart_body("fb", "a.txt", b"hello")
    framing = [("Content-Type", "multipart/form-data; boundary=fb"), ("Content-Length", str(len(ok_body)))]
    ok = await call_asgi(test_app, "POST", FORM_PROBE_URL, auth + framing, split(ok_body, 16))
    assert ok.status == 200 and ok.json() == {"fields": 1}, ok.body

    anon = await call_asgi(test_app, "POST", FORM_PROBE_URL, framing, split(ok_body, 16))
    assert anon.status == 401 and anon.chunks_consumed == 0

    big = multipart_body("fb", "a.txt", b"x" * (300 * 1024))
    big_framing = [framing[0], ("Content-Length", str(len(big)))]
    over = await call_asgi(test_app, "POST", FORM_PROBE_URL, auth + big_framing, split(big))
    assert over.status == 413 and over.chunks_consumed == 0

    four = b"".join(f'--fb\r\nContent-Disposition: form-data; name="f{i}"\r\n\r\nv\r\n'.encode() for i in range(4))
    four += b"--fb--\r\n"
    parts = await call_asgi(test_app, "POST", FORM_PROBE_URL, auth + [framing[0], ("Content-Length", str(len(four)))], split(four, 16))
    assert parts.status == 413 and parts.json()["detail"]["code"] == "too_many_parts"

    # PUT is not the registered method: no pre-body auth, the default multipart limit applies
    put = await call_asgi(test_app, "PUT", FORM_PROBE_URL, framing, split(ok_body, 16))
    assert put.status == 200


async def test_real_app_existing_route_uses_its_registered_limit() -> None:
    """Through app.main.app: the timetable import (2 MB + 64 KB) and the documents area (64 KB)."""
    from app.main import app

    auth = [("Host", "localhost"), ("Authorization", auth_headers()["Authorization"])]
    path = f"/api/v1/classes/{uuid.uuid4()}/timetables/import-preview"
    ct = ("Content-Type", "multipart/form-data; boundary=zz")
    over = await call_asgi(app, "POST", path, auth + [ct, ("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))], [b"x"])
    assert over.status == 413 and over.chunks_consumed == 0
    json_ct = ("Content-Type", "application/json")
    docs = await call_asgi(app, "PATCH", f"/api/v1/documents/{uuid.uuid4()}", auth + [json_ct, ("Content-Length", str(3 * MB))], [b"x"])
    assert docs.status == 413 and docs.chunks_consumed == 0
