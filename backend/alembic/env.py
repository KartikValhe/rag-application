"""
alembic/env.py
──────────────
Alembic environment configured for async SQLAlchemy + the `app` schema.

Uses MIGRATION_DATABASE_URL (direct connection) so it is never routed
through a transaction-mode pooler.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Import all models so Alembic can build an accurate diff.
from app.core.config import settings
from app.db.models import Base  # noqa: F401 — registers all models

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection)."""
    context.configure(
        url=settings.MIGRATION_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Emit SQL that creates/uses the app schema.
        include_schemas=True,
        version_table_schema="app",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="app",
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an engine and run migrations inside a connection."""
    engine = create_async_engine(
        settings.MIGRATION_DATABASE_URL,
        connect_args={"statement_cache_size": 0},
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async engine)."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

