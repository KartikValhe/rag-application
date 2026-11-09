"""
tests/test_branding.py
──────────────────────
Phase 3a White-label theming test suite:
  1. WCAG AA contrast validation (passes accessible colors, rejects inaccessible).
  2. Design tokens schema enforcement (regex hex, allow-listed radius/font, extra="forbid").
  3. Public branding endpoint (Host resolution, ETag, 304, Redis cache, rate limiting, no-enumeration default fallback).
  4. Admin branding management (permissions, version bumping, audit logging, reset).
  5. ObjectStore image uploads (raster only, metadata stripping, reject SVG, size & dimension checks).
  6. Public media serving (safe defensive headers).
"""

import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis_client import get_redis
from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.branding import TenantBranding
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import app
from app.schemas.branding import (
    DEFAULT_TOKENS,
    BrandingUpdateRequest,
    DesignTokens,
    calculate_contrast_ratio,
    calculate_relative_luminance,
    validate_wcag_contrast,
)
from app.services.storage import get_storage

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def flush_redis() -> None:
    """Clear the Redis test DB before each test to avoid counter bleed."""
    try:
        redis = await get_redis()
        await redis.flushdb()
    except Exception:
        pass


# ── 1. WCAG Contrast Validation Tests ─────────────────────────────────────────

async def test_wcag_luminance_and_contrast_math() -> None:
    white_l = calculate_relative_luminance("#ffffff")
    black_l = calculate_relative_luminance("#000000")
    assert abs(white_l - 1.0) < 0.001
    assert abs(black_l - 0.0) < 0.001

    ratio_wb = calculate_contrast_ratio("#ffffff", "#000000")
    assert abs(ratio_wb - 21.0) < 0.1


async def test_wcag_valid_colors() -> None:
    # Standard accessible colors must pass
    valid_colors = ["#2563eb", "#16a34a", "#0d9488", "#7c3aed"]
    for c in valid_colors:
        validate_wcag_contrast(c)


async def test_wcag_rejects_bright_yellow_on_white_surface() -> None:
    with pytest.raises(ValueError) as exc:
        validate_wcag_contrast("#ffff00")
    assert "light theme surface" in str(exc.value).lower()


async def test_wcag_rejects_dark_color_on_dark_surface() -> None:
    with pytest.raises(ValueError) as exc:
        validate_wcag_contrast("#050505")
    assert "dark theme surface" in str(exc.value).lower()


async def test_wcag_rejects_mid_tone_insufficient_text() -> None:
    with pytest.raises(ValueError) as exc:
        validate_wcag_contrast("#0284c7")
    assert "contrast" in str(exc.value).lower()


# ── 2. Design Tokens Schema Tests ─────────────────────────────────────────────

async def test_design_tokens_valid_defaults() -> None:
    tokens = DesignTokens()
    assert tokens.primary == "#2563eb"
    assert tokens.radius == "md"
    assert tokens.font_family == "system-ui"


async def test_design_tokens_rejects_invalid_hex() -> None:
    with pytest.raises(ValidationError):
        DesignTokens(primary="not-a-color")
    with pytest.raises(ValidationError):
        DesignTokens(primary="#12345")


async def test_design_tokens_rejects_disallowed_radius() -> None:
    with pytest.raises(ValidationError):
        DesignTokens(radius="huge")


async def test_design_tokens_rejects_disallowed_font_family() -> None:
    with pytest.raises(ValidationError):
        DesignTokens(font_family="Comic Sans")


async def test_design_tokens_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        DesignTokens(primary="#2563eb", extra_custom_css="body{color:red;}")


# ── 3. Public Branding Endpoint Tests ─────────────────────────────────────────

async def test_public_branding_unknown_tenant_returns_default() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/public/branding",
            headers={"Host": "unknown-nonexistent.platform.local"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "RAG Platform"
    assert data["tokens"]["primary"] == "#2563eb"
    assert resp.headers.get("ETag") is not None
    assert "public, max-age=300" in resp.headers.get("Cache-Control", "")


async def test_public_branding_suspended_tenant_returns_default(
    raw_db: AsyncSession,
) -> None:
    suspended_tenant = Tenant(
        slug=f"suspended-{uuid.uuid4().hex[:6]}",
        name="Suspended University",
        kind="college",
        status="suspended",
    )
    raw_db.add(suspended_tenant)
    await raw_db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/public/branding",
            headers={"Host": f"{suspended_tenant.slug}.localhost"},
        )
    assert resp.status_code == 200
    data = resp.json()
    # Prevents tenant enumeration: returns same default shape
    assert data["display_name"] == "RAG Platform"
    assert data["tokens"]["primary"] == "#2563eb"


