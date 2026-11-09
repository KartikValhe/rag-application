"""
app/api/v1/branding.py
──────────────────────
White-label theming and design token endpoints:
  - Public branding resolver (Host-header based, cached in Redis, ETag, rate-limited, no-enumeration fallback).
  - Public media asset server (safe headers, local object storage).
  - Admin branding management (tokens/text update, logo/favicon upload with metadata stripping, reset to defaults).
"""

import json
import re
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, TenantDB
from app.core.authz import public_endpoint, require
from app.core.redis_client import get_redis
from app.core.security import get_client_ip
from app.core.tenant_db import get_platform_session, tenant_session_context
from app.db.models.branding import TenantBranding
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.schemas.branding import (
    DEFAULT_TOKENS,
    BrandingResponse,
    BrandingUpdateRequest,
    DesignTokens,
    PublicBrandingResponse,
)
from app.services.audit import record_audit_event
from app.services.storage import get_storage, validate_and_sanitize_image

public_router = APIRouter(prefix="/public/branding", tags=["branding"])
admin_router = APIRouter(prefix="/admin/branding", tags=["branding"])

PUBLIC_BRANDING_THROTTLE_LIMIT = 600
PUBLIC_BRANDING_THROTTLE_WINDOW = 60  # seconds


# ── Helpers ────────────────────────────────────────────────────────────────────

def _default_public_branding() -> PublicBrandingResponse:
    """Fallback branding when tenant is unknown, inactive, or unconfigured."""
    return PublicBrandingResponse(
        display_name="RAG Platform",
        tagline=None,
        footer_text=None,
        support_email=None,
        tokens=DesignTokens(**DEFAULT_TOKENS),
        logo_url=None,
        favicon_url=None,
        version=1,
    )


def _to_public_branding_response(branding: TenantBranding) -> PublicBrandingResponse:
    storage = get_storage()
    return PublicBrandingResponse(
        display_name=branding.display_name,
        tagline=branding.tagline,
        footer_text=branding.footer_text,
        support_email=branding.support_email,
        tokens=DesignTokens(**branding.tokens),
        logo_url=storage.get_url(branding.logo_key) if branding.logo_key else None,
        favicon_url=storage.get_url(branding.favicon_key) if branding.favicon_key else None,
        version=branding.version,
    )


def _to_admin_branding_response(branding: TenantBranding) -> BrandingResponse:
    storage = get_storage()
    return BrandingResponse(
        display_name=branding.display_name,
        tagline=branding.tagline,
        footer_text=branding.footer_text,
        support_email=branding.support_email,
        tokens=DesignTokens(**branding.tokens),
        logo_key=branding.logo_key,
        logo_url=storage.get_url(branding.logo_key) if branding.logo_key else None,
        favicon_key=branding.favicon_key,
        favicon_url=storage.get_url(branding.favicon_key) if branding.favicon_key else None,
        version=branding.version,
        updated_at=branding.updated_at,
    )


async def _check_public_rate_limit(request: Request) -> None:
    """Sliding rate limit on public branding endpoint per IP."""
    try:
        redis = await get_redis()
        ip = get_client_ip(request)
        key = f"throttle:public_branding:{ip}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, PUBLIC_BRANDING_THROTTLE_WINDOW)
        if count > PUBLIC_BRANDING_THROTTLE_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "rate_limited", "message": "Too many requests. Please try again later."},
            )
    except HTTPException:
        raise
    except Exception:
        # If Redis is temporarily unreachable, do not hard-fail public requests
        pass


async def _get_or_create_tenant_branding(
    tenant_id: uuid.UUID,
    db: AsyncSession,
    tenant_name: str = "",
) -> TenantBranding:
    """Fetch existing branding for a tenant or initialize defaults."""
    res = await db.execute(select(TenantBranding).where(TenantBranding.tenant_id == tenant_id))
    branding = res.scalar_one_or_none()
    if branding is None:
        branding = TenantBranding(
            tenant_id=tenant_id,
            tokens=DEFAULT_TOKENS,
            display_name=tenant_name,
            version=1,
        )
        db.add(branding)
        await db.flush()
        await db.refresh(branding)
    return branding


async def _invalidate_branding_cache(
    tenant_id: uuid.UUID,
    db: AsyncSession,
    request: Request | None = None,
    old_version: int | None = None,
) -> None:
    """Invalidate Redis cache for tenant branding by slug and version."""
    try:
        redis = await get_redis()
        tenant_res = await db.execute(select(Tenant.slug).where(Tenant.id == tenant_id))
        slug = tenant_res.scalar_one_or_none()
        if slug:
            await redis.delete(f"cache:branding:slug:{slug}")
        if request:
            req_slug = getattr(request.state, "tenant_slug", None)
            if req_slug and req_slug != slug:
                await redis.delete(f"cache:branding:slug:{req_slug}")
        if old_version is not None:
            await redis.delete(f"cache:branding:{tenant_id}:{old_version}")
    except Exception:
        pass


