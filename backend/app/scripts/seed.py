"""
backend/app/scripts/seed.py
───────────────────────────
Seeds initial demo data for local development:
  1. Platform Superadmin: superadmin@platform.local / SuperAdmin123!
  2. Demo Tenants: acme and default
  3. System roles (tenant_admin, teacher, student) and permission mappings
  4. Users:
     - Tenant Admins: admin@acme.edu, admin@default.edu (Admin123!)
     - Teachers: teacher@acme.edu, teacher@default.edu (Teacher123!)
     - Students: student@acme.edu, student@default.edu (Student123!)
  5. Academic structure: Classes, Subjects, Enrollments, and Teacher Assignments

Usage:
  python -m app.scripts.seed
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Permission, Role, RolePermission, UserRole
from app.db.models.tenant import PlatformAdmin, Tenant
from app.db.models.user import User

ALL_PERMISSIONS = [
    ("user:manage", "User Management", "Invite, deactivate, and manage tenant users"),
    ("role:manage", "Role Management", "Create, update, and assign custom roles"),
    ("class:manage", "Academic Management", "Create and manage classes and cohorts"),
    ("subject:manage", "Academic Management", "Create and manage subjects"),
    ("enrollment:manage", "Academic Management", "Enroll and unenroll students in classes"),
    ("assignment:manage", "Academic Management", "Assign and remove teachers from class subjects"),
    ("document:upload", "Content Management", "Upload course and syllabus documents"),
    ("document:delete", "Content Management", "Delete syllabus documents"),
    ("timetable:publish", "Timetable Management", "Create and publish class timetables"),
    ("chat:use", "Student AI Chat", "Chat with scoped RAG syllabus assistant"),
    ("analytics:view", "Reporting", "View system and engagement analytics"),
    ("branding:manage", "Organization", "Manage white-label theme and branding"),
    ("audit:view", "Security", "View tenant audit log history"),
]

SYSTEM_ROLES = {
    "tenant_admin": {
        "description": "Full administrative access to manage the organization",
        "permissions": [p[0] for p in ALL_PERMISSIONS],
    },
    "teacher": {
        "description": "Teacher with access to assigned classes and subjects",
        "permissions": [
            "class:manage",
            "subject:manage",
            "document:upload",
            "document:delete",
            "timetable:publish",
            "chat:use",
        ],
    },
    "student": {
        "description": "Student learner with access to enrolled classes and chat assistant",
        "permissions": [
            "chat:use",
        ],
    },
}


async def seed() -> None:
    if settings.ENVIRONMENT.lower() != "development":
        raise RuntimeError(
            f"Seed script refused to run: ENVIRONMENT is '{settings.ENVIRONMENT}', but must be 'development'."
        )
    print("Connecting to database...")
    engine = create_async_engine(
        settings.MIGRATION_DATABASE_URL,
        connect_args={"statement_cache_size": 0},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session, session.begin():
        # 1. Superadmin accounts (both .local and .com)
        for sa_email in ("superadmin@platform.local", "superadmin@platform.com"):
            res = await session.execute(
                select(PlatformAdmin).where(PlatformAdmin.email == sa_email)
            )
            admin = res.scalar_one_or_none()
            if not admin:
                admin = PlatformAdmin(
                    email=sa_email,
                    full_name="Super Administrator",
                    password_hash=hash_password("SuperAdmin123!"),
                    is_active=True,
                )
                session.add(admin)
                print(f"[OK] Created Superadmin: {sa_email} (Password: SuperAdmin123!)")
            else:
                admin.password_hash = hash_password("SuperAdmin123!")
                print(f"[OK] Superadmin {sa_email} exists (password reset to SuperAdmin123!)")

        # 2. Permissions catalog (platform-wide)
        for perm_id, category, description in ALL_PERMISSIONS:
            res_p = await session.execute(select(Permission).where(Permission.id == perm_id))
            if not res_p.scalar_one_or_none():
                session.add(Permission(id=perm_id, category=category, description=description))
        await session.flush()
        print("[OK] Permissions catalog verified")

        # 3. Tenants (acme and default)
        tenants_data = [
            ("acme", "Acme University", "college", "open"),
            ("default", "Default Organization", "college", "open"),
        ]

        for slug, name, kind, reg_mode in tenants_data:
            res_t = await session.execute(select(Tenant).where(Tenant.slug == slug))
            tenant = res_t.scalar_one_or_none()
            if not tenant:
                tenant = Tenant(
                    slug=slug,
                    name=name,
                    kind=kind,
                    registration_mode=reg_mode,
                    status="active",
                )
                session.add(tenant)
                await session.flush()
                print(f"[OK] Created Tenant: {tenant.name} (Slug: {tenant.slug})")
            else:
                print(f"[OK] Tenant {tenant.name} ({tenant.slug}) already exists")

            # 4. System Roles for Tenant
            roles_by_name: dict[str, Role] = {}
            for role_name, role_def in SYSTEM_ROLES.items():
                res_r = await session.execute(
                    select(Role).where(Role.tenant_id == tenant.id, Role.name == role_name)
                )
                role = res_r.scalar_one_or_none()
                if not role:
                    role = Role(
                        tenant_id=tenant.id,
                        name=role_name,
                        description=role_def["description"],
                        is_system=True,
                    )
                    session.add(role)
                    await session.flush()
                    print(f"[OK] Created system role '{role_name}' for tenant '{slug}'")
                roles_by_name[role_name] = role

                # Sync role permissions
                for perm_id in role_def["permissions"]:
                    res_rp = await session.execute(
                        select(RolePermission).where(
                            RolePermission.tenant_id == tenant.id,
                            RolePermission.role_id == role.id,
                            RolePermission.permission_id == perm_id,
                        )
                    )
                    if not res_rp.scalar_one_or_none():
                        session.add(
                            RolePermission(
                                tenant_id=tenant.id,
                                role_id=role.id,
                                permission_id=perm_id,
                            )
                        )

            # 5. Seed Users & Assign Roles
            # Admin users
            admin_emails = ["admin@acme.edu", "admin@default.edu"]
            for admin_email in admin_emails:
                res_u = await session.execute(
                    select(User).where(User.tenant_id == tenant.id, User.email == admin_email)
                )
                t_admin = res_u.scalar_one_or_none()
                if not t_admin:
                    t_admin = User(
                        tenant_id=tenant.id,
                        email=admin_email,
                        full_name=f"Alice Admin ({slug})",
                        password_hash=hash_password("Admin123!"),
                        is_admin=True,
                        status="active",
                    )
                    session.add(t_admin)
                    await session.flush()
                    print(f"[OK] Created Tenant Admin: {admin_email} on '{slug}'")
                else:
                    t_admin.password_hash = hash_password("Admin123!")
                    t_admin.is_admin = True

                # UserRole assignment
                res_ur = await session.execute(
                    select(UserRole).where(
                        UserRole.tenant_id == tenant.id,
                        UserRole.user_id == t_admin.id,
                        UserRole.role_id == roles_by_name["tenant_admin"].id,
                    )
                )
                if not res_ur.scalar_one_or_none():
                    session.add(
                        UserRole(
                            tenant_id=tenant.id,
                            user_id=t_admin.id,
                            role_id=roles_by_name["tenant_admin"].id,
                        )
                    )

            # Teacher users
            teacher_emails = ["teacher@acme.edu", "teacher@default.edu"]
            teachers: list[User] = []
            for teacher_email in teacher_emails:
                res_tch = await session.execute(
                    select(User).where(User.tenant_id == tenant.id, User.email == teacher_email)
                )
                teacher = res_tch.scalar_one_or_none()
                if not teacher:
                    teacher = User(
                        tenant_id=tenant.id,
                        email=teacher_email,
                        full_name=f"Prof. Smith ({slug})",
                        password_hash=hash_password("Teacher123!"),
                        is_admin=False,
                        status="active",
                    )
                    session.add(teacher)
                    await session.flush()
                    print(f"[OK] Created Teacher: {teacher_email} on '{slug}' (Password: Teacher123!)")
                else:
                    teacher.password_hash = hash_password("Teacher123!")
                teachers.append(teacher)

                res_ur = await session.execute(
                    select(UserRole).where(
                        UserRole.tenant_id == tenant.id,
                        UserRole.user_id == teacher.id,
                        UserRole.role_id == roles_by_name["teacher"].id,
                    )
                )
                if not res_ur.scalar_one_or_none():
                    session.add(
                        UserRole(
                            tenant_id=tenant.id,
                            user_id=teacher.id,
                            role_id=roles_by_name["teacher"].id,
                        )
                    )

            # Student users
            student_emails = ["student@acme.edu", "student@default.edu"]
            students: list[User] = []
            for student_email in student_emails:
                res_s = await session.execute(
                    select(User).where(User.tenant_id == tenant.id, User.email == student_email)
                )
                student = res_s.scalar_one_or_none()
                if not student:
                    student = User(
                        tenant_id=tenant.id,
                        email=student_email,
                        full_name=f"Bob Student ({slug})",
                        password_hash=hash_password("Student123!"),
                        is_admin=False,
                        status="active",
                    )
                    session.add(student)
                    await session.flush()
                    print(f"[OK] Created Student: {student_email} on '{slug}' (Password: Student123!)")
                else:
                    student.password_hash = hash_password("Student123!")
                students.append(student)

                res_ur = await session.execute(
                    select(UserRole).where(
                        UserRole.tenant_id == tenant.id,
                        UserRole.user_id == student.id,
                        UserRole.role_id == roles_by_name["student"].id,
                    )
                )
                if not res_ur.scalar_one_or_none():
                    session.add(
                        UserRole(
                            tenant_id=tenant.id,
                            user_id=student.id,
                            role_id=roles_by_name["student"].id,
                        )
                    )

            # 6. Academic Structure: Classes & Subjects
            classes_to_seed = [
                ("Class 10-A", "section", "Morning batch section A"),
                ("Class 10-B", "section", "Morning batch section B"),
            ]
            classes_by_name: dict[str, Class] = {}
            for c_name, c_kind, c_desc in classes_to_seed:
                res_c = await session.execute(
                    select(Class).where(Class.tenant_id == tenant.id, Class.name == c_name)
                )
                c_obj = res_c.scalar_one_or_none()
                if not c_obj:
                    c_obj = Class(tenant_id=tenant.id, name=c_name, kind=c_kind, description=c_desc)
                    session.add(c_obj)
                    await session.flush()
                classes_by_name[c_name] = c_obj

            subjects_to_seed = [
                ("Mathematics", "MATH101", "Calculus and Algebra"),
                ("Physics", "PHYS101", "Mechanics and Waves"),
            ]
            subjects_by_name: dict[str, Subject] = {}
            for s_name, s_code, s_desc in subjects_to_seed:
                res_sb = await session.execute(
                    select(Subject).where(Subject.tenant_id == tenant.id, Subject.name == s_name)
                )
                sb_obj = res_sb.scalar_one_or_none()
                if not sb_obj:
                    sb_obj = Subject(tenant_id=tenant.id, name=s_name, code=s_code, description=s_desc)
                    session.add(sb_obj)
                    await session.flush()
                subjects_by_name[s_name] = sb_obj

            # Link ClassSubjects (Class 10-A has Math & Physics; Class 10-B has Math)
            class_subject_links = [
                ("Class 10-A", "Mathematics"),
                ("Class 10-A", "Physics"),
                ("Class 10-B", "Mathematics"),
            ]
            for c_n, s_n in class_subject_links:
                c_id = classes_by_name[c_n].id
                s_id = subjects_by_name[s_n].id
                res_cs = await session.execute(
                    select(ClassSubject).where(
                        ClassSubject.tenant_id == tenant.id,
                        ClassSubject.class_id == c_id,
                        ClassSubject.subject_id == s_id,
                    )
                )
                if not res_cs.scalar_one_or_none():
                    session.add(ClassSubject(tenant_id=tenant.id, class_id=c_id, subject_id=s_id))
            await session.flush()

            # Enroll students in Class 10-A
            for std in students:
                c_10a_id = classes_by_name["Class 10-A"].id
                res_enr = await session.execute(
                    select(Enrollment).where(
                        Enrollment.tenant_id == tenant.id,
                        Enrollment.user_id == std.id,
                        Enrollment.class_id == c_10a_id,
                    )
                )
                if not res_enr.scalar_one_or_none():
                    session.add(Enrollment(tenant_id=tenant.id, user_id=std.id, class_id=c_10a_id))

            # Assign teacher to (Class 10-A, Mathematics)
            for tch in teachers:
                c_10a_id = classes_by_name["Class 10-A"].id
                math_id = subjects_by_name["Mathematics"].id
                res_ta = await session.execute(
                    select(TeacherAssignment).where(
                        TeacherAssignment.tenant_id == tenant.id,
                        TeacherAssignment.user_id == tch.id,
                        TeacherAssignment.class_id == c_10a_id,
                        TeacherAssignment.subject_id == math_id,
                    )
                )
                if not res_ta.scalar_one_or_none():
                    session.add(
                        TeacherAssignment(
                            tenant_id=tenant.id,
                            user_id=tch.id,
                            class_id=c_10a_id,
                            subject_id=math_id,
                        )
                    )

    await engine.dispose()
    print("\nPhase 2 seed completed successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
