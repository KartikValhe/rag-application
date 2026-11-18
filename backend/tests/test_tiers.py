"""
tests/test_tiers.py
───────────────────
The tier table (tests/tier_map.py) cannot silently rot: every test file has an entry, no entry is stale,
an unmapped file fails collection with a clear message, the markers are applied, and a `fast` test really
cannot touch Postgres, Redis or a child process.
"""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.tier_map import (
    TEST_FILE_TIERS,
    apply_tiers,
    discover_test_files,
    table_problems,
)

TESTS_ROOT = Path(__file__).resolve().parent


def test_every_test_file_has_an_entry_and_every_entry_has_a_file() -> None:
    assert table_problems(discover_test_files(TESTS_ROOT)) == []


def test_table_problems_flag_each_kind_of_mistake() -> None:
    table = {
        "test_a.py": ("fast", "db"),  # contradictory
        "test_b.py": ("security",),  # no infrastructure tier at all
        "test_c.py": ("fast", "bogus"),  # unknown marker
        "test_d.py": ("db",),  # fine, but the file is missing
        "test_e.py": ("linux", "db"),  # linux is automatic, never in the table
    }
    problems = "\n".join(
        table_problems(["test_a.py", "test_b.py", "test_c.py", "test_e.py", "test_new.py"], table)
    )
    assert "test_new.py: test file has no entry" in problems
    assert "test_d.py: entry in TEST_FILE_TIERS but no such test file" in problems
    assert "test_a.py: 'fast' cannot be combined with ['db']" in problems
    assert "test_b.py: needs 'fast' or at least one of" in problems
    assert "test_c.py: unknown or automatic marker(s) ['bogus']" in problems
    assert "test_e.py: unknown or automatic marker(s) ['linux']" in problems


class _FakeItem:
    """Just enough of a pytest Item for apply_tiers."""

    def __init__(self, path: Path, skipif_reasons: tuple[str, ...] = ()) -> None:
        self.path = path
        self.marks: list[str] = []
        self._skipifs = [SimpleNamespace(kwargs={"reason": r}) for r in skipif_reasons]

    def add_marker(self, mark: pytest.MarkDecorator) -> None:
        self.marks.append(mark.name)

    def iter_markers(self, name: str):  # type: ignore[no-untyped-def]
        return iter(self._skipifs if name == "skipif" else [])


def test_a_test_file_without_an_entry_fails_collection_with_a_clear_message() -> None:
    items = [_FakeItem(TESTS_ROOT / "test_isolation.py"), _FakeItem(TESTS_ROOT / "test_brand_new.py")]
    with pytest.raises(pytest.UsageError) as excinfo:
        apply_tiers(items, TESTS_ROOT)
    message = str(excinfo.value)
    assert "tests/test_brand_new.py" in message
    assert "TEST_FILE_TIERS in tests/tier_map.py" in message
    assert "tests/test_isolation.py" not in message  # only the unmapped file is named


def test_markers_come_from_the_table_and_touched_and_linux_are_added() -> None:
    plain = _FakeItem(TESTS_ROOT / "test_isolation.py")
    linux = _FakeItem(TESTS_ROOT / "test_sandbox.py", skipif_reasons=("PR_SET_PDEATHSIG is Linux only",))
    posix = _FakeItem(TESTS_ROOT / "test_ingest_worker.py", skipif_reasons=("POSIX signals",))
    other_skip = _FakeItem(TESTS_ROOT / "test_sandbox.py", skipif_reasons=("needs a thing",))
    apply_tiers([plain, linux, posix, other_skip], TESTS_ROOT, frozenset({"test_isolation.py"}))
    assert set(plain.marks) == {*TEST_FILE_TIERS["test_isolation.py"], "touched"}
    assert "linux" in linux.marks and "linux" in posix.marks
    assert "linux" not in other_skip.marks and "touched" not in other_skip.marks
    assert "linux" not in plain.marks


def test_nested_files_use_their_relative_path_as_the_key() -> None:
    item = _FakeItem(TESTS_ROOT / "security" / "test_prove_it_fails_review2.py")
    apply_tiers([item], TESTS_ROOT)
    assert {"fast", "security"} <= set(item.marks)


def test_the_gate_tier_contains_the_isolation_suite_and_the_hostile_input_tests() -> None:
    security = {rel for rel, marks in TEST_FILE_TIERS.items() if "security" in marks}
    assert {"test_isolation.py", "test_tenants_rls.py", "test_authz_rbac.py"} <= security
    assert {"test_upload_preauth.py", "test_sandbox.py", "test_worker_tenant_guard.py"} <= security


# ── a `fast` test cannot use infrastructure (this file is in the fast tier) ──


def test_this_file_is_fast_and_the_guard_is_active() -> None:
    assert TEST_FILE_TIERS["test_tiers.py"] == ("fast",)


def test_a_fast_test_cannot_start_a_child_process() -> None:
    with pytest.raises(AssertionError, match="marked 'fast' tried to use a child process"):
        subprocess.Popen(["python", "-c", "pass"])


async def test_a_fast_test_cannot_start_an_asyncio_child_process() -> None:
    with pytest.raises(AssertionError, match="marked 'fast' tried to use a child process"):
        await asyncio.create_subprocess_exec("python", "-c", "pass")


async def test_a_fast_test_cannot_open_a_postgres_connection() -> None:
    import asyncpg

    with pytest.raises(AssertionError, match="marked 'fast' tried to use a Postgres connection"):
        await asyncpg.connect("postgresql://nobody@localhost:1/none")


async def test_a_fast_test_cannot_open_a_redis_connection() -> None:
    import redis.asyncio as aioredis

    client = aioredis.from_url("redis://localhost:1/0")
    try:
        with pytest.raises(AssertionError, match="marked 'fast' tried to use a Redis connection"):
            await client.ping()
    finally:
        await client.aclose()
