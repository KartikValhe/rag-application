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
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


# Content-Security-Policy values a route may set for itself besides the sandbox rule below. Exact
# policy strings, empty on purpose: add an entry only with a reason written next to it.
ALLOWED_ROUTE_CSPS: frozenset[str] = frozenset()

_DEFAULT_CSP = "default-src 'none'; frame-ancestors 'none'"
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "frame-ancestors 'none'"
)
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
_INTERNAL_ERROR_BODY = (
    b'{"detail":{"code":"internal_error","message":"Something went wrong. Please try again later."}}'
)


def _route_csp_is_allowed(value: str) -> bool:
    """
    A route's own CSP survives only when it is on the explicit allow-list or carries a plain
    `sandbox` directive (no allow-* flags), like the document download. Anything else, which could
    only be weaker than the default, is replaced.
    """
    if value in ALLOWED_ROUTE_CSPS:
        return True
    for part in value.split(";"):
        tokens = part.split()
        if tokens and tokens[0].lower() == "sandbox":
            return not any(t.lower().startswith("allow-") for t in tokens[1:])
    return False


class SecurityHeadersMiddleware:
    """
    Defensive HTTP headers on EVERY response, whoever produces it. It is a plain ASGI middleware that
    sits directly inside CORS (so a browser can still read an error) and outside everything else: the
    body guard's 401/411/413, route responses of any status (304 included) and the unhandled-error path
    all pass through it. An exception that escapes the app is answered with a generic 500 HERE, so that
    response carries the headers too (Starlette's own 500 handler sits outside every middleware and would
    send none), and is then re-raised for the server to log, as Starlette does.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                self._apply(MutableHeaders(scope=message), path)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if not started:
                # A generic body: the exception text never reaches the client
                await send_with_headers(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(_INTERNAL_ERROR_BODY)).encode()),
                        ],
                    }
                )
                await send_with_headers({"type": "http.response.body", "body": _INTERNAL_ERROR_BODY})
            # Like Starlette's own 500 handler: answer, then re-raise, so the server logs the error
            # (and a test client with raise_app_exceptions still sees it)
            raise

    @staticmethod
    def _apply(headers: MutableHeaders, path: str) -> None:
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if path in _DOCS_PATHS:
            headers["Content-Security-Policy"] = _DOCS_CSP
        else:
            own = headers.get("content-security-policy")
            if own is None or not _route_csp_is_allowed(own):
                headers["Content-Security-Policy"] = _DEFAULT_CSP
        if not settings.is_development:
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"


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