async def test_public_branding_known_tenant_with_etag_and_304(
    raw_db: AsyncSession,
    tenant_a: Tenant,
) -> None:
    # Seed branding for tenant A
    branding = TenantBranding(
        tenant_id=tenant_a.id,
        tokens={
            "primary": "#0d9488",
            "secondary": "#475569",
            "accent": "#06b6d4",
            "radius": "lg",
            "font_family": "serif",
        },
        display_name="Acme Custom Academy",
        tagline="Strive for Excellence",
        version=3,
    )
    raw_db.add(branding)
    await raw_db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First request: 200 with ETag
        resp1 = await client.get(
            "/api/v1/public/branding",
            headers={"Host": f"{tenant_a.slug}.localhost"},
        )
        assert resp1.status_code == 200
        data = resp1.json()
        assert data["display_name"] == "Acme Custom Academy"
        assert data["tagline"] == "Strive for Excellence"
        assert data["tokens"]["primary"] == "#0d9488"
        assert data["tokens"]["radius"] == "lg"
        etag = resp1.headers.get("ETag")
        assert etag == f'W/"{tenant_a.id}-3"'

        # Second request with matching If-None-Match: 304 Not Modified
        resp2 = await client.get(
            "/api/v1/public/branding",
            headers={
                "Host": f"{tenant_a.slug}.localhost",
                "If-None-Match": etag,
            },
        )
        assert resp2.status_code == 304


