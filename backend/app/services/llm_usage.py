"""
app/services/llm_usage.py
─────────────────────────
One llm_usage_events row per LLM call (docs/phase6-plan.md 9.1): for cost accounting and the Phase 8 usage page.

No content of any kind: there is no parameter for a question, an answer, a prompt, a title or any other text. The two
names (provider, model_key) must look like a provider on the allow-list and a model key of that provider, so text
cannot be smuggled into them; the table's CHECK constraints repeat the rule. A refused value raises UsageRecordError
naming the field only, never the value, and nothing is written.

Runs in the caller's TENANT session (app_tenant with app.tenant_id set): the RLS WITH CHECK refuses a row for another
tenant. app_tenant may only INSERT and SELECT here; a row is never changed afterwards. No caller until 6a-10/6a-11.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.llm_usage import (
    MODEL_KEY_MAX,
    MODEL_KEY_PATTERN,
    USAGE_OUTCOMES,
    USAGE_PURPOSES,
    LLMUsageEvent,
)
from app.llm.providers import is_allowed_provider, split_model_key

_INT4_MAX: Final = 2**31 - 1
_INT2_MAX: Final = 2**15 - 1
_MODEL_KEY = re.compile(MODEL_KEY_PATTERN.removeprefix("^").removesuffix("$") + r"\Z")


class UsageRecordError(ValueError):
    """A usage row was refused. The message names the field and the rule, never the value."""


def _count(name: str, value: object, maximum: int, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise UsageRecordError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _optional_uuid(name: str, value: object) -> uuid.UUID | None:
    if value is not None and not isinstance(value, uuid.UUID):
        raise UsageRecordError(f"{name} must be a UUID or None")
    return value


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    purpose: str,
    provider: str,
    model_key: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated: bool,
    latency_ms: int,
    outcome: str,
    attempt: int,
) -> uuid.UUID:
    """Insert one usage row and return its id. `estimated` is True when the provider reported no usage and the
    numbers come from the budget layer's estimate (plan 7.7)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise UsageRecordError("tenant_id must be a UUID")
    user = _optional_uuid("user_id", user_id)
    message = _optional_uuid("message_id", message_id)
    if purpose not in USAGE_PURPOSES:
        raise UsageRecordError("purpose is not one of the usage purposes")
    if outcome not in USAGE_OUTCOMES:
        raise UsageRecordError("outcome is not one of the usage outcomes")
    if not is_allowed_provider(provider):
        raise UsageRecordError("provider is not on the allow-list")
    if not isinstance(model_key, str) or len(model_key) > MODEL_KEY_MAX or not _MODEL_KEY.match(model_key):
        raise UsageRecordError("model_key must look like 'provider:model-id'")
    try:
        key_provider, _ = split_model_key(model_key)
    except ValueError:
        raise UsageRecordError("model_key must look like 'provider:model-id'") from None
    if key_provider != provider:
        raise UsageRecordError("model_key does not belong to provider")
    if not isinstance(estimated, bool):
        raise UsageRecordError("estimated must be a bool")
    row_id = uuid.uuid4()
    await session.execute(
        insert(LLMUsageEvent).values(
            tenant_id=tenant_id,
            id=row_id,
            user_id=user,
            message_id=message,
            purpose=purpose,
            provider=provider,
            model_key=model_key,
            prompt_tokens=_count("prompt_tokens", prompt_tokens, _INT4_MAX),
            completion_tokens=_count("completion_tokens", completion_tokens, _INT4_MAX),
            estimated=estimated,
            latency_ms=_count("latency_ms", latency_ms, _INT4_MAX),
            outcome=outcome,
            attempt=_count("attempt", attempt, _INT2_MAX, minimum=1),
        )
    )
    return row_id
