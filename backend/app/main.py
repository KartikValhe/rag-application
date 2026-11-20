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
from app.services import retrieval

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
    # One line with the retrieval threshold and whether it was calibrated for the configured model (5d-5).
    retrieval.log_threshold_calibration()
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


# The parts of a validation error that are about the SCHEMA, never what the client sent. "input" is dropped
# (for a missing field it is the whole body: a login without an email echoed the password). "ctx" is kept only
# with the schema's own limits (numbers and patterns from the model), never a validator's free text.
_PUBLIC_ERROR_KEYS = ("type", "loc", "msg")
_PUBLIC_CTX_KEYS = frozenset(
    {"ge", "gt", "le", "lt", "min_length", "max_length", "multiple_of", "max_digits", "decimal_places", "pattern", "expected"}
)


def _public_ctx(ctx: Any) -> dict[str, Any] | None:
    if not isinstance(ctx, dict):
        return None
    kept = {k: v for k, v in ctx.items() if k in _PUBLIC_CTX_KEYS and isinstance(v, int | float | str) and not isinstance(v, bool)}
    return kept or None


def public_errors(errors: Any) -> Any:
    """FastAPI's error list without the client's input (review of 5d-2, finding 8): type, loc, msg and, when it
    holds only the schema's limits, ctx. A module-level function so a prove-it test can switch it off."""
    out: list[dict[str, Any]] = []
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        public = {key: error[key] for key in _PUBLIC_ERROR_KEYS if key in error}
        ctx = _public_ctx(error.get("ctx"))
        if ctx is not None:
            public["ctx"] = ctx
        out.append(public)
    return _json_safe(out)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422 shape ({"detail": [errors]}) without the echoed input (public_errors). Location keys
    can hold a lone surrogate, so the body is made encodable (_json_safe): the default handler failed on one
    and the request ended as a 500 (5d-2)."""
    return JSONResponse(status_code=422, content={"detail": public_errors(jsonable_encoder(exc.errors()))})


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(v1_router)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"], dependencies=[Depends(public_endpoint())])
async def health() -> dict[str, str]:
    """Minimal liveness probe.  Does not hit the DB (k8s readiness is separate)."""
    return {"status": "ok", "version": "0.1.0"}