# ── Public Endpoints ──────────────────────────────────────────────────────────

@public_router.get("", response_model=PublicBrandingResponse, dependencies=[Depends(public_endpoint())])
async def get_public_branding(
    request: Request,
    platform_db: AsyncSession = Depends(get_platform_session),
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """
    Public, unauthenticated branding resolver.
    Resolves tenant from Host header or dev fallbacks.
    Returns safe fields with ETag and Cache-Control headers.
    Returns identical default branding on unknown or suspended tenant (no enumeration).
    """
    await _check_public_rate_limit(request)

    # Resolve tenant slug
    slug = getattr(request.state, "tenant_slug", None)
    if not slug:
        slug = request.headers.get("X-Tenant-Slug") or request.query_params.get("tenant_slug")

    # If no slug provided, return default branding
    if not slug:
        default_resp = _default_public_branding()
        etag = f'W/"default-{default_resp.version}"'
        if if_none_match == etag:
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
            )
        return Response(
            content=default_resp.model_dump_json(),
            media_type="application/json",
            headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
        )

    # Check Redis cache first
    redis = None
    try:
        redis = await get_redis()
        cached = await redis.get(f"cache:branding:slug:{slug}")
        if cached:
            cached_data = json.loads(cached)
            etag = f'W/"{cached_data.get("tenant_id", "default")}-{cached_data["data"]["version"]}"'
            if if_none_match == etag:
                return Response(
                    status_code=status.HTTP_304_NOT_MODIFIED,
                    headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
                )
            return Response(
                content=json.dumps(cached_data["data"]),
                media_type="application/json",
                headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
            )
    except Exception:
        pass

    # Look up tenant in database using platform session
    tenant_res = await platform_db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = tenant_res.scalar_one_or_none()

    # If tenant unknown or suspended, return default branding (fail closed, no enumeration)
    if tenant is None or tenant.status != "active":
        default_resp = _default_public_branding()
        etag = f'W/"default-{default_resp.version}"'
        if if_none_match == etag:
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
            )
        return Response(
            content=default_resp.model_dump_json(),
            media_type="application/json",
            headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
        )

    # Fetch branding in tenant RLS context.
    # If the tenant has no branding row in DB, return default branding safely (never an error).
    async with tenant_session_context(str(tenant.id)) as t_db:
        res = await t_db.execute(select(TenantBranding).where(TenantBranding.tenant_id == tenant.id))
        branding = res.scalar_one_or_none()
        if branding is None:
            branding_resp = PublicBrandingResponse(
                display_name=tenant.name or "RAG Platform",
                tagline=None,
                footer_text=None,
                support_email=None,
                tokens=DesignTokens(**DEFAULT_TOKENS),
                logo_url=None,
                favicon_url=None,
                version=1,
            )
        else:
            branding_resp = _to_public_branding_response(branding)

    etag = f'W/"{tenant.id}-{branding_resp.version}"'

    # Save to Redis cache
    if redis:
        try:
            cache_payload = {
                "tenant_id": str(tenant.id),
                "data": branding_resp.model_dump(),
            }
            await redis.set(f"cache:branding:slug:{slug}", json.dumps(cache_payload), ex=3600)
            await redis.set(
                f"cache:branding:{tenant.id}:{branding_resp.version}",
                json.dumps(cache_payload),
                ex=3600,
            )
        except Exception:
            pass

    if if_none_match == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
        )

    return Response(
        content=branding_resp.model_dump_json(),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
    )


@public_router.get(
    "/media/{tenant_id}/{asset_type}/{filename}",
    dependencies=[Depends(public_endpoint())],
)
async def get_branding_media(
    tenant_id: str,
    asset_type: str,
    filename: str,
) -> Response:
    """Serve uploaded branding media with strict defensive headers."""
    if asset_type not in ("logo", "favicon"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")

    if not re.match(r"^[a-f0-9]{32}\.(png|jpg|webp)$", filename):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid asset filename.")

    storage = get_storage()
    key = f"{tenant_id}/{asset_type}/{filename}"
    data = await storage.get(key)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")

    ext = filename.split(".")[-1].lower()
    media_type = "image/png" if ext == "png" else ("image/jpeg" if ext == "jpg" else "image/webp")

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


# ── Admin Endpoints ───────────────────────────────────────────────────────────

@admin_router.get("", response_model=BrandingResponse)
async def get_admin_branding(
    db: TenantDB,
    current_user: Annotated[User, Depends(require("branding:manage"))],
) -> BrandingResponse:
    """Retrieve the current tenant's complete branding configuration."""
    branding = await _get_or_create_tenant_branding(current_user.tenant_id, db)
    await db.refresh(branding)
    return _to_admin_branding_response(branding)


@admin_router.put("", response_model=BrandingResponse)
async def update_admin_branding(
    body: BrandingUpdateRequest,
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("branding:manage"))],
) -> BrandingResponse:
    """
    Update design tokens and branding copy for the tenant.
    Bumps version and invalidates Redis cache.
    """
    branding = await _get_or_create_tenant_branding(current_user.tenant_id, db)

    if body.display_name is not None:
        branding.display_name = body.display_name
    if body.tagline is not None:
        branding.tagline = body.tagline
    if body.footer_text is not None:
        branding.footer_text = body.footer_text
    if body.support_email is not None:
        branding.support_email = body.support_email
    if body.tokens is not None:
        branding.tokens = body.tokens.model_dump()

    branding.version += 1
    await db.flush()
    await db.refresh(branding)

    # Record audit log
    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="branding:update",
        target_type="branding",
        target_id=str(branding.id),
        details={"version": branding.version},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    # Invalidate Redis cache
    await _invalidate_branding_cache(
        tenant_id=current_user.tenant_id,
        db=db,
        request=request,
        old_version=branding.version - 1,
    )

    return _to_admin_branding_response(branding)


