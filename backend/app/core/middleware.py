"""
app/core/middleware.py
──────────────────────
TenantResolutionMiddleware

Runs on every request before any route handler.  It:
  1. Generates (or reads from X-Request-ID) a request_id and stores it in
     both the request state and structlog context.
  2. Resolves the tenant slug from the Host header for public/auth routes.
  3. For authenticated routes, the JWT dependency (in api/deps.py) later
     validates that the JWT's tenant_id matches the host's tenant_id.

Tenant resolution strategy (from implementation_plan.md § 1.1):
  - Host: {slug}.platform.local → slug = "slug"
  - Host: platform.local         → platform/superadmin routes (no tenant)
  - Host: localhost or 127.0.0.1 → dev convenience; slug extracted from
    X-Tenant-Slug header or query param `tenant_slug` for local testing.
"""

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.context import set_request_context

logger = structlog.get_logger(__name__)

# Routes that belong to the platform (superadmin) and do not need a tenant.
_PLATFORM_PREFIXES = ("/api/v1/platform",)
# Routes that are fully public (health, openapi).
_PUBLIC_PREFIXES = ("/health", "/docs", "/openapi", "/redoc")


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that sets the per-request tenant context.

    The resolved tenant_id is stored in a contextvar so that:
      - `get_tenant_session` can inject it into every DB transaction.
      - structlog includes it in every log line.
      - API dependencies can read it without touching the request object.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 1. Request ID — from header if provided by a proxy, else new UUID.
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        # 2. Resolve tenant slug from the Host header.
        slug = _extract_slug(request)
        request.state.tenant_slug = slug

        # 3. Populate structlog / contextvar context for this coroutine.
        set_request_context(request_id=request_id, tenant_slug=slug)

        # Note: tenant_id (UUID) is NOT set here — we don't have a DB
        # connection yet.  It is set by the authenticated route's dependency
        # after fetching the tenant row.  For public auth routes (login,
        # register) the tenant slug is enough to look up the tenant.

        response = await call_next(request)
        # Echo back the request_id so clients can trace a request.
        response.headers["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Injects standard defensive HTTP security headers on all responses.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if not settings.is_development:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response


def _extract_slug(request: Request) -> str | None:
    """
    Return the tenant slug from the Host header or dev-mode fallback headers.
    Returns None for platform routes and public routes with no tenant context.
    """
    path = request.url.path

    # Platform and public routes don't need a tenant.
    if any(path.startswith(p) for p in _PLATFORM_PREFIXES + _PUBLIC_PREFIXES):
        return None

    host = request.headers.get("host", "").split(":")[0].lower()
    platform_domain = settings.PLATFORM_DOMAIN.lower()

    # Production / staging: {slug}.platform.com
    if host.endswith(f".{platform_domain}"):
        return host[: -len(f".{platform_domain}")]

    # Exact platform domain with no subdomain → superadmin / platform.
    if host == platform_domain:
        return None

    # Subdomain of localhost: e.g. acme.localhost -> acme
    if host.endswith(".localhost"):
        return host[: -len(".localhost")]

    # Local dev: use X-Tenant-Slug header or ?tenant_slug= query param.
    if host in ("localhost", "127.0.0.1", "platform.local"):
        slug = request.headers.get("X-Tenant-Slug")
        if not slug:
            slug = request.query_params.get("tenant_slug")
        return slug or None

    return None

