"""
Step 6a-5 (docs/phase6-plan.md 9.1, 9.7 step 1): one tenant_settings row per tenant.

Creating a tenant through the platform route writes exactly one row, in the new tenant's own context (app_tenant
with app.tenant_id set; app_platform has no grant on the table). The token budget override is read in a tenant
session.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db import engine as db_engine
from app.db.models.tenant import Tenant
from app.services import tenant_settings as ts


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client: AsyncClient, token: str, slug: str) -> Any:
    return await client.post(
        "/api/v1/platform/tenants",
        headers=_headers(token),
        json={"slug": slug, "name": "Settings School", "kind": "school", "registration_mode": "invite_only"},
    )


async def _rows(raw_db: AsyncSession, tenant_id: uuid.UUID | str) -> list[tuple[Any, ...]]:
    res = await raw_db.execute(
        text(
            "SELECT tenant_id, chat_enabled, chat_retention_days, allowed_llm_providers, chat_daily_token_budget "
            "FROM app.tenant_settings WHERE tenant_id = :t"
        ),
        {"t": tenant_id},
    )
    return [tuple(r) for r in res.all()]


class _StatementLog:
    """Every statement run on one engine, with its parameters."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine.sync_engine
        self.statements: list[tuple[str, Any]] = []

    def __enter__(self) -> _StatementLog:
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *_a: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._on)

    def _on(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        self.statements.append((" ".join(statement.split()), parameters))


async def test_creating_a_tenant_gives_exactly_one_settings_row_with_the_defaults(
    client: AsyncClient, platform_token: str, raw_db: AsyncSession
) -> None:
    resp = await _create(client, platform_token, f"settings-{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 201, resp.text
    tenant_id = uuid.UUID(resp.json()["id"])
    assert await _rows(raw_db, tenant_id) == [(tenant_id, True, 90, None, None)]


async def test_the_row_is_written_by_app_tenant_in_the_new_tenants_context(
    client: AsyncClient, platform_token: str
) -> None:
    with _StatementLog(db_engine.engine) as tenant_log, _StatementLog(db_engine.platform_engine) as platform_log:
        resp = await _create(client, platform_token, f"settings-ctx-{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 201, resp.text
    tenant_id = resp.json()["id"]

    assert not [s for s, _ in platform_log.statements if "tenant_settings" in s], "app_platform touched tenant_settings"
    inserts = [i for i, (s, _) in enumerate(tenant_log.statements) if s.startswith("INSERT INTO app.tenant_settings")]
    assert len(inserts) == 1, tenant_log.statements
    # The transaction of that insert set app.tenant_id to the NEW tenant first.
    before = tenant_log.statements[: inserts[0]]
    contexts = [p for s, p in before if "set_config('app.tenant_id'" in s]
    assert contexts, "no tenant context was set before the insert"
    assert str(tenant_id) in str(contexts[-1]), f"the context set before the insert was {contexts[-1]!r}"


async def test_a_second_create_with_the_same_slug_adds_no_row(
    client: AsyncClient, platform_token: str, raw_db: AsyncSession
) -> None:
    slug = f"settings-dup-{uuid.uuid4().hex[:12]}"
    first = await _create(client, platform_token, slug)
    assert first.status_code == 201
    second = await _create(client, platform_token, slug)
    assert second.status_code == 409
    assert len(await _rows(raw_db, first.json()["id"])) == 1


async def test_other_tenants_rows_are_untouched_by_a_create(
    client: AsyncClient, platform_token: str, raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, chat_daily_token_budget) VALUES (:t, 777)"), {"t": tenant_a.id}
    )
    await raw_db.commit()
    resp = await _create(client, platform_token, f"settings-other-{uuid.uuid4().hex[:12]}")
    assert resp.status_code == 201
    assert await _rows(raw_db, tenant_a.id) == [(tenant_a.id, True, 90, None, 777)]


async def test_create_default_settings_is_idempotent(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    async with tenant_session_context(str(tenant_a.id)) as session:
        assert await ts.create_default_settings(session, tenant_a.id) is True
    async with tenant_session_context(str(tenant_a.id)) as session:
        assert await ts.create_default_settings(session, tenant_a.id) is False
    assert len(await _rows(raw_db, tenant_a.id)) == 1


async def test_create_default_settings_for_another_tenant_is_refused_by_rls(tenant_a: Tenant, tenant_b: Tenant) -> None:
    with pytest.raises(Exception, match="row-level security"):
        async with tenant_session_context(str(tenant_a.id)) as session:
            await ts.create_default_settings(session, tenant_b.id)


async def test_read_token_budget_override(tenant_a: Tenant, tenant_b: Tenant, raw_db: AsyncSession) -> None:
    async with tenant_session_context(str(tenant_a.id)) as session:
        assert await ts.read_token_budget_override(session, tenant_a.id) is None  # no row: platform default
    await raw_db.execute(text("INSERT INTO app.tenant_settings (tenant_id) VALUES (:t)"), {"t": tenant_a.id})
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, chat_daily_token_budget) VALUES (:t, 9000)"), {"t": tenant_b.id}
    )
    await raw_db.commit()
    async with tenant_session_context(str(tenant_a.id)) as session:
        assert await ts.read_token_budget_override(session, tenant_a.id) is None  # NULL: platform default
        # Asking for tenant B's value from tenant A's session sees nothing (RLS), never B's 9000.
        assert await ts.read_token_budget_override(session, tenant_b.id) is None
    await raw_db.execute(
        text("UPDATE app.tenant_settings SET chat_daily_token_budget = 1500 WHERE tenant_id = :t"), {"t": tenant_a.id}
    )
    await raw_db.commit()
    async with tenant_session_context(str(tenant_a.id)) as session:
        assert await ts.read_token_budget_override(session, tenant_a.id) == 1500
