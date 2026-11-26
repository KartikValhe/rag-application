"""
app/services/tenant_settings.py
───────────────────────────────
The one tenant_settings row of a tenant (docs/phase6-plan.md 9.1, migration 0012).

Every function here runs in a TENANT session (app_tenant with app.tenant_id set): app_platform and app_worker have no
grant on the table, so the row of a new tenant can only be written in that tenant's own context. The queries also
filter on tenant_id themselves (TENANT_FILTER); RLS alone already limits them to the session's tenant, which
tests/security/test_prove_it_fails_usage.py proves by switching the filter off.

A tenant without a row (the moment between the platform transaction and the settings insert, see
`create_for_new_tenant`) reads as all defaults, which is exactly what its row would hold.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context

# The application's own tenant filter. A prove-it test replaces it with TRUE to show that RLS alone still holds.
TENANT_FILTER = "tenant_id = :tenant_id"

_INSERT_DEFAULT_ROW = (
    "INSERT INTO app.tenant_settings (tenant_id) VALUES (:tenant_id) ON CONFLICT (tenant_id) DO NOTHING"
)


async def create_default_settings(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Insert the default row for `tenant_id` in the caller's tenant session. True when inserted, False when the
    tenant already had one (idempotent). Another tenant's id is refused by the RLS WITH CHECK."""
    result = await session.execute(text(_INSERT_DEFAULT_ROW), {"tenant_id": tenant_id})
    return bool(getattr(result, "rowcount", 0) == 1)


async def create_for_new_tenant(tenant_id: uuid.UUID) -> None:
    """Write the default row of a tenant that was just created and committed (the platform create path). It runs in
    its own short transaction inside the new tenant's context: the tenants row must be committed first, because the
    foreign key check of this insert cannot see another connection's uncommitted row."""
    async with tenant_session_context(str(tenant_id)) as session:
        await create_default_settings(session, tenant_id)


async def read_chat_enabled(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """tenant_settings.chat_enabled of the tenant. No row reads as the default (true), like every other value."""
    result = await session.execute(
        text(f"SELECT chat_enabled FROM app.tenant_settings WHERE {TENANT_FILTER}"),
        {"tenant_id": tenant_id},
    )
    value = result.scalar_one_or_none()
    return True if value is None else bool(value)


async def read_token_budget_override(session: AsyncSession, tenant_id: uuid.UUID) -> int | None:
    """The tenant's chat_daily_token_budget, or None (no row, or NULL): the platform default applies then. The budget
    layer (app/services/llm_budget.py) refuses a value above the platform ceiling."""
    result = await session.execute(
        text(f"SELECT chat_daily_token_budget FROM app.tenant_settings WHERE {TENANT_FILTER}"),
        {"tenant_id": tenant_id},
    )
    value = result.scalar_one_or_none()
    return None if value is None else int(value)
