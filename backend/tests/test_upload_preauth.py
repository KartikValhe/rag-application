"""
Findings 2, 3 and 5 of the 5a-3 security review, tested through the REAL app (app.main.app).

2. Header confusion: a repeated Content-Type or Content-Length is refused with 400 on every route,
   a Content-Length must be a plain non-negative integer, and the multipart boundary is read with
   the same parser Starlette uses.
3. Anonymous spooling: on every upload route the middleware checks the Bearer token (signature,
   type and expiry; no DB), the content type, Content-Length (411/413) and a per-IP in-flight cap
   (429) BEFORE one body byte is read. Route-level authorization is unchanged.
5. The middleware sits in front of form parsing in main.py; real form routes are covered.

The ASGI-level client (tests/security/asgi_stream.py) records how many body chunks the app pulled.
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.cors import CORSMiddleware

from app.core import redis_client
from app.core.body_limit import BodyLimitMiddleware
from app.core.config import settings
from app.core.security import create_access_token
from app.db.models.academic import Class
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import app
from tests.security.asgi_stream import Header, call_asgi, multipart_body, split
from tests.security.upload_assertions import (
    MB,
    assert_anonymous_upload_not_consumed,
    assert_duplicate_headers_rejected,
    record_spools,
)

CSV = b"weekday,start_time,end_time,subject_code\nMonday,09:00,10:00,CS101\n"


@dataclass
class Ctx:
    tenant: list[Header]  # Host + tenant slug
    auth: list[Header]  # tenant + valid Bearer token
    class_id: uuid.UUID

    @property
    def import_url(self) -> str:
        return f"/api/v1/classes/{self.class_id}/timetables/import-preview"

    def upload_routes(self) -> list[str]:
        return [
            "/api/v1/admin/branding/logo",
            "/api/v1/admin/branding/favicon",
            "/api/v1/admin/users/import",
            f"/api/v1/classes/{self.class_id}/students/import",
            self.import_url,
            "/api/v1/documents",
            f"/api/v1/documents/{uuid.uuid4()}/versions",
        ]


@pytest.fixture
async def ctx(tenant_a: Tenant, admin_a: User, raw_db: AsyncSession) -> AsyncIterator[Ctx]:
    c = Class(tenant_id=tenant_a.id, name="Grade 9")
    raw_db.add(c)
    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    tenant = [("Host", "localhost"), ("X-Tenant-Slug", tenant_a.slug)]
    yield Ctx(tenant=tenant, auth=[*tenant, ("Authorization", f"Bearer {token}")], class_id=c.id)


def _framing(body: bytes, boundary: str) -> list[Header]:
    return [("Content-Type", f"multipart/form-data; boundary={boundary}"), ("Content-Length", str(len(body)))]


def _unique_ip() -> str:
    return f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250 + 1}"


# ─── Finding 2: header confusion ─────────────────────────────────────────────

@pytest.mark.parametrize("index", range(5))
async def test_repeated_content_type_or_length_is_refused_on_upload_routes(ctx: Ctx, index: int) -> None:
    await assert_duplicate_headers_rejected(app, "POST", ctx.upload_routes()[index], ctx.auth)


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/v1/auth/login"), ("GET", "/health"), ("POST", "/api/v1/elsewhere"), ("PATCH", "/api/v1/admin/settings")],
)
async def test_repeated_headers_are_refused_on_every_route(ctx: Ctx, method: str, path: str) -> None:
    for extra in (
        [("Content-Type", "application/json"), ("Content-Type", "text/plain")],
        [("Content-Length", "2"), ("Content-Length", "2")],
    ):
        res = await call_asgi(app, method, path, ctx.tenant + extra, [b"{}"])
        assert res.status == 400, f"{method} {path} {extra}: {res.status}"
        assert res.json()["detail"]["code"] == "duplicate_header"
        assert res.chunks_consumed == 0


@pytest.mark.parametrize("value", ["abc", "-1", "+5", "1e3", "0x10", "12 34", "", " ", "1.0", "9" * 40])
async def test_content_length_must_be_a_plain_non_negative_integer(ctx: Ctx, value: str) -> None:
    for path in (ctx.import_url, "/health"):
        res = await call_asgi(app, "POST", path, [*ctx.auth, ("Content-Length", value)], [b"x"])
        assert res.status == 400, f"{path} Content-Length {value!r}: {res.status}"
        assert res.json() == {
            "detail": {"code": "invalid_content_length", "message": "The Content-Length header is not a valid number."}
        }
        assert res.chunks_consumed == 0


async def test_part_count_uses_the_boundary_starlette_uses(ctx: Ctx) -> None:
    """
    `xboundary=zz; boundary=real`: a naive regex reads "zz" and counts no parts, while Starlette
    parses with "real". The middleware must use the same parser, so the 6th part is refused.
    """
    parts = b"".join(
        f'--real\r\nContent-Disposition: form-data; name="f{i}"\r\n\r\nv\r\n'.encode() for i in range(8)
    ) + b"--real--\r\n"
    headers = [
        *ctx.auth,
        ("Content-Type", "multipart/form-data; xboundary=zz; boundary=real"),
        ("Content-Length", str(len(parts))),
    ]
    res = await call_asgi(app, "POST", ctx.import_url, headers, split(parts, 32))
    assert res.status == 413, res.body
    assert res.json()["detail"]["code"] == "too_many_parts"


# ─── Finding 3: no body is read before the Bearer check ─────────────────────

@pytest.mark.parametrize("index", range(7))
async def test_anonymous_or_bad_token_upload_gets_401_without_reading_the_body(ctx: Ctx, index: int) -> None:
    await assert_anonymous_upload_not_consumed(app, ctx.upload_routes()[index], ctx.tenant)


async def test_template_download_needs_a_bearer_token(ctx: Ctx) -> None:
    for name in ("template.csv", "template.xlsx"):
        path = f"/api/v1/classes/{ctx.class_id}/timetables/{name}"
        res = await call_asgi(app, "GET", path, ctx.tenant, [])
        assert res.status == 401, f"{name}: {res.status}"
        res = await call_asgi(app, "GET", path, [*ctx.tenant, ("Authorization", "Bearer x.y.z")], [])
        assert res.status == 401
        ok = await call_asgi(app, "GET", path, ctx.auth, [])
        assert ok.status == 200, ok.body


async def test_save_imported_draft_needs_a_bearer_token_before_the_body(ctx: Ctx) -> None:
    path = f"/api/v1/classes/{ctx.class_id}/timetables/save-imported-draft"
    body = b'{"entries": []}' + b" " * (1024 * 1024)
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    res = await call_asgi(app, "POST", path, ctx.tenant + headers, split(body))
    assert res.status == 401
    assert res.chunks_consumed == 0


async def test_valid_token_still_uploads_normally(ctx: Ctx) -> None:
    body = multipart_body("okbnd", "t.csv", CSV)
    res = await call_asgi(app, "POST", ctx.import_url, ctx.auth + _framing(body, "okbnd"), split(body, 32))
    assert res.status == 200, res.body
    assert res.json()["total_rows"] == 1
    assert res.chunks_consumed == res.chunks_total


async def test_valid_token_large_upload_is_parsed_and_spooled_by_starlette(ctx: Ctx) -> None:
    """Positive control for the spool recorder: an authenticated 1.5 MB upload IS spooled."""
    body = multipart_body("bigbnd", "t.csv", CSV + b"x" * (3 * MB // 2))
    with record_spools() as spools:
        res = await call_asgi(app, "POST", ctx.import_url, ctx.auth + _framing(body, "bigbnd"), split(body))
    assert res.status != 401, res.body
    assert spools, "Starlette did not spool the authenticated upload (the recorder is broken)"
    assert res.chunks_consumed == res.chunks_total


async def test_upload_without_content_length_gets_411(ctx: Ctx) -> None:
    body = multipart_body("nolen", "t.csv", CSV)
    headers = [*ctx.auth, ("Content-Type", "multipart/form-data; boundary=nolen")]
    res = await call_asgi(app, "POST", ctx.import_url, headers, split(body, 32))
    assert res.status == 411
    assert res.json() == {
        "detail": {"code": "length_required", "message": "The upload must include a Content-Length header."}
    }
    assert res.chunks_consumed == 0


async def test_upload_with_a_declared_size_over_the_limit_gets_413_before_the_body(ctx: Ctx) -> None:
    body = multipart_body("big", "t.csv", CSV)
    headers = [*ctx.auth, ("Content-Type", "multipart/form-data; boundary=big"), ("Content-Length", str(3 * MB))]
    res = await call_asgi(app, "POST", ctx.import_url, headers, split(body, 32))
    assert res.status == 413
    assert res.chunks_consumed == 0


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "multipart/form-data", "multipart/mixed; boundary=x",
     "application/x-www-form-urlencoded", "multipart/form-data; boundary="],
)
async def test_upload_must_be_multipart_form_data_with_a_boundary(ctx: Ctx, content_type: str) -> None:
    body = b"x" * 100
    headers = [*ctx.auth, ("Content-Type", content_type), ("Content-Length", str(len(body)))]
    res = await call_asgi(app, "POST", ctx.import_url, headers, [body])
    assert res.status == 415, f"{content_type}: {res.status}"
    assert res.json()["detail"]["code"] == "unsupported_media_type"
    assert res.chunks_consumed == 0


async def test_lying_content_length_is_still_cut_off_on_a_real_form_route(ctx: Ctx) -> None:
    """Content-Length says 1 KB, 6 MB arrive: the counted stream stops near 2 MB + 64 KB."""
    body = multipart_body("lie", "t.csv", b"x" * (6 * MB))
    headers = [*ctx.auth, ("Content-Type", "multipart/form-data; boundary=lie"), ("Content-Length", "1024")]
    chunks = split(body)
    res = await call_asgi(app, "POST", ctx.import_url, headers, chunks)
    assert res.status == 413
    assert res.chunks_consumed <= (2 * MB + 64 * 1024) // (64 * 1024) + 2


async def test_per_ip_in_flight_cap_returns_429_and_releases(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import body_limit

    monkeypatch.setattr(body_limit, "UPLOAD_IP_MAX_IN_FLIGHT", 1, raising=False)
    ip = _unique_ip()
    body = multipart_body("capbnd", "t.csv", CSV)
    headers = ctx.auth + _framing(body, "capbnd")
    hold, started = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(
        call_asgi(app, "POST", ctx.import_url, headers, split(body, 32), client_ip=ip, hold=hold, started=started)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        second = await call_asgi(app, "POST", ctx.import_url, headers, split(body, 32), client_ip=ip)
        assert second.status == 429, f"second upload from the same IP was not capped ({second.status})"
        assert second.json()["detail"]["code"] == "too_many_concurrent_uploads"
        assert second.header("retry-after"), "429 without Retry-After"
        assert second.chunks_consumed == 0
        other_ip = await call_asgi(app, "POST", ctx.import_url, headers, split(body, 32), client_ip=_unique_ip())
        assert other_ip.status == 200, "the cap is not per IP"
    finally:
        hold.set()
        first_res = await asyncio.wait_for(first, timeout=30)
    assert first_res.status == 200, first_res.body
    third = await call_asgi(app, "POST", ctx.import_url, headers, split(body, 32), client_ip=ip)
    assert third.status == 200, f"the slot was not released after the first upload ({third.status})"


async def test_per_ip_cap_fails_closed_outside_development(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(redis_client, "get_redis", broken)
    body = multipart_body("down", "t.csv", CSV)
    res = await call_asgi(app, "POST", ctx.import_url, ctx.auth + _framing(body, "down"), split(body, 32))
    assert res.status == 503
    assert res.json()["detail"]["code"] == "service_unavailable"
    assert res.chunks_consumed == 0


FORM_MEDIA_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


def test_every_form_route_of_the_real_app_is_in_the_upload_registry() -> None:
    """
    Derived from the real routes (the app's OpenAPI schema lists every operation whose body is a
    form): each one must be a registered multipart upload route, so it gets the pre-body checks.
    """
    from app.core.body_limit import match_upload_route

    found = []
    for path, operations in app.openapi()["paths"].items():
        sample = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        for method, op in operations.items():
            content = op.get("requestBody", {}).get("content", {})
            if not any(media in content for media in FORM_MEDIA_TYPES):
                continue
            rule = match_upload_route(method.upper(), sample)
            assert rule is not None and rule.kind == "multipart", f"{method.upper()} {path} parses a form but is not registered"
            found.append((method.upper(), path))
    assert len(found) >= 5, found  # logo, favicon, users import, enrollment import, timetable import


# ─── Finding 5: middleware order in main.py ──────────────────────────────────

def test_body_limit_runs_before_every_layer_that_could_read_the_body() -> None:
    """Outermost first: only CORS (which never reads the body) may sit in front of the limit/auth check."""
    classes = [m.cls for m in app.user_middleware]
    assert BodyLimitMiddleware in classes
    i = classes.index(BodyLimitMiddleware)
    assert classes[:i] == [CORSMiddleware], classes
    # Form parsing only happens inside the routes, so every route is behind the check
    assert all(c is not BodyLimitMiddleware for c in classes[i + 1 :])


async def test_middleware_refusals_carry_cors_headers(ctx: Ctx) -> None:
    """A browser must be able to read the 401 (to refresh its token), so CORS wraps the check."""
    origin = settings.cors_origins[0]
    res = await call_asgi(app, "POST", ctx.import_url, [*ctx.tenant, ("Origin", origin)], [b"x"])
    assert res.status == 401
    assert res.header("access-control-allow-origin") == origin
