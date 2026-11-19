"""
app/main.py
───────────
FastAPI application entry point.

Startup order:
  1. Configure structured logging.
  2. Mount middleware (tenant resolution, CORS).
  3. Register routers.
  4. Connect Redis pool on startup, close on shutdown.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import router as v1_router
from app.core.authz import public_endpoint
from app.core.body_limit import BodyLimitMiddleware
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.middleware import SecurityHeadersMiddleware, TenantResolutionMiddleware
from app.core.redis_client import close_redis, get_redis

try:
    import email_validator

    # Permit local development domains (.local, .localhost, .internal) in EmailStr validation
    for _d in ("local", "localhost", "internal"):
        if _d in getattr(email_validator, "SPECIAL_USE_DOMAIN_NAMES", []):
            email_validator.SPECIAL_USE_DOMAIN_NAMES.remove(_d)
except ImportError:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run startup and shutdown tasks."""
    # Warm up Redis connection pool.
    await get_redis()
    yield
    # Graceful Redis shutdown.
    await close_redis()


# Configure logging before anything else so all startup logs are structured.
configure_logging()

app = FastAPI(
    title="RAG Platform API",
    version="0.1.0",
    description="White-label multi-tenant RAG chatbot platform.",
    # Only expose OpenAPI in development.
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
    openapi_url="/openapi.json" if settings.is_development else None,
    lifespan=lifespan,
)

# ── Middleware (order matters: outermost is applied first) ────────────────────

# Add order is the reverse of the run order. Outermost first: CORS, security headers, body guard,
# tenant resolution, routes.
#
# Tenant resolution must run before any route handler so that context vars
# (request_id, tenant_slug) are set when route dependencies execute.
app.add_middleware(TenantResolutionMiddleware)

# Body guard: duplicate headers, Content-Length, the pre-body Bearer check on upload routes, the
# per-IP upload cap and the body limits. It must run before anything reads the body; forms are
# only parsed inside the routes.
app.add_middleware(BodyLimitMiddleware)

# Security headers sit OUTSIDE the body guard, so its 401/411/413 carry them too, and they also
# cover an unhandled error (turned into a generic 500 inside this layer). Only CORS is in front of
# it: CORS never reads the body, and a browser can then read these errors (for example to refresh
# an expired token).
app.add_middleware(SecurityHeadersMiddleware)

# Configure CORS with an explicit origin whitelist (outermost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # Required for the refresh-token cookie.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Tenant-Slug",
        "X-Request-ID",
        "X-Requested-With",
        "Accept",
        "Origin",
    ],
    expose_headers=["X-Request-ID"],
)

# ── Request validation errors ─────────────────────────────────────────────────


def _json_safe(value: Any) -> Any:
    """Strings with a lone surrogate (valid as a JSON escape, but not as UTF-8 text) get U+FFFD in its place."""
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {_json_safe(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422 (the same body: {"detail": [errors]}), except that the echoed input can always be
    encoded. The default handler failed on a lone surrogate and the request ended as a 500 (5d-2)."""
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(v1_router)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"], dependencies=[Depends(public_endpoint())])
async def health() -> dict[str, str]:
    """Minimal liveness probe.  Does not hit the DB (k8s readiness is separate)."""
    return {"status": "ok", "version": "0.1.0"}

