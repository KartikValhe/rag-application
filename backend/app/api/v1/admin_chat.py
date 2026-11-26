"""
app/api/v1/admin_chat.py
────────────────────────
The tenant's chat settings (docs/phase6-plan.md 8.1, 8.2, 9.1, 9.6; step 6a-12a).

  GET   /api/v1/admin/chat/settings    the effective values (defaults when the tenant has no row); creates nothing
  PATCH /api/v1/admin/chat/settings    change some of them (upsert of the tenant's one row), audited

Permission settings:manage only (the tenant admin). Both routes use require_released (no pooled connection is held
after the permission check, B100) and then one short tenant transaction of their own. Every refusal of PATCH is a
422 with a stable code (app/services/chat_settings.py): invalid_setting, provider_not_allowed, token_budget_invalid,
token_budget_above_platform_ceiling. The body is checked by the service, not by FastAPI's schema, so that an unknown
field or a wrong type also answers invalid_setting; only a body that is not JSON at all gets FastAPI's own 422.

Each real change writes one chat:settings_update audit row in the same transaction, with the changed field names and
their old and new values (booleans, numbers, provider names): never text, never a secret. A PATCH that changes
nothing writes no audit row.
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.core.authz import require_released
from app.core.tenant_db import tenant_session_context
from app.db.models.user import User
from app.schemas.chat import ChatSettingsResponse, ChatSettingsUpdateRequest
from app.services import chat_settings
from app.services.audit import record_audit_event

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/chat", tags=["admin-chat"])

SettingsAdmin = Annotated[User, Depends(require_released("settings:manage"))]

_UPDATE_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": ChatSettingsUpdateRequest.model_json_schema()}},
    }
}


def _response(s: chat_settings.ChatSettings) -> ChatSettingsResponse:
    return ChatSettingsResponse(
        chat_enabled=s.chat_enabled,
        retention_days=s.retention_days,
        allowed_llm_providers=None if s.allowed_llm_providers is None else list(s.allowed_llm_providers),
        daily_token_budget=s.daily_token_budget,
        effective_daily_token_budget=s.effective_daily_token_budget,
    )


@router.get("/settings", response_model=ChatSettingsResponse)
async def get_chat_settings(user: SettingsAdmin) -> ChatSettingsResponse:
    """The tenant's effective chat settings. Reads only: a tenant without a row gets the defaults, and no row is made."""
    async with tenant_session_context(str(user.tenant_id)) as db:
        current = await chat_settings.read_effective(db, user.tenant_id)
    return _response(current)


@router.patch("/settings", response_model=ChatSettingsResponse, openapi_extra=_UPDATE_SCHEMA)
async def patch_chat_settings(
    request: Request, user: SettingsAdmin, body: Annotated[Any, Body()]
) -> ChatSettingsResponse:
    """Change some of the tenant's chat settings. 422 with a stable code for any refused field (nothing is written
    then). Audited as chat:settings_update with the changed fields' old and new values."""
    try:
        fields = chat_settings.parse_update(body, chat_settings.known_providers())
    except chat_settings.SettingsRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": exc.code, "message": exc.message}
        ) from None
    async with tenant_session_context(str(user.tenant_id)) as db:
        updated, record = await chat_settings.apply_update(db, user.tenant_id, user.id, fields)
        if record:
            await record_audit_event(
                tenant_id=user.tenant_id,
                action="chat:settings_update",
                target_type="tenant_settings",
                target_id=str(user.tenant_id),
                details={"changes": record},
                actor_id=user.id,
                request=request,
                db=db,
            )
    logger.info("chat_settings_updated", tenant_id=str(user.tenant_id), fields=sorted(record))
    return _response(updated)
