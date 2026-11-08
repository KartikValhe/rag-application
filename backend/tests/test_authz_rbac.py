"""
tests/test_authz_rbac.py
────────────────────────
Phase 2 Security & Policy Layer Tests:
  1. Default-deny: Every single route must declare an explicit permission.
  2. Route x Role permission matrix (admin, teacher, student, unauthenticated).
  3. Scope verification: teacher class/subject assignment and student enrollment boundaries.
  4. Timing & enumeration defense: Generic 404 for 'not found' and 'not yours'.
  5. Privilege escalation defenses:
     - Cannot grant permissions caller does not possess.
     - System roles are immutable.
     - Last tenant admin cannot be removed or deactivated.
  6. Audit log sanitization & database least-privilege verification:
     - Passwords/tokens redacted.
     - app_tenant has NOBYPASSRLS.
     - app_tenant gets permission denied on UPDATE/DELETE against audit_log.
"""

import uuid
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Scope, ScopeType, get_route_declared_permission, require
from app.core.security import create_access_token
from app.db.engine import SessionLocal
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import app
from app.services.audit import record_audit_event
from app.services.rbac import (
    assign_user_role,
    create_custom_role,
    deactivate_user_safely,
    delete_custom_role,
    remove_user_role,
    update_custom_role,
)

pytestmark = pytest.mark.asyncio


# ── 1. Default-Deny: Every route declares a permission ────────────────────────

async def test_default_deny_all_routes_declare_permission() -> None:
    """
    Every route registered on the FastAPI application MUST declare an explicit permission.
    If any route is missing a permission declaration, CI fails immediately.
    """
    def extract_api_routes(router: Any) -> list[Any]:
        routes = []
        for r in router.routes:
            if hasattr(r, "original_router"):
                routes.extend(extract_api_routes(r.original_router))
            elif hasattr(r, "routes"):
                routes.extend(extract_api_routes(r))
            elif hasattr(r, "endpoint"):
                routes.append(r)
        return routes

    all_routes = extract_api_routes(app.router)
    missing = []
    docs_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    for r in all_routes:
        path = getattr(r, "path", "")
        if path in docs_paths:
            continue
        perm = get_route_declared_permission(r)
        if not perm:
            missing.append(f"{getattr(r, 'methods', None)} {path}")

    assert not missing, f"Default-Deny failure! Routes without declared permission: {missing}"


# ── 2. Route x Role Permission Matrix ─────────────────────────────────────────

