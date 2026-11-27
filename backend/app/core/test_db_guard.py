"""Safety guard for the test suite: pytest may only ever touch a dedicated local test database.

Used by tests/conftest.py and app/scripts/prepare_test_db.py. Pure functions, no app settings.
"""

from collections.abc import Mapping, MutableMapping

from sqlalchemy.engine import make_url

DB_URL_VARS = (
    "DATABASE_URL",
    "PLATFORM_DATABASE_URL",
    "WORKER_DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "CHAT_MAINT_DATABASE_URL",
)

DEFAULT_URLS = {
    "DATABASE_URL": "postgresql+asyncpg://app_tenant:tenant_password@localhost:5433/ragplatform",
    "PLATFORM_DATABASE_URL": "postgresql+asyncpg://app_platform:platform_password@localhost:5433/ragplatform",
    "WORKER_DATABASE_URL": "postgresql+asyncpg://app_worker:worker_password@localhost:5433/ragplatform",
    "MIGRATION_DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5433/ragplatform",
    # The role has no password in the repo (tests reach it with SET ROLE from the superuser URL); the user name is checked.
    "CHAT_MAINT_DATABASE_URL": "postgresql+asyncpg://app_chat_maint@localhost:5433/ragplatform",
}

# Hosts a test run may talk to: loopback and the docker-compose service names.
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "postgres", "rag-postgres"})

TEST_SUFFIX = "_test"


class TestDatabaseGuardError(RuntimeError):
    """Raised when the configured URLs could point tests at a non-test database."""

    __test__ = False  # not a pytest test class


def derive_test_url(url: str) -> str:
    """Append `_test` to the database name unless it already ends with it."""
    parsed = make_url(url)
    name = parsed.database or ""
    if name.endswith(TEST_SUFFIX):
        return url
    return parsed.set(database=name + TEST_SUFFIX).render_as_string(hide_password=False)


def check_test_urls(urls: Mapping[str, str], environment: str) -> None:
    """Abort (raise) unless every URL is a local `_test` database and ENVIRONMENT is development."""
    problems: list[str] = []
    if environment.lower() != "development":
        problems.append(f"ENVIRONMENT is {environment!r}, must be 'development'")
    for var, url in urls.items():
        parsed = make_url(url)
        if not (parsed.database or "").endswith(TEST_SUFFIX):
            problems.append(f"{var}: database {parsed.database!r} does not end with '{TEST_SUFFIX}'")
        if (parsed.host or "") not in ALLOWED_HOSTS:
            problems.append(f"{var}: host {parsed.host!r} is not local (allowed: {sorted(ALLOWED_HOSTS)})")
    if problems:
        raise TestDatabaseGuardError(
            "Refusing to run tests, they must never touch dev, staging or Supabase: "
            + "; ".join(problems)
        )


def apply_test_database_env(env: MutableMapping[str, str]) -> None:
    """Rewrite the DB URLs in `env` to the test database, then run the hard guard."""
    for var in DB_URL_VARS:
        env[var] = derive_test_url(env.get(var) or DEFAULT_URLS[var])
    env.setdefault("ENVIRONMENT", "development")
    check_test_urls({v: env[v] for v in DB_URL_VARS}, env["ENVIRONMENT"])