@admin_router.post("/logo", response_model=BrandingResponse)
async def upload_admin_logo(
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("branding:manage"))],
    file: UploadFile = File(...),
) -> BrandingResponse:
    """Upload and sanitize tenant logo (PNG, JPEG, WebP; max 1MB, max 1024x1024)."""
    file_bytes = await file.read()
    clean_bytes, content_type, ext = validate_and_sanitize_image(file_bytes, asset_type="logo")

    storage = get_storage()
    branding = await _get_or_create_tenant_branding(current_user.tenant_id, db)

    # Clean up previous logo if present
    if branding.logo_key:
        try:
            await storage.delete(branding.logo_key)
        except Exception:
            pass

    key = f"{current_user.tenant_id}/logo/{uuid.uuid4().hex}.{ext}"
    await storage.put(key, clean_bytes, content_type)

    branding.logo_key = key
    branding.version += 1
    await db.flush()
    await db.refresh(branding)

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="branding:upload_logo",
        target_type="branding",
        target_id=str(branding.id),
        details={"version": branding.version, "logo_key": key},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    await _invalidate_branding_cache(
        tenant_id=current_user.tenant_id,
        db=db,
        request=request,
        old_version=branding.version - 1,
    )

    return _to_admin_branding_response(branding)


@admin_router.post("/favicon", response_model=BrandingResponse)
async def upload_admin_favicon(
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("branding:manage"))],
    file: UploadFile = File(...),
) -> BrandingResponse:
    """Upload and sanitize tenant favicon (PNG, JPEG, WebP; max 1MB, max 256x256)."""
    file_bytes = await file.read()
    clean_bytes, content_type, ext = validate_and_sanitize_image(file_bytes, asset_type="favicon")

    storage = get_storage()
    branding = await _get_or_create_tenant_branding(current_user.tenant_id, db)

    # Clean up previous favicon if present
    if branding.favicon_key:
        try:
            await storage.delete(branding.favicon_key)
        except Exception:
            pass

    key = f"{current_user.tenant_id}/favicon/{uuid.uuid4().hex}.{ext}"
    await storage.put(key, clean_bytes, content_type)

    branding.favicon_key = key
    branding.version += 1
    await db.flush()
    await db.refresh(branding)

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="branding:upload_favicon",
        target_type="branding",
        target_id=str(branding.id),
        details={"version": branding.version, "favicon_key": key},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    await _invalidate_branding_cache(
        tenant_id=current_user.tenant_id,
        db=db,
        request=request,
        old_version=branding.version - 1,
    )

    return _to_admin_branding_response(branding)


@admin_router.post("/reset", response_model=BrandingResponse)
async def reset_admin_branding(
    request: Request,
    db: TenantDB,
    current_user: Annotated[User, Depends(require("branding:manage"))],
) -> BrandingResponse:
    """Reset branding tokens and copy to system defaults."""
    branding = await _get_or_create_tenant_branding(current_user.tenant_id, db)
    storage = get_storage()

    if branding.logo_key:
        try:
            await storage.delete(branding.logo_key)
        except Exception:
            pass
        branding.logo_key = None

    if branding.favicon_key:
        try:
            await storage.delete(branding.favicon_key)
        except Exception:
            pass
        branding.favicon_key = None

    branding.tokens = DEFAULT_TOKENS
    branding.tagline = None
    branding.footer_text = None
    branding.support_email = None
    branding.version += 1
    await db.flush()
    await db.refresh(branding)

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        action="branding:reset",
        target_type="branding",
        target_id=str(branding.id),
        details={"version": branding.version},
        actor_id=current_user.id,
        request=request,
        db=db,
    )

    await _invalidate_branding_cache(
        tenant_id=current_user.tenant_id,
        db=db,
        request=request,
        old_version=branding.version - 1,
    )

    return _to_admin_branding_response(branding)
