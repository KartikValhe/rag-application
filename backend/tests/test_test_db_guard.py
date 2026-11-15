"""The test-database guard must refuse anything that is not a local `_test` database."""

import pytest

from app.core.test_db_guard import (
    DB_URL_VARS,
    TestDatabaseGuardError,
    apply_test_database_env,
    check_test_urls,
    derive_test_url,
)

LOCAL_DEV = "postgresql+asyncpg://app_tenant:pw@localhost:5433/ragplatform"


def _urls(url: str) -> dict[str, str]:
    return {v: url for v in DB_URL_VARS}


def test_derive_appends_suffix_and_keeps_rest():
    assert derive_test_url(LOCAL_DEV) == "postgresql+asyncpg://app_tenant:pw@localhost:5433/ragplatform_test"


def test_derive_is_idempotent():
    test_url = derive_test_url(LOCAL_DEV)
    assert derive_test_url(test_url) == test_url


def test_guard_aborts_for_dev_style_url():
    with pytest.raises(TestDatabaseGuardError, match="does not end with"):
        check_test_urls(_urls(LOCAL_DEV), "development")


def test_guard_aborts_for_remote_host():
    remote = "postgresql+asyncpg://postgres:pw@db.abcdefgh.supabase.co:5432/ragplatform_test"
    with pytest.raises(TestDatabaseGuardError, match="not local"):
        check_test_urls(_urls(remote), "development")


def test_guard_aborts_when_not_development():
    with pytest.raises(TestDatabaseGuardError, match="ENVIRONMENT"):
        check_test_urls(_urls(derive_test_url(LOCAL_DEV)), "staging")


def test_guard_accepts_ci_urls():
    ci = "postgresql+asyncpg://postgres:postgres@localhost:5432/ragplatform_test"
    check_test_urls(_urls(ci), "development")


def test_apply_rewrites_dev_urls_and_blocks_remote_even_after_derivation():
    env = {v: LOCAL_DEV for v in DB_URL_VARS}
    apply_test_database_env(env)
    assert all(env[v].endswith("/ragplatform_test") for v in DB_URL_VARS)
    remote = {v: "postgresql+asyncpg://u:p@db.example.com:5432/postgres" for v in DB_URL_VARS}
    with pytest.raises(TestDatabaseGuardError):
        apply_test_database_env(remote)