async def test_route_role_matrix(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    user_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Assert route accessibility across roles:
      - Admin (has user:manage) -> 200
      - Teacher (lacks user:manage) -> 403
      - Student (lacks user:manage) -> 403
      - Unauthenticated -> 401
    """
    # 1. Unauthenticated request -> 401
    unauth_resp = await client.get("/api/v1/admin/users")
    assert unauth_resp.status_code == 401

    # 2. Student (user_a, is_admin=False) -> 403
    student_token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(user_a.id), is_admin=False
    )
    student_resp = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {student_token}", "X-Tenant-Slug": tenant_a.slug},
    )
    assert student_resp.status_code == 403
    assert student_resp.json()["detail"]["code"] == "permission_denied"

    # 3. Teacher user with 'teacher' role only -> 403
    teacher_user = User(
        tenant_id=tenant_a.id,
        email=f"matrix-teacher-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Teacher User",
        is_admin=False,
        status="active",
    )
    raw_db.add(teacher_user)
    await raw_db.flush()

    teacher_role = (
        await raw_db.execute(
            Role.__table__.select().where(
                Role.tenant_id == tenant_a.id, Role.name == "teacher"
            )
        )
    ).mappings().one()

    raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=teacher_user.id, role_id=teacher_role["id"]))
    await raw_db.commit()

    teacher_token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(teacher_user.id), is_admin=False
    )
    teacher_resp = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {teacher_token}", "X-Tenant-Slug": tenant_a.slug},
    )
    assert teacher_resp.status_code == 403

    # 4. Tenant Admin -> 200
    admin_token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True
    )
    admin_resp = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}", "X-Tenant-Slug": tenant_a.slug},
    )
    assert admin_resp.status_code == 200


# ── 3. Scope & Cross-Class Access Attempts ─────────────────────────────────────

async def test_scope_and_cross_class_defense(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    tenant_b: Tenant,
) -> None:
    """
    Verify scope boundaries:
      - Teacher assigned to (Class 10-A, Math) can act on that pair.
      - Teacher acting on Class 10-B or Physics receives generic 404 (not found / not yours).
      - Student enrolled in Class 10-A can act on Class 10-A, but gets generic 404 on Class 10-B.
      - Cross-tenant class ID attempt returns generic 404.
    """
    from fastapi import Request
    from app.core.authz import verify_scope

    # Setup Tenant A classes and subjects
    c_10a = Class(tenant_id=tenant_a.id, name=f"10-A-{uuid.uuid4().hex[:6]}")
    c_10b = Class(tenant_id=tenant_a.id, name=f"10-B-{uuid.uuid4().hex[:6]}")
    sub_math = Subject(tenant_id=tenant_a.id, name=f"Math-{uuid.uuid4().hex[:6]}")
    sub_phys = Subject(tenant_id=tenant_a.id, name=f"Phys-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c_10a, c_10b, sub_math, sub_phys])
    await raw_db.flush()

    cs_math = ClassSubject(tenant_id=tenant_a.id, class_id=c_10a.id, subject_id=sub_math.id)
    cs_phys = ClassSubject(tenant_id=tenant_a.id, class_id=c_10a.id, subject_id=sub_phys.id)
    raw_db.add_all([cs_math, cs_phys])
    await raw_db.flush()

    # Teacher setup in Tenant A
    teacher = User(
        tenant_id=tenant_a.id,
        email=f"tch-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Teacher",
        is_admin=False,
        status="active",
    )
    raw_db.add(teacher)
    await raw_db.flush()

    r_teacher = (
        await raw_db.execute(
            Role.__table__.select().where(Role.tenant_id == tenant_a.id, Role.name == "teacher")
        )
    ).mappings().one()
    raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=teacher.id, role_id=r_teacher["id"]))

    # Assign teacher ONLY to (c_10a, sub_math)
    ta = TeacherAssignment(
        tenant_id=tenant_a.id, user_id=teacher.id, class_id=c_10a.id, subject_id=sub_math.id
    )
    raw_db.add(ta)

    # Student setup in Tenant A, enrolled ONLY in c_10a
    student = User(
        tenant_id=tenant_a.id,
        email=f"std-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Student",
        is_admin=False,
        status="active",
    )
    raw_db.add(student)
    await raw_db.flush()

    r_student = (
        await raw_db.execute(
            Role.__table__.select().where(Role.tenant_id == tenant_a.id, Role.name == "student")
        )
    ).mappings().one()
    raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=student.id, role_id=r_student["id"]))
    raw_db.add(Enrollment(tenant_id=tenant_a.id, user_id=student.id, class_id=c_10a.id))

    # Tenant B class (cross-tenant)
    c_tenant_b = Class(tenant_id=tenant_b.id, name=f"TB-Class-{uuid.uuid4().hex[:6]}")
    raw_db.add(c_tenant_b)
    await raw_db.commit()

    # Test Helper: Fake Request with path_params
    def make_req(class_id: uuid.UUID | None = None, subject_id: uuid.UUID | None = None) -> Request:
        scope = {
            "type": "http",
            "path_params": {
                "class_id": str(class_id) if class_id else None,
                "subject_id": str(subject_id) if subject_id else None,
            },
            "query_string": b"",
            "headers": [],
        }
        return Request(scope)

    # 1. Teacher on assigned class -> Allowed (no exception)
    await verify_scope(
        teacher, Scope(ScopeType.CLASS), make_req(class_id=c_10a.id), raw_db
    )

    # 2. Teacher on assigned (class, subject) -> Allowed
    await verify_scope(
        teacher,
        Scope(ScopeType.CLASS_SUBJECT),
        make_req(class_id=c_10a.id, subject_id=sub_math.id),
        raw_db,
    )

    # 3. Teacher on UNASSIGNED subject (Phys) in assigned class -> 404 Generic
    with pytest.raises(Exception) as exc_info:
        await verify_scope(
            teacher,
            Scope(ScopeType.CLASS_SUBJECT),
            make_req(class_id=c_10a.id, subject_id=sub_phys.id),
            raw_db,
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["message"] == "Resource not found."

    # 4. Teacher on UNASSIGNED class (c_10b) -> 404 Generic
    with pytest.raises(Exception) as exc_info:
        await verify_scope(
            teacher, Scope(ScopeType.CLASS), make_req(class_id=c_10b.id), raw_db
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["message"] == "Resource not found."

    # 5. Student on enrolled class (c_10a) -> Allowed
    await verify_scope(
        student, Scope(ScopeType.CLASS), make_req(class_id=c_10a.id), raw_db
    )

    # 6. Student on UNENROLLED class (c_10b) -> 404 Generic
    with pytest.raises(Exception) as exc_info:
        await verify_scope(
            student, Scope(ScopeType.CLASS), make_req(class_id=c_10b.id), raw_db
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["message"] == "Resource not found."

    # 7. Cross-tenant attempt (Tenant A teacher querying Tenant B class) -> 404 Generic
    with pytest.raises(Exception) as exc_info:
        await verify_scope(
            teacher, Scope(ScopeType.CLASS), make_req(class_id=c_tenant_b.id), raw_db
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["message"] == "Resource not found."


# ── 4. Privilege Escalation Prevention ────────────────────────────────────────

async def test_privilege_escalation_defenses(
    raw_db: AsyncSession,
    tenant_a: Tenant,
    admin_a: User,
) -> None:
    """
    Verify:
      - Nobody can grant a permission they don't have.
      - System roles cannot be modified or deleted.
      - Last tenant admin cannot be removed or deactivated.
    """
    # 1. Non-admin teacher user tries to create a role with 'user:manage'
    teacher = User(
        tenant_id=tenant_a.id,
        email=f"esc-tch-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Teacher Esc",
        is_admin=False,
        status="active",
    )
    raw_db.add(teacher)
    await raw_db.flush()

    r_teacher = (
        await raw_db.execute(
            Role.__table__.select().where(Role.tenant_id == tenant_a.id, Role.name == "teacher")
        )
    ).mappings().one()
    raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=teacher.id, role_id=r_teacher["id"]))
    await raw_db.commit()

    # Teacher tries to create custom role with 'user:manage' -> 403 Forbidden
    with pytest.raises(Exception) as exc_info:
        await create_custom_role(
            tenant_id=tenant_a.id,
            caller=teacher,
            name="Sneaky Admin Role",
            permission_ids=["user:manage"],
            db=raw_db,
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "privilege_escalation"

    # 2. System roles cannot be modified or deleted
    r_admin_role = (
        await raw_db.execute(
            Role.__table__.select().where(Role.tenant_id == tenant_a.id, Role.name == "tenant_admin")
        )
    ).mappings().one()

    # Attempt to update system role -> 403
    with pytest.raises(Exception) as exc_info:
        await update_custom_role(
            tenant_id=tenant_a.id,
            caller=admin_a,
            role_id=r_admin_role["id"],
            name="Renamed Admin",
            db=raw_db,
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "system_role_immutable"

    # Attempt to delete system role -> 403
    with pytest.raises(Exception) as exc_info:
        await delete_custom_role(
            tenant_id=tenant_a.id,
            caller=admin_a,
            role_id=r_admin_role["id"],
            db=raw_db,
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "system_role_immutable"

    # 3. Last tenant admin cannot be deactivated
    # Deactivate another admin first if present, to isolate down to 1 admin
    other_admins = (
        await raw_db.execute(
            User.__table__.select().where(
                User.tenant_id == tenant_a.id, User.is_admin.is_(True), User.id != admin_a.id
            )
        )
    ).mappings().all()
    for oa in other_admins:
        await raw_db.execute(
            User.__table__.update().where(User.id == oa["id"]).values(status="deactivated")
        )
    await raw_db.commit()

    # Now admin_a is the ONLY active admin in tenant_a. Attempting deactivation fails:
    dummy_caller = User(
        tenant_id=tenant_a.id,
        email="caller@example.com",
        full_name="Caller",
        is_admin=True,
        status="active",
    )
    with pytest.raises(Exception) as exc_info:
        await deactivate_user_safely(
            tenant_id=tenant_a.id,
            caller=dummy_caller,
            target_user_id=admin_a.id,
            db=raw_db,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "last_admin_protection"

    # Attempting to remove tenant_admin role from admin_a fails:
    with pytest.raises(Exception) as exc_info:
        await remove_user_role(
            tenant_id=tenant_a.id,
            caller=dummy_caller,
            target_user_id=admin_a.id,
            role_id=r_admin_role["id"],
            db=raw_db,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "last_admin_protection"


# ── 5. Audit Log Security & Database Permissions ──────────────────────────────

async def test_audit_log_sanitization(raw_db: AsyncSession, tenant_a: Tenant) -> None:
    """Passwords, tokens, and secrets must be redacted before saving to audit_log."""
    entry = await record_audit_event(
        tenant_id=tenant_a.id,
        action="test:auth",
        target_type="user",
        details={
            "email": "user@example.com",
            "password": "SuperSecretPassword123!",
            "token": "sensitive-jwt-token-xyz",
            "nested": {"refresh_token": "secret-refresh-cookie"},
        },
        db=raw_db,
    )
    assert entry.details["email"] == "user@example.com"
    assert entry.details["password"] == "[REDACTED]"
    assert entry.details["token"] == "[REDACTED]"
    assert entry.details["nested"]["refresh_token"] == "[REDACTED]"


async def test_app_tenant_least_privilege_and_no_bypassrls() -> None:
    """
    1. app_tenant role must have rolbypassrls = False.
    2. app_tenant gets 'permission denied' on UPDATE and DELETE against audit_log.
    """
    # 1. Check rolbypassrls for app_tenant
    async with SessionLocal() as session:
        res = await session.execute(
            text("SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname = 'app_tenant'")
        )
        role_row = res.one()
        assert role_row[0] == "app_tenant"
        assert role_row[1] is False, "CRITICAL SECURITY FLAW: app_tenant has BYPASSRLS=True!"

    # 2. Check UPDATE on audit_log fails with permission denied
    async with SessionLocal() as session:
        with pytest.raises(Exception) as exc_u:
            await session.execute(
                text("UPDATE app.audit_log SET action = 'tampered' WHERE true")
            )
        assert "permission denied" in str(exc_u.value).lower()

    # 3. Check DELETE on audit_log fails with permission denied
    async with SessionLocal() as session:
        with pytest.raises(Exception) as exc_d:
            await session.execute(
                text("DELETE FROM app.audit_log WHERE true")
            )
        assert "permission denied" in str(exc_d.value).lower()

