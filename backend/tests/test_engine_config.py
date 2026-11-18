"""
tests/test_engine_config.py
───────────────────────────
Guard: the PRODUCTION engines keep asyncpg's statement cache OFF (statement_cache_size=0).

The API talks to Postgres through a transaction-mode pooler (Supavisor / pgBouncer), where a prepared
statement can land on a different server connection and fail with "prepared statement does not exist".
The test fixtures use the default cache for speed (see tests/conftest.py); this file makes sure nobody
"harmonises" the app engines with them by accident. It looks at the real asyncpg connection, not at the
source, so it also catches a changed `connect_args`.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.engine import engine, migration_engine, platform_engine

pytestmark = pytest.mark.asyncio


async def _stmt_cache_size(eng: AsyncEngine) -> int:
    async with eng.connect() as conn:
        raw = await conn.get_raw_connection()
        driver_conn = raw.driver_connection
        assert driver_conn is not None
        return int(driver_conn._config.statement_cache_size)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "app_engine", [engine, platform_engine, migration_engine], ids=["api", "platform", "migration"]
)
async def test_app_engines_keep_the_statement_cache_off(app_engine: AsyncEngine) -> None:
    assert await _stmt_cache_size(app_engine) == 0


async def test_the_probe_can_tell_the_difference() -> None:
    """Meta-test: an engine WITHOUT the setting reports a non-zero cache, so the check above is real."""
    from app.core.config import settings

    plain = create_async_engine(settings.MIGRATION_DATABASE_URL)
    try:
        assert await _stmt_cache_size(plain) > 0
    finally:
        await plain.dispose()
