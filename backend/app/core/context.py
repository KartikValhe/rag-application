"""
app/core/context.py
───────────────────
Request-scoped context variables.

We store per-request values (tenant_id, request_id, current user claims) as:
  - Python contextvars  — thread / coroutine-safe, no global state.
  - structlog contextvars — automatically merged into every log line.

The middleware sets these at request entry.  Dependencies and services read them.
"""

from contextvars import ContextVar

import structlog

# ── Context variables set by TenantResolutionMiddleware ──────────────────────

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_tenant_slug_var: ContextVar[str | None] = ContextVar("tenant_slug", default=None)


def set_request_context(
    *,
    request_id: str,
    tenant_id: str | None = None,
    tenant_slug: str | None = None,
) -> None:
    """Called once per request by the middleware."""
    _request_id_var.set(request_id)
    _tenant_id_var.set(tenant_id)
    _tenant_slug_var.set(tenant_slug)
    # Merge into structlog so every log line in this coroutine carries context.
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        tenant_id=tenant_id,
    )


def get_request_id() -> str:
    return _request_id_var.get()


def get_request_tenant_id() -> str:
    """
    FastAPI dependency: return the tenant_id set by the middleware.
    Raises if no tenant context is present — routes that call this dependency
    will fail fast rather than silently running without tenant isolation.
    """
    tid = _tenant_id_var.get()
    if tid is None:
        raise ValueError(
            "No tenant_id in request context. "
            "Ensure TenantResolutionMiddleware ran and set the tenant."
        )
    return tid


def get_request_tenant_slug() -> str | None:
    return _tenant_slug_var.get()

