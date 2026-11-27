"""
app/services/chat_settings.py
─────────────────────────────
The tenant's chat settings as the admin route reads and changes them: GET and PATCH /api/v1/admin/chat/settings
(docs/phase6-plan.md 8.1, 8.2, 9.1, 9.6, step 6a-12a; owner decision 2 of the 6a-12 brief).

  - GET returns the EFFECTIVE values: the stored row, or the column defaults when the tenant has no row. It never
    creates a row.
  - PATCH validates strictly (parse_update, no database), then upserts the tenant's one row in the same transaction
    as its audit row, and changes only the fields it names. Codes (6a-12a brief, plan 8.4):
      invalid_setting               the body is not a JSON object, is empty, names an unknown field (there is no
                                    bot-name setting: plan 5.1 has none, backlog), or a value has the wrong type or is
                                    out of range;
      provider_not_allowed          a name in allowed_llm_providers that is not EXACTLY a provider of models.json (no
                                    case folding, no trimming);
      token_budget_invalid,
      token_budget_above_platform_ceiling   the 6a-5 codes of llm_budget.check_budget_override, kept for the budget.
    The first problem wins; nothing is written when any field is refused.
  - retention_days is null (keep everything, the default: Q4 and C35, owner decisions of the 6a-12b brief) or whole days
    from 30 to 3650. Setting it back to null is a change like any other and is audited.
  - The change record (the audit details) holds field names with their old and new values: booleans, numbers, null and
    provider names. Never free text, never a secret.

Rows are read and written in a tenant session (app_tenant: SELECT, INSERT, UPDATE on tenant_settings, plan 9.2);
every query also names the tenant itself (tenant_settings.TENANT_FILTER).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenant_settings import (
    CHAT_RETENTION_DAYS_MAX,
    CHAT_RETENTION_DAYS_MIN,
    TenantSettings,
)
from app.llm.models import load_registry
from app.services import llm_budget, tenant_settings
from app.services.tenant_settings import TENANT_FILTER

# The API names of the settings, in the order they are checked, and the column each one is stored in.
COLUMNS: Final[dict[str, str]] = {
    "chat_enabled": "chat_enabled",
    "retention_days": "chat_retention_days",
    "allowed_llm_providers": "allowed_llm_providers",
    "daily_token_budget": "chat_daily_token_budget",
}
SETTING_KEYS: Final = tuple(COLUMNS)

# One fixed text per code: never the value that was sent.
ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_setting": "One of the settings is not valid.",
    "provider_not_allowed": "One of the providers is not allowed.",
    "token_budget_invalid": "The daily token budget must be a positive whole number.",
    "token_budget_above_platform_ceiling": "The daily token budget may not be above the platform ceiling.",
}


class SettingsRefused(ValueError):
    """A PATCH body that may not be stored (422 with `code`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = ERROR_MESSAGES[code]


def error_message(code: str) -> str:
    return ERROR_MESSAGES[code]


@dataclass(frozen=True, slots=True)
class ChatSettings:
    """The effective settings of one tenant."""

    chat_enabled: bool
    retention_days: int | None  # None: keep everything (Q4, C35); otherwise whole days from 30 to 3650
    allowed_llm_providers: tuple[str, ...] | None  # None: the platform chain; (): no LLM (retrieval-only)
    daily_token_budget: int | None  # None: the platform default

    @property
    def effective_daily_token_budget(self) -> int:
        return llm_budget.effective_tenant_limit(self.daily_token_budget)


DEFAULTS: Final = ChatSettings(
    chat_enabled=True, retention_days=None, allowed_llm_providers=None, daily_token_budget=None
)


def known_providers() -> frozenset[str]:
    """The exact provider names of the models in models.json (owner decision 2)."""
    return frozenset(model.provider for model in load_registry().models)


# ── Validation (no database) ──────────────────────────────────────────────────


def _chat_enabled(value: object, _providers: frozenset[str]) -> bool:
    if type(value) is not bool:
        raise SettingsRefused("invalid_setting")
    return value


def _retention_days(value: object, _providers: frozenset[str]) -> int | None:
    """null (keep everything) or a whole number of days from 30 to 3650. Nothing else, not a string, a float or a bool."""
    if value is None:
        return None
    if type(value) is not int or not CHAT_RETENTION_DAYS_MIN <= value <= CHAT_RETENTION_DAYS_MAX:
        raise SettingsRefused("invalid_setting")
    return value


def _providers(value: object, providers: frozenset[str]) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) is not list or any(type(name) is not str for name in value):
        raise SettingsRefused("invalid_setting")
    if len(set(value)) != len(value):
        raise SettingsRefused("invalid_setting")
    if any(name not in providers for name in value):
        raise SettingsRefused("provider_not_allowed")
    return tuple(sorted(value))