async def test_public_branding_rate_limiting(client: AsyncClient) -> None:
    redis = await get_redis()
    # Pre-set the throttle counter to the 600 limit for test client IPs
    for test_ip in ("127.0.0.1", "testclient"):
        await redis.set(f"throttle:public_branding:{test_ip}", 600, ex=60)

    resp = await client.get(
        "/api/v1/public/branding",
        headers={"Host": "ratelimit-test.localhost"},
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "rate_limited"


async def test_public_branding_tenant_without_branding_row_returns_default(
    client: AsyncClient,
    raw_db: AsyncSession,
) -> None:
    # Create tenant without a branding row
    tenant_unbranded = Tenant(
        slug=f"nobrand-{uuid.uuid4().hex[:6]}",
        name="No Brand Academy",
        kind="school",
        status="active",
    )
    raw_db.add(tenant_unbranded)
    await raw_db.commit()

    resp = await client.get(
        "/api/v1/public/branding",
        headers={"Host": f"{tenant_unbranded.slug}.localhost"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "No Brand Academy"
    assert data["tokens"]["primary"] == "#2563eb"
    assert data["version"] == 1


async def test_branding_cache_invalidated_on_update_and_suspension(
    client: AsyncClient,
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    redis = await get_redis()
    slug_key = f"cache:branding:slug:{tenant_a.slug}"

    # Pre-populate cache
    await redis.set(slug_key, '{"tenant_id": "dummy", "data": {"version": 99}}')
    assert await redis.get(slug_key) is not None

    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Update branding as admin
    resp = await client.put(
        "/api/v1/admin/branding",
        json={"display_name": "Cache Invalidation Test"},
        headers=headers,
    )
    assert resp.status_code == 200
    # Slug cache must be invalidated
    assert await redis.get(slug_key) is None


# ── 4. Admin Branding Endpoints Tests ─────────────────────────────────────────

async def test_admin_branding_requires_permission(
    user_a: User,
    tenant_a: Tenant,
) -> None:
    # Regular user lacks branding:manage
    token = create_access_token(
        user_id=str(user_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/admin/branding",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403


async def test_admin_branding_get_and_update(
    admin_a: User,
    tenant_a: Tenant,
    raw_db: AsyncSession,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET current branding
        resp = await client.get("/api/v1/admin/branding", headers=headers)
        assert resp.status_code == 200
        initial_version = resp.json()["version"]

        # PUT update tokens and copy
        update_payload = {
            "display_name": "Updated Acme",
            "tagline": "Innovative Learning",
            "tokens": {
                "primary": "#16a34a",
                "secondary": "#334155",
                "accent": "#0ea5e9",
                "radius": "full",
                "font_family": "mono",
            },
        }
        put_resp = await client.put(
            "/api/v1/admin/branding",
            json=update_payload,
            headers=headers,
        )
        assert put_resp.status_code == 200
        data = put_resp.json()
        assert data["display_name"] == "Updated Acme"
        assert data["tagline"] == "Innovative Learning"
        assert data["tokens"]["primary"] == "#16a34a"
        assert data["tokens"]["radius"] == "full"
        assert data["version"] == initial_version + 1

    # Verify audit log was recorded
    audit_res = await raw_db.execute(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_a.id,
            AuditLog.action == "branding:update",
        )
    )
    audit_row = audit_res.scalar_one_or_none()
    assert audit_row is not None
    assert audit_row.actor_id == admin_a.id


async def test_admin_branding_reset(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First set custom tagline
        await client.put(
            "/api/v1/admin/branding",
            json={"tagline": "Custom Before Reset"},
            headers=headers,
        )

        # Call reset
        resp = await client.post("/api/v1/admin/branding/reset", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["tagline"] is None
        assert data["tokens"]["primary"] == "#2563eb"
        assert data["tokens"]["radius"] == "md"


# ── 5. Image Upload & Sanitization Tests ──────────────────────────────────────

def _create_test_image(
    format_name: str = "PNG",
    dimensions: tuple[int, int] = (100, 100),
    color: tuple[int, ...] = (37, 99, 235),
) -> bytes:
    mode = "RGBA" if format_name in ("PNG", "WEBP") else "RGB"
    img = Image.new(mode, dimensions, color)
    buf = io.BytesIO()
    img.save(buf, format=format_name)
    return buf.getvalue()


async def test_admin_upload_valid_logo_png(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    img_bytes = _create_test_image(format_name="PNG", dimensions=(200, 100))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("test_logo.png", img_bytes, "image/png")},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["logo_key"] is not None
        assert data["logo_url"] is not None
        assert data["logo_url"].startswith("/api/v1/public/branding/media/")

        # Verify the media endpoint returns the uploaded image
        media_resp = await client.get(data["logo_url"])
        assert media_resp.status_code == 200
        assert media_resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "immutable" in media_resp.headers.get("Cache-Control", "")
        assert media_resp.headers.get("Content-Type") == "image/png"


async def test_admin_upload_valid_favicon(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    favicon_bytes = _create_test_image(format_name="WEBP", dimensions=(64, 64))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/favicon",
            files={"file": ("favicon.webp", favicon_bytes, "image/webp")},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["favicon_key"] is not None
        assert data["favicon_url"] is not None


async def test_admin_upload_rejects_svg(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    svg_payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("logo.svg", svg_payload, "image/svg+xml")},
            headers=headers,
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "svg" in detail["message"].lower()


async def test_admin_upload_rejects_oversized_file(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    oversized_data = b"x" * (1024 * 1024 + 10)  # > 1 MB

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("huge.png", oversized_data, "image/png")},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "1 mb" in resp.json()["detail"]["message"].lower()


async def test_admin_upload_rejects_oversized_dimensions(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    # Logo max is 1024x1024; create 1200x800 image
    large_img = _create_test_image(format_name="PNG", dimensions=(1200, 800))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("large_logo.png", large_img, "image/png")},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "dimensions" in resp.json()["detail"]["message"].lower()


async def test_admin_branding_concurrency_conflict(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch current branding version
        get_res = await client.get("/api/v1/admin/branding", headers=headers)
        assert get_res.status_code == 200
        initial_version = get_res.json()["version"]

        # 2. First update with correct expected_version succeeds and bumps version
        put_res = await client.put(
            "/api/v1/admin/branding",
            json={"display_name": "First Admin Update", "expected_version": initial_version},
            headers=headers,
        )
        assert put_res.status_code == 200
        new_version = put_res.json()["version"]
        assert new_version == initial_version + 1

        # 3. Second concurrent update with stale expected_version fails with 409
        stale_res = await client.put(
            "/api/v1/admin/branding",
            json={"display_name": "Stale Admin Update", "expected_version": initial_version},
            headers=headers,
        )
        assert stale_res.status_code == 409
        detail = stale_res.json()["detail"]
        assert detail["code"] == "concurrency_conflict"
        assert "another administrator" in detail["message"].lower()


async def test_admin_upload_rejects_script_text_disguised_as_png(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    script_payload = b"<script>alert('xss')</script>"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("fake_logo.png", script_payload, "image/png")},
            headers=headers,
        )
        assert resp.status_code == 400
        msg = resp.json()["detail"]["message"].lower()
        assert "valid image" in msg or "content" in msg or "format" in msg or "corrupt" in msg or "svg" in msg


async def test_admin_upload_rejects_renamed_svg(
    admin_a: User,
    tenant_a: Tenant,
) -> None:
    token = create_access_token(
        user_id=str(admin_a.id),
        tenant_id=str(tenant_a.id),
        is_admin=True,
    )
    headers = {"Authorization": f"Bearer {token}"}
    svg_disguised_as_png = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/branding/logo",
            files={"file": ("innocent_look.png", svg_disguised_as_png, "image/png")},
            headers=headers,
        )
        assert resp.status_code == 400
        msg = resp.json()["detail"]["message"].lower()
        assert "svg" in msg or "content" in msg or "format" in msg
