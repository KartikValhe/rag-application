"""
tests/conftest.py
─────────────────
Shared pytest fixtures.

Requires a running Postgres and Redis (provided by docker-compose in CI
and by `docker compose up` locally).  Connection details come from
TEST_DATABASE_URL and TEST_REDIS_URL env vars (fall back to .env).
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set test env vars before importing app modules.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://app_tenant:tenant_password@localhost:5433/ragplatform",
)
os.environ.setdefault(
    "PLATFORM_DATABASE_URL",
    "postgresql+asyncpg://app_platform:platform_password@localhost:5433/ragplatform",
)
os.environ.setdefault(
    "MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/ragplatform",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")  # DB 1 for tests
os.environ.setdefault("SECRET_KEY", "a" * 64)
os.environ.setdefault("PLATFORM_SECRET_KEY", "b" * 64)
os.environ.setdefault("ENVIRONMENT", "development")

from app.core.security import hash_password
from app.db.engine import SessionLocal
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import app
from app.services.rbac import seed_tenant_system_roles

# ── Database setup ────────────────────────────────────────────────────────────

# A direct-connection engine for test setup (bypasses RLS so fixtures can
# insert rows in any tenant).
_test_engine = create_async_engine(
    os.environ["MIGRATION_DATABASE_URL"],
    connect_args={"statement_cache_size": 0},
)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database() -> AsyncGenerator[None, None]:
    """Ensure database schema is ready for tests."""
    yield
    await _test_engine.dispose()


@pytest_asyncio.fixture
async def raw_db() -> AsyncGenerator[AsyncSession, None]:
    """
    A raw (no-RLS) DB session for test setup fixtures.
    Uses the migration/direct connection — bypasses RLS so fixtures can
    insert rows into any tenant.
    """
    async with _TestSession() as session:
        yield session
        await session.commit()


# ── Tenant fixtures ───────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def tenant_a(raw_db: AsyncSession) -> Tenant:
    t = Tenant(slug=f"tenant-a-{uuid.uuid4().hex[:6]}", name="Tenant A", kind="school")
    raw_db.add(t)
    await raw_db.commit()
    await seed_tenant_system_roles(t.id, raw_db)
    await raw_db.commit()
    return t


@pytest_asyncio.fixture
async def tenant_b(raw_db: AsyncSession) -> Tenant:
    t = Tenant(slug=f"tenant-b-{uuid.uuid4().hex[:6]}", name="Tenant B", kind="college")
    raw_db.add(t)
    await raw_db.commit()
    await seed_tenant_system_roles(t.id, raw_db)
    await raw_db.commit()
    return t


# ── User fixtures ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def user_a(raw_db: AsyncSession, tenant_a: Tenant) -> User:
    u = User(
        tenant_id=tenant_a.id,
        email=f"user-a-{uuid.uuid4().hex[:6]}@example.com",
        full_name="User A",
        password_hash=hash_password("Password123!"),
        status="active",
        is_admin=False,
    )
    raw_db.add(u)
    await raw_db.commit()
    return u


@pytest_asyncio.fixture
async def admin_a(raw_db: AsyncSession, tenant_a: Tenant) -> User:
    u = User(
        tenant_id=tenant_a.id,
        email=f"admin-a-{uuid.uuid4().hex[:6]}@example.com",
        full_name="Admin A",
        password_hash=hash_password("Password123!"),
        status="active",
        is_admin=True,
    )
    raw_db.add(u)
    await raw_db.flush()
    admin_role_res = await raw_db.execute(
        select(Role.id).where(Role.tenant_id == tenant_a.id, Role.name == "tenant_admin")
    )
    admin_role_id = admin_role_res.scalar_one_or_none()
    if admin_role_id:
        raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=u.id, role_id=admin_role_id))
    await raw_db.commit()
    return u


@pytest_asyncio.fixture
async def user_b(raw_db: AsyncSession, tenant_b: Tenant) -> User:
    u = User(
        tenant_id=tenant_b.id,
        email=f"user-b-{uuid.uuid4().hex[:6]}@example.com",
        full_name="User B",
        password_hash=hash_password("Password123!"),
        status="active",
        is_admin=False,
    )
    raw_db.add(u)
    await raw_db.commit()
    return u


# ── HTTP client fixtures ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async test client for the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def tenant_a_session(tenant_a: Tenant) -> AsyncGenerator[AsyncSession, None]:
    """
    A DB session locked to tenant A via SET LOCAL.
    Use this to verify that RLS is working correctly.
    """
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_a.id)},
        )
        yield session


@pytest_asyncio.fixture
async def tenant_b_session(tenant_b: Tenant) -> AsyncGenerator[AsyncSession, None]:
    """A DB session locked to tenant B."""
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_b.id)},
        )
        yield session

