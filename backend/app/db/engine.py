"""
app/db/engine.py
────────────────
SQLAlchemy async engine and session factory.

Two engines are created:
  - `engine`           — transaction-pooler URL for all API requests.
  - `migration_engine` — direct URL for Alembic; never used at runtime.

Key notes (see implementation_plan.md § 1.6):
  - statement_cache_size=0 is REQUIRED when using Supavisor / pgBouncer in
    transaction mode, because each transaction may land on a different server
    connection.  asyncpg's prepared-statement cache would cause
    "prepared statement does not exist" errors otherwise.
  - The API role (app_tenant) must NOT have BYPASSRLS; RLS is always enforced.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# ── API engine (transaction-mode pooler) ────────────────────────────────────

def _make_engine(url: str, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,  # burst headroom
        pool_pre_ping=True,      # discard stale pooled connections
        connect_args={
            # Disable asyncpg's prepared-statement cache.  This is mandatory
            # for transaction-mode connection poolers (pgBouncer / Supavisor).
            "statement_cache_size": 0,
            # Fail fast rather than hanging when the DB is unreachable.
            "command_timeout": 10,
        },
        # Keep SQLAlchemy's own query cache small to avoid stale plans.
        query_cache_size=0,
    )


engine: AsyncEngine = _make_engine(settings.DATABASE_URL)

# Session factory used by all API request handlers.
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# ── Platform engine (superadmin routes; connects as app_platform) ────────────
platform_engine: AsyncEngine = _make_engine(
    settings.PLATFORM_DATABASE_URL or settings.DATABASE_URL,
    pool_size=3,
)

PlatformSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=platform_engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# ── Migration engine (direct connection, session mode) ───────────────────────
# Only imported by alembic/env.py and worker startup scripts.

migration_engine: AsyncEngine = _make_engine(
    settings.MIGRATION_DATABASE_URL,
    pool_size=1,
)

