"""
tests/test_isolation.py
─────────────────────────
Phase 1 exit criterion: tenant isolation test suite.

For every tenant-scoped table in TENANT_TABLES, this suite verifies:
  1. SELECT as tenant A returns only tenant A's rows (not tenant B's).
  2. UPDATE on tenant B's row (while in tenant A's context) affects 0 rows.
  3. DELETE on tenant B's row (while in tenant A's context) affects 0 rows.
  4. INSERT with tenant_id=B (while in tenant A's context) is rejected by the
     RLS WITH CHECK clause.
  5. A query run WITHOUT setting app.tenant_id returns 0 rows (fail-closed).

The suite is table-driven: add a new entry to TENANT_TABLES and the
isolation checks run automatically for the new table.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.auth import EmailToken, Invitation, RefreshToken
from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

# ── Table registry ────────────────────────────────────────────────────────────
# Each entry: (ORM model class, dict of extra columns needed for a valid INSERT).
# The test harness fills in tenant_id, id automatically.
TENANT_TABLES = [
    (User, {
        "email": lambda: f"iso-test-{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "Isolation Test User",
        "password_hash": "x",
        "status": "active",
    }),
    (RefreshToken, {
        "user_id": lambda: uuid.uuid4(),
        "token_hash": lambda: uuid.uuid4().hex,
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
    (Invitation, {
        "email": lambda: f"iso-inv-{uuid.uuid4().hex[:8]}@example.com",
        "token_hash": lambda: uuid.uuid4().hex,
        "invited_by": lambda: uuid.uuid4(),
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
    (EmailToken, {
        "user_id": lambda: uuid.uuid4(),
        "token_hash": lambda: uuid.uuid4().hex,
        "purpose": "verify_email",
        "expires_at": lambda: datetime(2099, 1, 1, tzinfo=UTC),
    }),
]


def _build_row(model: type, tenant_id: uuid.UUID, extra: dict) -> dict:
    """Resolve callables in `extra` and add tenant_id."""
    resolved = {k: (v() if callable(v) else v) for k, v in extra.items()}
    resolved["tenant_id"] = tenant_id
    return resolved


# ── 1. SELECT isolation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_select_returns_only_own_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    Rows inserted for tenant B must NOT appear in a session locked to tenant A.
    """
    # Insert a row for tenant B using the privileged (raw, no-RLS) session.
    row_b = _build_row(model, tenant_b.id, extra)
    await raw_db.execute(insert(model).values(**row_b))

    # SELECT through tenant A's RLS context.
    result = await tenant_a_session.execute(select(model))
    rows = result.scalars().all()

    for row in rows:
        assert row.tenant_id == tenant_a.id, (
            f"{model.__tablename__}: got row with tenant_id={row.tenant_id!r} "
            f"(belongs to tenant B) in tenant A's session."
        )


# ── 2. UPDATE isolation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_update_cannot_affect_other_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    An UPDATE in tenant A's context must not touch tenant B's rows.
    """
    row_b = _build_row(model, tenant_b.id, extra)
    result = await raw_db.execute(insert(model).values(**row_b).returning(model))
    inserted = result.scalar_one()

    # Attempt UPDATE from tenant A's session — should match 0 rows due to RLS.
    pk_col = list(model.__table__.primary_key.columns)[0]
    update_result = await tenant_a_session.execute(
        update(model).where(pk_col == inserted.id).values(tenant_id=tenant_a.id)
        if hasattr(inserted, "id") else
        update(model).where(model.tenant_id == tenant_b.id).values()
    )
    assert update_result.rowcount == 0, (
        f"{model.__tablename__}: UPDATE in tenant A's context affected "
        f"{update_result.rowcount} row(s) belonging to tenant B."
    )


# ── 3. DELETE isolation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_delete_cannot_affect_other_tenant_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    DELETE in tenant A's context must leave tenant B's rows untouched.
    """
    row_b = _build_row(model, tenant_b.id, extra)
    result = await raw_db.execute(insert(model).values(**row_b).returning(model))
    inserted = result.scalar_one()

    pk_col = list(model.__table__.primary_key.columns)[0]
    del_result = await tenant_a_session.execute(
        delete(model).where(pk_col == inserted.id)
        if hasattr(inserted, "id") else
        delete(model).where(model.tenant_id == tenant_b.id)
    )
    assert del_result.rowcount == 0, (
        f"{model.__tablename__}: DELETE in tenant A's context removed "
        f"{del_result.rowcount} row(s) belonging to tenant B."
    )


# ── 4. INSERT WITH CHECK ──────────────────────────────────────────────────────

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_insert_wrong_tenant_id_rejected_by_rls(
    model: type,
    extra: dict,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
) -> None:
    """
    Inserting a row with tenant_id=B while in tenant A's RLS context must
    be rejected by the WITH CHECK clause (raises ProgrammingError / check violation).
    """
    row = _build_row(model, tenant_b.id, extra)
    with pytest.raises(Exception) as exc_info:
        await tenant_a_session.execute(insert(model).values(**row))
        # Force the error to surface if it's deferred.
        await tenant_a_session.flush()

    error_str = str(exc_info.value).lower()
    assert any(
        kw in error_str
        for kw in ("policy", "check", "violat", "new row", "rls")
    ), (
        f"{model.__tablename__}: Expected RLS WITH CHECK violation but got: "
        f"{exc_info.value}"
    )


# ── 5. Fail-closed: query without tenant_id returns nothing ───────────────────

@pytest.mark.parametrize("model,extra", TENANT_TABLES)
async def test_query_without_tenant_id_returns_no_rows(
    model: type,
    extra: dict,
    raw_db: AsyncSession,
    tenant_a: Tenant,
) -> None:
    """
    A session that never calls set_config('app.tenant_id', ...) must see zero
    rows — the system fails closed, not open.

    We use a fresh session connected as app_tenant (which has NOBYPASSRLS).
    We deliberately do NOT set the tenant_id config.
    """
    # Insert a row for tenant A via raw (no-RLS) session so there's data to miss.
    row = _build_row(model, tenant_a.id, extra)
    await raw_db.execute(insert(model).values(**row))

    from app.db.engine import SessionLocal

    async with SessionLocal() as session, session.begin():
        # No set_config call here — tenant context is absent.
        result = await session.execute(select(model))
        rows = result.scalars().all()

    assert rows == [], (
        f"{model.__tablename__}: Expected 0 rows with no tenant_id set "
        f"but got {len(rows)} row(s). RLS is NOT fail-closed!"
    )

