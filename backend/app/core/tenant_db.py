"""
app/core/tenant_db.py
─────────────────────
THE most important 30 lines in the codebase.

get_tenant_session / get_platform_session are FastAPI dependencies.
tenant_session_context is an async context manager for programmatic usage.

They ensure:
  1. Every database transaction runs within an explicit BEGIN / COMMIT.
  2. The tenant is locked in at transaction start with SET LOCAL — meaning the
     value exists ONLY for the life of this transaction.  When a connection
     returns to the pool the setting is gone, so a subsequent request can never
     accidentally inherit a previous tenant's context.
  3. Queries run as app_tenant (which has NOBYPASSRLS), so RLS is always active.

Usage in a route:
    @router.get("/users")
    async def list_users(db: AsyncSession = Depends(get_tenant_session)):
        ...

For superadmin routes use `get_platform_session` (no tenant_id set).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_tenant_id
from app.db.engine import PlatformSessionLocal, SessionLocal

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def tenant_session_context(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager: opens a DB session, begins a transaction,
    sets app.tenant_id with SET LOCAL (transaction-scoped), and yields the session.
    """
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        logger.debug("tenant_session_opened", tenant_id=str(tenant_id))
        try:
            yield session
        except Exception:
            logger.warning("tenant_session_rolled_back", tenant_id=str(tenant_id))
            raise


async def get_tenant_session(
    tenant_id: str = Depends(get_request_tenant_id),
) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency: yields a DB session locked to the caller's tenant.
    Depends on `get_request_tenant_id` which is populated by JWT authentication.
    """
    async with tenant_session_context(tenant_id) as session:
        yield session


async def get_platform_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Open a DB session for superadmin (platform) routes.
    Connects with the app_platform DB role.
    No tenant_id is set; RLS policies on tenant-scoped tables will
    return zero rows because current_setting('app.tenant_id', true) is NULL.
    Platform routes only touch platform-level tables (tenants, platform_admins).
    """
    async with PlatformSessionLocal() as session, session.begin():
        yield session
