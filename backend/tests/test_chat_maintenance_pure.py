"""
tests/test_chat_maintenance_pure.py
───────────────────────────────────
Step 6a-12b (docs/phase6-plan.md 9.2, 9.4; owner answers of the 6a-12a brief and the Q4 / C35 decisions), no database:

  - the worker refuses to start when the user name in CHAT_MAINT_DATABASE_URL is not app_chat_maint (the check never
    prints the URL or the password); an unset URL means "no chat maintenance in this worker", not an error;
  - the retention delete does NOTHING for a null retention: it does not even open a connection;
  - a retention that is not a whole number from 30 to 3650 (a bool, a float, 29, 3651) is refused before any
    connection is used, so no code path can delete with a made-up period;
  - the settings and the guard list: LLM_USAGE_RETENTION_DAYS (400), the test-DB guard names the URL, .env.example
    documents both.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings, settings
from app.core.test_db_guard import DB_URL_VARS, DEFAULT_URLS, apply_test_database_env
from app.services import chat_maintenance
from app.services.chat_maintenance import ChatMaintenance, MaintenanceNotConfigured, RetentionCounts
from app.workers import sweeper
from app.workers.db import TenantRef

GOOD_URL = "postgresql+asyncpg://app_chat_maint:s3cret-pw@db.internal:5432/ragplatform"
REF = TenantRef(uuid.uuid4())


class ExplodingEngine:
    """Any use of the engine fails the test: the code under test must refuse before it touches a connection."""

    def connect(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("a connection was opened")

    begin = connect

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the engine was used ({name})")


def _maint() -> ChatMaintenance:
    return ChatMaintenance(ExplodingEngine(), stale_seconds=120, usage_retention_days=400)  # type: ignore[arg-type]


# ── the URL's user name ──────────────────────────────────────────────────────


def test_the_right_user_is_accepted() -> None:
    chat_maintenance.check_url_user(GOOD_URL)
    chat_maintenance.check_url_user("postgresql+asyncpg://app_chat_maint@localhost:5433/ragplatform")  # no password


@pytest.mark.parametrize("user", ["app_tenant", "app_worker", "app_platform", "postgres", "App_Chat_Maint", "app_chat_maint2", ""])
def test_any_other_user_is_refused_without_echoing_the_url_or_the_password(user: str) -> None:
    url = f"postgresql+asyncpg://{user}:s3cret-pw@db.internal:5432/ragplatform"
    with pytest.raises(MaintenanceNotConfigured) as err:
        chat_maintenance.check_url_user(url)
    assert "app_chat_maint" in str(err.value)
    assert "s3cret-pw" not in str(err.value) and "db.internal" not in str(err.value)


def test_an_unparsable_url_is_refused_without_echoing_it() -> None:
    with pytest.raises(MaintenanceNotConfigured) as err:
        chat_maintenance.check_url_user("not a url s3cret-pw")
    assert "s3cret-pw" not in str(err.value)


def test_from_settings_is_none_when_unset_and_refuses_a_wrong_user() -> None:
    assert ChatMaintenance.from_settings(SimpleNamespace(
        CHAT_MAINT_DATABASE_URL=SecretStr(""), CHAT_PENDING_STALE_SECONDS=120, LLM_USAGE_RETENTION_DAYS=400)) is None  # type: ignore[arg-type]
    with pytest.raises(MaintenanceNotConfigured):
        ChatMaintenance.from_settings(SimpleNamespace(  # type: ignore[arg-type]
            CHAT_MAINT_DATABASE_URL=SecretStr("postgresql+asyncpg://app_tenant:x@h/db"), CHAT_PENDING_STALE_SECONDS=120,
            LLM_USAGE_RETENTION_DAYS=400))


def test_the_sweepers_start_check_lists_a_wrong_user_and_says_nothing_for_unset_or_right() -> None:
    def problems(url: str) -> list[str]:
        return sweeper.maintenance_problems(SimpleNamespace(CHAT_MAINT_DATABASE_URL=SecretStr(url)))  # type: ignore[arg-type]

    assert problems("") == [] and problems("   ") == [] and problems(GOOD_URL) == []
    wrong = problems("postgresql+asyncpg://app_worker:s3cret-pw@db.internal/ragplatform")
    assert len(wrong) == 1 and "app_chat_maint" in wrong[0] and "s3cret-pw" not in wrong[0]


# ── a null retention deletes nothing, an invalid one is refused ──────────────


async def test_a_null_retention_deletes_and_counts_nothing_and_opens_no_connection() -> None:
    maint = _maint()
    assert await maint.delete_expired(REF, None) == RetentionCounts()
    assert await maint.count_retention_due(REF, None) == RetentionCounts()


@pytest.mark.parametrize("days", [0, 1, 29, 3651, -30, True, False, 30.0, 90.5, "90", [90], 10**20])
async def test_a_retention_that_is_not_a_whole_number_from_30_to_3650_is_refused_before_any_connection(days: Any) -> None:
    maint = _maint()
    with pytest.raises(ValueError):
        await maint.delete_expired(REF, days)
    with pytest.raises(ValueError):
        await maint.count_retention_due(REF, days)


def test_the_valid_range_is_the_owners_30_to_3650() -> None:
    assert (chat_maintenance.RETENTION_DAYS_MIN, chat_maintenance.RETENTION_DAYS_MAX) == (30, 3650)
    for days in (30, 90, 3650):
        chat_maintenance.validate_retention_days(days)


def test_a_maintenance_object_needs_sane_numbers() -> None:
    for bad in ({"batch_size": 0}, {"max_batches": 0}, {"stale_seconds": 0}, {"usage_retention_days": 0}):
        values: dict[str, Any] = {"stale_seconds": 120, "usage_retention_days": 400} | bad
        with pytest.raises(ValueError):
            ChatMaintenance(ExplodingEngine(), **values)  # type: ignore[arg-type]
    assert chat_maintenance.DEFAULT_BATCH_SIZE == 500


# ── settings, guard, .env.example ────────────────────────────────────────────


def test_the_usage_retention_default_is_400_days_and_is_bounded() -> None:
    assert Settings.model_fields["LLM_USAGE_RETENTION_DAYS"].default == 400
    for bad in (0, 29, 3651):
        with pytest.raises(ValueError):
            Settings(LLM_USAGE_RETENTION_DAYS=bad, _env_file=None)  # type: ignore[call-arg]


def test_the_maintenance_url_is_a_secret_and_defaults_to_unset() -> None:
    field = Settings.model_fields["CHAT_MAINT_DATABASE_URL"]
    assert field.default == SecretStr("") or getattr(field.default, "get_secret_value", lambda: field.default)() == ""
    assert isinstance(settings.CHAT_MAINT_DATABASE_URL, SecretStr)
    assert "s3cret" not in repr(Settings.model_construct(CHAT_MAINT_DATABASE_URL=SecretStr("s3cret")))


def test_the_test_database_guard_knows_the_maintenance_url_and_its_user() -> None:
    assert "CHAT_MAINT_DATABASE_URL" in DB_URL_VARS
    assert DEFAULT_URLS["CHAT_MAINT_DATABASE_URL"].startswith("postgresql+asyncpg://app_chat_maint")
    env: dict[str, str] = {}
    apply_test_database_env(env)
    assert env["CHAT_MAINT_DATABASE_URL"].endswith("/ragplatform_test")
    chat_maintenance.check_url_user(env["CHAT_MAINT_DATABASE_URL"])


@pytest.mark.parametrize("name", ["CHAT_MAINT_DATABASE_URL", "LLM_USAGE_RETENTION_DAYS"])
def test_env_example_documents_the_new_settings(name: str) -> None:
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^#?\s*{name}=", text, flags=re.MULTILINE), f"{name} is missing from .env.example"
    for line in text.splitlines():
        if line.startswith(f"{name}=") and name == "CHAT_MAINT_DATABASE_URL":
            assert "password" not in line.lower().replace("user:password", ""), "no password in the example"
