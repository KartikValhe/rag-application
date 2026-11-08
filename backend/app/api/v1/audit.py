"""
app/api/v1/audit.py
───────────────────
Audit log viewer endpoint:
  - Read-only, paginated, filterable.
  - Requires 'audit:view' permission (organization admin).
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import TenantDB
from app.core.authz import require
from app.db.models.audit import AuditLog
from app.db.models.user import User

router = APIRouter(tags=["audit"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class AuditLogResponse(BaseModel):
    id: str
    actor_id: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    request_id: str | None = None
    created_at: str


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    db: TenantDB,
    current_user: User = Depends(require("audit:view")),
    actor_id: uuid.UUID | None = None,
    action: str | None = Query(None, max_length=100),
    target_type: str | None = Query(None, max_length=50),
    target_id: str | None = Query(None, max_length=100),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> AuditLogListResponse:
    """
    List audit logs for the current tenant.
    Filterable by actor, action, target_type, target_id, and date range.
    """
    query = select(AuditLog).where(AuditLog.tenant_id == current_user.tenant_id)

    if actor_id is not None:
        query = query.where(AuditLog.actor_id == actor_id)

    if action:
        query = query.where(AuditLog.action == action.strip())

    if target_type:
        query = query.where(AuditLog.target_type == target_type.strip())

    if target_id:
        query = query.where(AuditLog.target_id == target_id.strip())

    if date_from is not None:
        query = query.where(AuditLog.created_at >= date_from)

    if date_to is not None:
        query = query.where(AuditLog.created_at <= date_to)

    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_res.scalar_one()

    result = await db.execute(
        query.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit)
    )
    rows = result.scalars().all()

    items = [
        AuditLogResponse(
            id=str(log.id),
            actor_id=str(log.actor_id) if log.actor_id else None,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            details=log.details or {},
            ip_address=log.ip_address,
            request_id=log.request_id,
            created_at=log.created_at.isoformat() if hasattr(log.created_at, "isoformat") else str(log.created_at),
        )
        for log in rows
    ]
    return AuditLogListResponse(items=items, total=total)

