"""
backend/app/scripts/seed.py
───────────────────────────
Seeds initial demo data for local development:
  1. Platform Superadmin: superadmin@platform.local / SuperAdmin123!
  2. Demo Tenant: Acme University (slug: acme)
  3. Tenant Admin: admin@acme.edu / Admin123!
  4. Tenant Student: student@acme.edu / Student123!

Usage:
  python -m app.scripts.seed
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.models.tenant import PlatformAdmin, Tenant
from app.db.models.user import User


async def seed() -> None:
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

        # 2. Tenants (acme and default for local convenience)
        tenants_data = [
            ("acme", "Acme University", "college", "open"),
            ("default", "Default Organization", "college", "open"),
        ]

        # For seamless local development, seed both admin and student emails into both tenants
        admin_emails = ["admin@acme.edu", "admin@default.edu"]
        student_emails = ["student@acme.edu", "student@default.edu"]

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

            # Tenant Admins
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
                    print(f"[OK] Created Tenant Admin: {admin_email} on '{slug}' (Password: Admin123!)")
                else:
                    t_admin.password_hash = hash_password("Admin123!")
                    print(f"[OK] Tenant Admin {admin_email} on '{slug}' exists (password reset to Admin123!)")

            # Tenant Students
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
                    print(f"[OK] Created Student: {student_email} on '{slug}' (Password: Student123!)")
                else:
                    student.password_hash = hash_password("Student123!")
                    print(f"[OK] Student {student_email} on '{slug}' exists (password reset to Student123!)")

    await engine.dispose()
    print("\nSeed completed successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