def _budget(value: object, _providers: frozenset[str]) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise SettingsRefused("token_budget_invalid")
    try:
        llm_budget.check_budget_override(value)
    except llm_budget.BudgetOverrideRefused as exc:
        raise SettingsRefused(exc.code) from None
    return value


_PARSERS: Final[dict[str, Callable[[object, frozenset[str]], Any]]] = {
    "chat_enabled": _chat_enabled,
    "retention_days": _retention_days,
    "allowed_llm_providers": _providers,
    "daily_token_budget": _budget,
}


def parse_update(body: object, providers: frozenset[str]) -> dict[str, Any]:
    """The fields a PATCH body sets, checked and normalised (a provider list is stored sorted). Raises SettingsRefused."""
    if type(body) is not dict or not body:
        raise SettingsRefused("invalid_setting")
    if any(key not in COLUMNS for key in body):
        raise SettingsRefused("invalid_setting")
    return {key: _PARSERS[key](body[key], providers) for key in SETTING_KEYS if key in body}


def _audit_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def changes(current: ChatSettings, update: dict[str, Any]) -> dict[str, dict[str, object]]:
    """{field: {"old", "new"}} for every field whose value really changes, in SETTING_KEYS order."""
    out: dict[str, dict[str, object]] = {}
    for key in SETTING_KEYS:
        if key in update and update[key] != getattr(current, key):
            out[key] = {"old": _audit_value(getattr(current, key)), "new": _audit_value(update[key])}
    return out


# ── Database ──────────────────────────────────────────────────────────────────


_SELECT = (
    "SELECT chat_enabled, chat_retention_days, allowed_llm_providers, chat_daily_token_budget "
    f"FROM app.tenant_settings WHERE {TENANT_FILTER}"
)


def _from_row(row: Any) -> ChatSettings:
    enabled, retention, providers, budget = row
    return ChatSettings(
        chat_enabled=bool(enabled),
        retention_days=None if retention is None else int(retention),
        allowed_llm_providers=None if providers is None else tuple(str(p) for p in providers),
        daily_token_budget=None if budget is None else int(budget),
    )


async def read_effective(db: AsyncSession, tenant_id: uuid.UUID) -> ChatSettings:
    """The stored row, or DEFAULTS when there is none. Creates nothing."""
    row = (await db.execute(text(_SELECT), {"tenant_id": tenant_id})).one_or_none()
    return DEFAULTS if row is None else _from_row(row)


async def apply_update(
    db: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID, update: dict[str, Any]
) -> tuple[ChatSettings, dict[str, dict[str, object]]]:
    """Upsert the tenant's row with `update` (from parse_update). Returns the new effective settings and the change
    record; when nothing really changes, nothing is updated (the row exists afterwards, with the values it had or the
    defaults). The row is locked first, so two admins saving at once are applied one after the other."""
    await tenant_settings.create_default_settings(db, tenant_id)
    row = (await db.execute(text(f"{_SELECT} FOR UPDATE"), {"tenant_id": tenant_id})).one()
    current = _from_row(row)
    record = changes(current, update)
    if record:
        values: dict[str, Any] = {
            COLUMNS[key]: list(update[key]) if isinstance(update[key], tuple) else update[key] for key in record
        }
        await db.execute(
            sql_update(TenantSettings)
            .where(TenantSettings.tenant_id == tenant_id)
            .values(**values, updated_at=func.clock_timestamp(), updated_by=actor_id)
            .execution_options(synchronize_session=False)
        )
    return dataclasses.replace(current, **{key: update[key] for key in update}), record
