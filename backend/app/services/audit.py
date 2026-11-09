"""
app/services/audit.py
─────────────────────
Audit logging service.

Rules:
  1. Append-only: Writes to app.audit_log.
  2. Redaction: Automatically strips sensitive keys (passwords, tokens, secrets).
  3. Context-aware: Extracts IP and request_id from Request when available.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_client_ip
from app.db.models.audit import AuditLog

# Keys that must NEVER be written to the audit log
SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "authorization",
    "cookie",
}


def sanitize_audit_details(data: Any) -> Any:
    """Recursively strip any sensitive keys from details dict."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if k.lower() in SENSITIVE_KEYS:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_audit_details(v)
        return sanitized
    if isinstance(data, list):
        return [sanitize_audit_details(item) for item in data]
    return data


async def record_audit_event(
    *,
    tenant_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    actor_id: uuid.UUID | None = None,
    request: Request | None = None,
    db: AsyncSession,
) -> AuditLog:
    """
    Append an entry to the tenant's audit trail.
    """
    ip_address = None
    request_id = None

    if request:
        ip_address = get_client_ip(request)
        request_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id")

    safe_details = sanitize_audit_details(details or {})

    entry = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        details=safe_details,
        ip_address=ip_address,
        request_id=request_id,
    )
    db.add(entry)
    await db.flush()
    return entry

