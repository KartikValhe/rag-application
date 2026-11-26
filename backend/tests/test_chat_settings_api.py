"""
tests/test_chat_settings_api.py
───────────────────────────────
GET and PATCH /api/v1/admin/chat/settings (plan 8.1, 8.2, 9.1, 9.6; step 6a-12a; owner decision 2 of the 6a-12 brief).

  - permission settings:manage only (the route matrix covers every role);
  - GET returns the effective values with the defaults and creates nothing;
  - PATCH upserts the tenant's one row, changes only the fields it names and is audited as chat:settings_update with
    the changed field names and their old and new values (no other key, no text); a refused or no-op PATCH writes
    nothing and audits nothing;
  - the row belongs to the caller's tenant only, and the chat turn reads what PATCH wrote (6a-11 narrowing, chat_disabled);
  - no route holds two pooled connections at once.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tenant_db import tenant_session_context
from app.db.engine import engine as app_engine
from app.db.models.audit import AuditLog
from app.db.models.tenant_settings import TenantSettings
from app.services import tenant_settings
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth

URL = "/api/v1/admin/chat/settings"
KEYS = {"chat_enabled", "retention_days", "allowed_llm_providers", "daily_token_budget", "effective_daily_token_budget"}


def _defaults() -> dict[str, Any]:
    return {"chat_enabled": True, "retention_days": 90, "allowed_llm_providers": None, "daily_token_budget": None,
            "effective_daily_token_budget": settings.LLM_TENANT_DAILY_TOKENS}


async def _row(raw_db: AsyncSession, w: WorldTenant) -> TenantSettings | None:
    await raw_db.commit()
    return (await raw_db.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == w.id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _audits(raw_db: AsyncSession, w: WorldTenant) -> list[AuditLog]:
    await raw_db.commit()
    return list((await raw_db.execute(
        select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "chat:settings_update")
        .order_by(AuditLog.created_at)
    )).scalars().all())


async def _patch(client: AsyncClient, w: WorldTenant, body: Any) -> Any:
    return await client.patch(URL, headers=auth(w, w.admin), json=body)


# ── GET ───────────────────────────────────────────────────────────────────────


async def test_get_without_a_row_returns_the_defaults_and_creates_nothing(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    assert await _row(raw_db, w) is None
    resp = await client.get(URL, headers=auth(w, w.admin))
    assert resp.status_code == 200, resp.text
    assert resp.json() == _defaults()
    assert await _row(raw_db, w) is None


async def test_get_returns_the_stored_values_and_the_effective_budget(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    raw_db.add(TenantSettings(tenant_id=w.id, chat_enabled=False, chat_retention_days=30, allowed_llm_providers=[],
                              chat_daily_token_budget=1234))
    await raw_db.commit()
    resp = await client.get(URL, headers=auth(w, w.admin))
    assert resp.json() == {"chat_enabled": False, "retention_days": 30, "allowed_llm_providers": [],
                           "daily_token_budget": 1234, "effective_daily_token_budget": 1234}
    assert set(resp.json()) == KEYS


async def test_a_stored_budget_above_a_lowered_ceiling_shows_the_platform_default_as_effective(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    raw_db.add(TenantSettings(tenant_id=w.id, chat_daily_token_budget=5000))
    await raw_db.commit()
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 4000)
    body = (await client.get(URL, headers=auth(w, w.admin))).json()
    assert (body["daily_token_budget"], body["effective_daily_token_budget"]) == (5000, settings.LLM_TENANT_DAILY_TOKENS)


# ── PATCH ─────────────────────────────────────────────────────────────────────


async def test_patch_without_a_row_inserts_it_and_audits_the_changes(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    resp = await _patch(client, w, {"retention_days": 30, "allowed_llm_providers": ["groq"]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == _defaults() | {"retention_days": 30, "allowed_llm_providers": ["groq"]}
    row = await _row(raw_db, w)
    assert row is not None
    assert (row.chat_enabled, row.chat_retention_days, row.allowed_llm_providers, row.chat_daily_token_budget) == (
        True, 30, ["groq"], None)
    assert row.updated_by == w.admin.id
    audits = await _audits(raw_db, w)
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.actor_id, audit.target_type, audit.target_id) == (w.admin.id, "tenant_settings", str(w.id))
    assert audit.details == {"changes": {"retention_days": {"old": 90, "new": 30},
                                         "allowed_llm_providers": {"old": None, "new": ["groq"]}}}


async def test_patch_changes_only_the_named_fields_and_audits_only_real_changes(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    raw_db.add(TenantSettings(tenant_id=w.id, chat_enabled=True, chat_retention_days=45, allowed_llm_providers=["groq"],
                              chat_daily_token_budget=777))
    await raw_db.commit()
    before = (await _row(raw_db, w))
    assert before is not None
    first_updated = before.updated_at
    resp = await _patch(client, w, {"chat_enabled": False, "retention_days": 45})
    assert resp.status_code == 200, resp.text
    row = await _row(raw_db, w)
    assert row is not None
    assert (row.chat_enabled, row.chat_retention_days, row.allowed_llm_providers, row.chat_daily_token_budget) == (
        False, 45, ["groq"], 777)
    assert row.updated_at > first_updated
    assert [a.details for a in await _audits(raw_db, w)] == [{"changes": {"chat_enabled": {"old": True, "new": False}}}]

    # A no-op PATCH writes nothing: capture the value now (_row refreshes the same ORM object in place)
    updated_after_change = row.updated_at
    same = await _patch(client, w, {"chat_enabled": False, "daily_token_budget": 777})
    assert same.status_code == 200
    assert len(await _audits(raw_db, w)) == 1
    again = await _row(raw_db, w)
    assert again is not None and again.updated_at == updated_after_change

    cleared = await _patch(client, w, {"allowed_llm_providers": None, "daily_token_budget": None})
    assert cleared.json()["allowed_llm_providers"] is None and cleared.json()["daily_token_budget"] is None
    assert (await _audits(raw_db, w))[-1].details == {"changes": {
        "allowed_llm_providers": {"old": ["groq"], "new": None}, "daily_token_budget": {"old": 777, "new": None}}}


@pytest.mark.parametrize(("body", "code"), [
    ({}, "invalid_setting"), ([], "invalid_setting"), ("x", "invalid_setting"), ({"bot_name": "Ada"}, "invalid_setting"),
    ({"chat_enabled": "no"}, "invalid_setting"), ({"retention_days": 0}, "invalid_setting"),
    ({"retention_days": 731}, "invalid_setting"), ({"allowed_llm_providers": ["Groq"]}, "provider_not_allowed"),
    ({"allowed_llm_providers": [" groq"]}, "provider_not_allowed"),
    ({"allowed_llm_providers": ["groq", "groq"]}, "invalid_setting"),
    ({"daily_token_budget": 0}, "token_budget_invalid"),
    ({"daily_token_budget": 10**12}, "token_budget_above_platform_ceiling"),
    ({"chat_enabled": False, "allowed_llm_providers": ["nope"]}, "provider_not_allowed"),
])
async def test_a_refused_patch_is_422_with_its_code_and_writes_nothing(
    body: Any, code: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    resp = await _patch(client, w, body)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert set(detail) == {"code", "message"} and detail["code"] == code
    assert "nope" not in resp.text and "Ada" not in resp.text and "Groq" not in resp.text
    assert await _row(raw_db, w) is None
    assert await _audits(raw_db, w) == []


async def test_a_body_that_is_not_json_is_422(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    resp = await client.patch(URL, headers=auth(w, w.admin) | {"Content-Type": "application/json"}, content=b"{nope")
    assert resp.status_code == 422
    assert await _row(raw_db, w) is None


async def test_the_row_is_the_callers_tenant_only(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    a, b = academic_world.a, academic_world.b
    assert (await _patch(client, a, {"chat_enabled": False})).status_code == 200
    assert await _row(raw_db, b) is None
    assert (await client.get(URL, headers=auth(b, b.admin))).json() == _defaults()
    assert (await _patch(client, b, {"retention_days": 10})).status_code == 200
    row_a, row_b = await _row(raw_db, a), await _row(raw_db, b)
    assert row_a is not None and row_b is not None
    assert (row_a.chat_enabled, row_a.chat_retention_days) == (False, 90)
    assert (row_b.chat_enabled, row_b.chat_retention_days) == (True, 10)
    assert [x.details for x in await _audits(raw_db, b)] == [{"changes": {"retention_days": {"old": 90, "new": 10}}}]


async def test_the_chat_reads_what_patch_wrote(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    assert (await _patch(client, w, {"allowed_llm_providers": [], "daily_token_budget": 4321})).status_code == 200
    async with tenant_session_context(str(w.id)) as db:
        llm = await tenant_settings.read_llm_settings(db, w.id)
    assert (llm.allowed_llm_providers, llm.token_budget_override) == (frozenset(), 4321)
    assert (await _patch(client, w, {"chat_enabled": False})).status_code == 200
    resp = await client.post("/api/v1/chat/conversations", headers=auth(w, w.s1),
                             json={"class_id": str(w.classes["10-A"].id), "subject_id": None})
    assert (resp.status_code, resp.json()["detail"]["code"]) == (503, "chat_disabled")


async def test_a_teacher_cannot_read_or_change_the_settings(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    for user in (w.t1, w.s1, w.viewer):
        assert (await client.get(URL, headers=auth(w, user))).status_code == 403
        assert (await client.patch(URL, headers=auth(w, user), json={"chat_enabled": False})).status_code == 403
    assert await _row(raw_db, w) is None


# ── Connections ───────────────────────────────────────────────────────────────


class _PoolWatch:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def checkout(self, *_a: Any) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def checkin(self, *_a: Any) -> None:
        self.current -= 1


@pytest.fixture
def pool_watch() -> Iterator[_PoolWatch]:
    watch = _PoolWatch()
    event.listen(app_engine.sync_engine, "checkout", watch.checkout)
    event.listen(app_engine.sync_engine, "checkin", watch.checkin)
    try:
        yield watch
    finally:
        event.remove(app_engine.sync_engine, "checkout", watch.checkout)
        event.remove(app_engine.sync_engine, "checkin", watch.checkin)


async def test_the_settings_routes_never_hold_two_connections(
    client: AsyncClient, academic_world: AcademicWorld, pool_watch: _PoolWatch
) -> None:
    w = academic_world.a
    assert (await client.get(URL, headers=auth(w, w.admin))).status_code == 200
    assert (await _patch(client, w, {"retention_days": 12})).status_code == 200
    assert pool_watch.peak == 1, f"{pool_watch.peak} connections were checked out at the same time"
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]
