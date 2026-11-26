"""Prepare the dedicated test database (idempotent).

    python -m app.scripts.prepare_test_db            # check roles/extension, migrate to head
    python -m app.scripts.prepare_test_db --reset    # drop and recreate the test DB first

Always works on the `_test` database derived from the configured URLs, never the dev one.

The chat maintenance role `app_chat_maint` (migration 0013) is created here when it is missing, the same way
infra/postgres/init.sql creates it: LOGIN, NOSUPERUSER, NOBYPASSRLS and NO password (the tests reach it with SET
ROLE). Roles are cluster-wide, so this is a role in the cluster, not a change to any database; it gets grants only
from migration 0013, in the database that migration runs on.
"""

import argparse
import asyncio
import os
import subprocess
import sys

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

from app.core.test_db_guard import TestDatabaseGuardError, apply_test_database_env

REQUIRED_ROLES = ("app_worker", "app_tenant", "app_platform", "app_chat_maint")

# No password on purpose (no secret in the repo); the same statement as infra/postgres/init.sql.
CREATE_CHAT_MAINT_ROLE_SQL = "CREATE ROLE app_chat_maint LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"


def _dsn(url: str, database: str | None = None) -> str:
    u = make_url(url).set(drivername="postgresql")
    if database is not None:
        u = u.set(database=database)
    return u.render_as_string(hide_password=False)


async def _reset(migration_url: str) -> None:
    db = make_url(migration_url).database or ""
    conn = await asyncpg.connect(_dsn(migration_url, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db}"')
    finally:
        await conn.close()
    # Re-apply the extensions and schema (roles are cluster-wide and already exist).
    conn = await asyncpg.connect(_dsn(migration_url))
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA app")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA app")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA app")
        await conn.execute("GRANT USAGE ON SCHEMA app TO app_tenant, app_platform, app_worker")
    finally:
        await conn.close()


async def _ensure_chat_maint_role(migration_url: str) -> bool:
    """Create app_chat_maint if it is missing. Returns True when it was created."""
    conn = await asyncpg.connect(_dsn(migration_url))
    try:
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'app_chat_maint'"):
            return False
        await conn.execute(CREATE_CHAT_MAINT_ROLE_SQL)
        await conn.execute("ALTER ROLE app_chat_maint SET search_path = app")
        return True
    finally:
        await conn.close()


async def _check(migration_url: str) -> list[str]:
    missing: list[str] = []
    conn = await asyncpg.connect(_dsn(migration_url))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'"):
            missing.append("extension vector")
        for role in REQUIRED_ROLES:
            if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                missing.append(f"role {role}")
    finally:
        await conn.close()
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the test database first")
    args = parser.parse_args()

    env = dict(os.environ)
    try:
        apply_test_database_env(env)
    except TestDatabaseGuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    url = env["MIGRATION_DATABASE_URL"]
    print(f"Test database: {make_url(url).database} on {make_url(url).host}:{make_url(url).port}")

    try:
        if args.reset:
            asyncio.run(_reset(url))
            print("Test database dropped and recreated.")
        if asyncio.run(_ensure_chat_maint_role(url)):
            print("Role app_chat_maint created (no password).")
        missing = asyncio.run(_check(url))
    except Exception as exc:
        print(f"ERROR: cannot prepare the test database ({exc}). Is `docker compose up -d` running?", file=sys.stderr)
        return 1
    if missing:
        print(
            f"ERROR: missing {', '.join(missing)}. Run infra/postgres/init.sql (or recreate the "
            "postgres volume), then retry.",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=False)
    if result.returncode == 0:
        print("Test database is at migration head.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
