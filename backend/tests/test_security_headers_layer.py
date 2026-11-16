"""
Security headers on EVERY response (finding 10 of the review of 5b-1/5b-2).

Before the fix the headers layer sat inside the body-limit middleware and inside Starlette's own
error handling, so the body guard's 401/411/413 and an unhandled 500 carried no CSP and no nosniff, and
any route could set a weaker CSP. These tests go through the REAL app (`app.main.app`).
"""

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import documents as documents_api
from app.core import middleware
from app.core.config import settings
from app.core.middleware import SecurityHeadersMiddleware
from app.main import app
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth, make_document, upload
from tests.security.header_assertions import DEFAULT_CSP, assert_security_headers

MB = 1024 * 1024
UPLOAD_URL = "/api/v1/documents"
ORIGIN = settings.cors_origins[0]


def _cors(resp: Any, label: str) -> None:
    """The browser can still read the error: CORS stays the outermost layer."""
    assert resp.headers.get("access-control-allow-origin") == ORIGIN, f"{label}: no CORS header on the error"


async def test_401_from_the_body_guard_carries_the_security_headers(client: AsyncClient) -> None:
    resp = await client.post(UPLOAD_URL, headers={"Origin": ORIGIN}, files={"file": ("a.txt", b"x", "text/plain")})
    assert resp.status_code == 401
    assert_security_headers(resp.headers, label="401 body guard")
    _cors(resp, "401")


async def test_413_from_the_body_guard_carries_the_security_headers(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    headers = {
        **auth(w, w.t1),
        "Origin": ORIGIN,
        "Content-Type": "multipart/form-data; boundary=zz",
        "Content-Length": str(60 * MB),  # over the 50 MB ceiling: refused before any body byte is read
    }
    resp = await client.post(UPLOAD_URL, headers=headers, content=b"x")
    assert resp.status_code == 413, resp.text
    assert_security_headers(resp.headers, label="413 body guard")
    _cors(resp, "413")


async def test_411_from_the_body_guard_carries_the_security_headers(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    headers = {**auth(w, w.t1), "Content-Type": "multipart/form-data; boundary=zz"}

    async def no_length() -> Any:
        yield b"x"

    resp = await client.post(UPLOAD_URL, headers=headers, content=no_length())  # chunked: no Content-Length
    assert resp.status_code == 411, resp.text
    assert_security_headers(resp.headers, label="411 body guard")


async def test_unhandled_500_carries_the_security_headers(
    academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_audit(**_: Any) -> None:
        raise RuntimeError("audit write failed SECRET-IN-EXCEPTION")

    monkeypatch.setattr(documents_api, "record_audit_event", broken_audit)
    w = academic_world.a
    # The layer answers with the generic 500 and then re-raises (as Starlette does) so the server logs it
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver") as c:
        resp = await upload(c, w, w.t1)
    assert resp.status_code == 500
    assert resp.json() == {"detail": {"code": "internal_error", "message": "Something went wrong. Please try again later."}}
    assert "SECRET-IN-EXCEPTION" not in resp.text
    assert_security_headers(resp.headers, label="unhandled 500")


async def test_download_404_carries_the_default_csp_not_the_sandbox_one(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    url = f"/api/v1/documents/{uuid.uuid4()}/versions/{uuid.uuid4()}/download"
    resp = await client.get(url, headers=auth(w, w.t1))
    assert resp.status_code == 404
    assert_security_headers(resp.headers, label="download 404")


async def test_a_real_download_keeps_its_sandbox_csp(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published",
                               store_root=document_store_root)
    url = f"/api/v1/documents/{made.document.id}/versions/{made.version.id}/download"
    resp = await client.get(url, headers=auth(w, w.t1))
    assert resp.status_code == 200
    assert_security_headers(resp.headers, label="download 200", sandboxed=True)


async def test_hsts_is_still_sent_outside_development(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.post(UPLOAD_URL, files={"file": ("a.txt", b"x", "text/plain")})
    assert resp.status_code == 401
    assert_security_headers(resp.headers, label="401 in production", hsts=True)


# ── The CSP rule, against a minimal inner app ────────────────────────────────


async def _call(inner: Callable[..., Awaitable[None]], path: str = "/x") -> dict[str, str]:
    wrapped = SecurityHeadersMiddleware(inner)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}
    await wrapped(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def _responder(status: int, csp: str | None) -> Callable[..., Awaitable[None]]:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        headers = [(b"content-security-policy", csp.encode())] if csp is not None else []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    return inner


@pytest.mark.parametrize("status", [200, 204, 304, 401, 404, 411, 413, 500])
async def test_every_status_gets_the_headers(status: int) -> None:
    headers = await _call(_responder(status, None))
    assert_security_headers(headers, label=f"status {status}")


async def test_a_route_cannot_weaken_the_csp() -> None:
    for weak in ("default-src *", "default-src 'self'; script-src 'unsafe-inline'", "sandboxed-not; default-src *"):
        headers = await _call(_responder(200, weak))
        assert headers["content-security-policy"] == DEFAULT_CSP, weak


async def test_a_route_csp_with_the_sandbox_directive_is_kept() -> None:
    own = "sandbox; default-src 'none'; frame-ancestors 'none'"
    assert (await _call(_responder(200, own)))["content-security-policy"] == own


async def test_an_explicit_allow_list_entry_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = "default-src 'none'; img-src 'self'; frame-ancestors 'none'"
    assert (await _call(_responder(200, custom)))["content-security-policy"] == DEFAULT_CSP
    monkeypatch.setattr(middleware, "ALLOWED_ROUTE_CSPS", frozenset({custom}))
    assert (await _call(_responder(200, custom)))["content-security-policy"] == custom
