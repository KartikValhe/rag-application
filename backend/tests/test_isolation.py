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
from datetime import UTC, datetime, time

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.academic import (
    Class,
    ClassSubject,
    Enrollment,
    Subject,
    TeacherAssignment,
)
from app.db.models.audit import AuditLog
from app.db.models.auth import EmailToken, Invitation, RefreshToken
from app.db.models.branding import TenantBranding
from app.db.models.rbac import (
    Role,
    RolePermission,
    UserRole,
)
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
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
    (Role, {
        "name": lambda: f"iso-role-{uuid.uuid4().hex[:8]}",
        "description": "Isolation Role",
        "is_system": False,
    }),
    (Class, {
        "name": lambda: f"iso-class-{uuid.uuid4().hex[:8]}",
        "kind": "section",
        "description": "Isolation Class",
    }),
    (Subject, {
        "name": lambda: f"iso-subject-{uuid.uuid4().hex[:8]}",
        "code": "ISO101",
        "description": "Isolation Subject",
    }),
    (AuditLog, {
        "action": "iso:audit_test",
        "target_type": "user",
        "details": {"test": True},
    }),
    (TenantBranding, {
        "tokens": lambda: {
            "primary": "#2563eb",
            "secondary": "#475569",
            "accent": "#06b6d4",
            "radius": "md",
            "font_family": "system-ui",
        },
        "display_name": "Isolation Branding",
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

    # Attempt UPDATE from tenant A's session — should match 0 rows due to RLS,
    # or fail with permission denied if the table is append-only (like audit_log).
    pk_col = list(model.__table__.primary_key.columns)[0]
    try:
        update_result = await tenant_a_session.execute(
            update(model).where(pk_col == inserted.id).values(tenant_id=tenant_a.id)
            if hasattr(inserted, "id") else
            update(model).where(model.tenant_id == tenant_b.id).values()
        )
        assert update_result.rowcount == 0, (
            f"{model.__tablename__}: UPDATE in tenant A's context affected "
            f"{update_result.rowcount} row(s) belonging to tenant B."
        )
    except Exception as exc:
        assert any(kw in str(exc).lower() for kw in ("permission denied", "policy", "violat"))


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
    try:
        del_result = await tenant_a_session.execute(
            delete(model).where(pk_col == inserted.id)
            if hasattr(inserted, "id") else
            delete(model).where(model.tenant_id == tenant_b.id)
        )
        assert del_result.rowcount == 0, (
            f"{model.__tablename__}: DELETE in tenant A's context removed "
            f"{del_result.rowcount} row(s) belonging to tenant B."
        )
    except Exception as exc:
        assert any(kw in str(exc).lower() for kw in ("permission denied", "policy", "violat"))


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


# ── 6. Composite Junction Tables Isolation ────────────────────────────────────

async def test_academic_and_rbac_junctions_isolation(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
    tenant_b_session: AsyncSession,
    user_a: User,
    user_b: User,
) -> None:
    """
    Verify that junction tables (RolePermission, UserRole, ClassSubject, Enrollment,
    TeacherAssignment) enforce tenant isolation and RLS boundaries.
    """
    # 1. Create parents for Tenant A
    role_a = Role(tenant_id=tenant_a.id, name=f"role-a-{uuid.uuid4().hex[:6]}", is_system=False)
    class_a = Class(tenant_id=tenant_a.id, name=f"class-a-{uuid.uuid4().hex[:6]}")
    subject_a = Subject(tenant_id=tenant_a.id, name=f"sub-a-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([role_a, class_a, subject_a])
    await raw_db.flush()

    # 2. Create parents for Tenant B
    role_b = Role(tenant_id=tenant_b.id, name=f"role-b-{uuid.uuid4().hex[:6]}", is_system=False)
    class_b = Class(tenant_id=tenant_b.id, name=f"class-b-{uuid.uuid4().hex[:6]}")
    subject_b = Subject(tenant_id=tenant_b.id, name=f"sub-b-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([role_b, class_b, subject_b])
    await raw_db.flush()

    # 3. Create junctions for Tenant A
    rp_a = RolePermission(tenant_id=tenant_a.id, role_id=role_a.id, permission_id="chat:use")
    ur_a = UserRole(tenant_id=tenant_a.id, user_id=user_a.id, role_id=role_a.id)
    cs_a = ClassSubject(tenant_id=tenant_a.id, class_id=class_a.id, subject_id=subject_a.id)
    raw_db.add_all([rp_a, ur_a, cs_a])
    await raw_db.flush()

    enr_a = Enrollment(tenant_id=tenant_a.id, user_id=user_a.id, class_id=class_a.id)
    ta_a = TeacherAssignment(
        tenant_id=tenant_a.id, user_id=user_a.id, class_id=class_a.id, subject_id=subject_a.id
    )
    raw_db.add_all([enr_a, ta_a])

    # 4. Create junctions for Tenant B
    rp_b = RolePermission(tenant_id=tenant_b.id, role_id=role_b.id, permission_id="chat:use")
    ur_b = UserRole(tenant_id=tenant_b.id, user_id=user_b.id, role_id=role_b.id)
    cs_b = ClassSubject(tenant_id=tenant_b.id, class_id=class_b.id, subject_id=subject_b.id)
    raw_db.add_all([rp_b, ur_b, cs_b])
    await raw_db.flush()

    enr_b = Enrollment(tenant_id=tenant_b.id, user_id=user_b.id, class_id=class_b.id)
    ta_b = TeacherAssignment(
        tenant_id=tenant_b.id, user_id=user_b.id, class_id=class_b.id, subject_id=subject_b.id
    )
    raw_db.add_all([enr_b, ta_b])
    await raw_db.commit()

    # Query as Tenant A: must ONLY see Tenant A's junction records
    res_enr = await tenant_a_session.execute(select(Enrollment))
    enr_rows = res_enr.scalars().all()
    assert len(enr_rows) >= 1
    assert all(e.tenant_id == tenant_a.id for e in enr_rows)
    assert not any(e.tenant_id == tenant_b.id for e in enr_rows)

    res_ta = await tenant_a_session.execute(select(TeacherAssignment))
    ta_rows = res_ta.scalars().all()
    assert len(ta_rows) >= 1
    assert all(t.tenant_id == tenant_a.id for t in ta_rows)
    assert not any(t.tenant_id == tenant_b.id for t in ta_rows)

    res_cs = await tenant_a_session.execute(select(ClassSubject))
    cs_rows = res_cs.scalars().all()
    assert all(c.tenant_id == tenant_a.id for c in cs_rows)
    assert not any(c.tenant_id == tenant_b.id for c in cs_rows)

    # Cross-tenant insert attempt in Tenant A's session using Tenant B's ID must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(Enrollment).values(
                tenant_id=tenant_b.id,
                user_id=user_b.id,
                class_id=class_b.id,
            )
        )
        await tenant_a_session.flush()


async def test_timetables_and_entries_isolation(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
    tenant_a_session: AsyncSession,
    user_a: User,
    user_b: User,
) -> None:
    """
    Verify that Timetable and TimetableEntry enforce tenant isolation and RLS boundaries:
      1. SELECT as tenant A returns only tenant A's timetables and entries.
      2. INSERT with tenant_id=B while in tenant A's context fails with RLS check.
    """
    # 1. Parents for Tenant A
    class_a = Class(tenant_id=tenant_a.id, name=f"class-tt-a-{uuid.uuid4().hex[:6]}")
    subject_a = Subject(tenant_id=tenant_a.id, name=f"sub-tt-a-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([class_a, subject_a])
    await raw_db.flush()

    tt_a = Timetable(
        tenant_id=tenant_a.id,
        class_id=class_a.id,
        academic_year="2026-2027",
        term="Fall",
        status="published",
        version=1,
    )
    raw_db.add(tt_a)
    await raw_db.flush()

    entry_a = TimetableEntry(
        tenant_id=tenant_a.id,
        timetable_id=tt_a.id,
        class_id=class_a.id,
        subject_id=subject_a.id,
        teacher_id=user_a.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
        room="Room 101",
    )
    raw_db.add(entry_a)

    # 2. Parents for Tenant B
    class_b = Class(tenant_id=tenant_b.id, name=f"class-tt-b-{uuid.uuid4().hex[:6]}")
    subject_b = Subject(tenant_id=tenant_b.id, name=f"sub-tt-b-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([class_b, subject_b])
    await raw_db.flush()

    tt_b = Timetable(
        tenant_id=tenant_b.id,
        class_id=class_b.id,
        academic_year="2026-2027",
        term="Fall",
        status="published",
        version=1,
    )
    raw_db.add(tt_b)
    await raw_db.flush()

    entry_b = TimetableEntry(
        tenant_id=tenant_b.id,
        timetable_id=tt_b.id,
        class_id=class_b.id,
        subject_id=subject_b.id,
        teacher_id=user_b.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 0),
        room="Room 202",
    )
    raw_db.add(entry_b)
    await raw_db.commit()

    # 3. SELECT as Tenant A: must ONLY see Tenant A's records
    res_tt = await tenant_a_session.execute(select(Timetable))
    tt_rows = res_tt.scalars().all()
    assert len(tt_rows) >= 1
    assert all(t.tenant_id == tenant_a.id for t in tt_rows)
    assert not any(t.tenant_id == tenant_b.id for t in tt_rows)

    res_entry = await tenant_a_session.execute(select(TimetableEntry))
    entry_rows = res_entry.scalars().all()
    assert len(entry_rows) >= 1
    assert all(e.tenant_id == tenant_a.id for e in entry_rows)
    assert not any(e.tenant_id == tenant_b.id for e in entry_rows)

    # 4. Cross-tenant INSERT into Timetable inside Tenant A's session must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(Timetable).values(
                tenant_id=tenant_b.id,
                class_id=class_b.id,
                academic_year="2026-2027",
                term="Spring",
                status="draft",
                version=1,
            )
        )
        await tenant_a_session.flush()

    # 5. Cross-tenant INSERT into TimetableEntry inside Tenant A's session must fail
    with pytest.raises(Exception):  # noqa: B017
        await tenant_a_session.execute(
            insert(TimetableEntry).values(
                tenant_id=tenant_b.id,
                timetable_id=tt_b.id,
                class_id=class_b.id,
                subject_id=subject_b.id,
                day_of_week=1,
                start_time=time(11, 0),
                end_time=time(12, 0),
            )
        )
        await tenant_a_session.flush()


