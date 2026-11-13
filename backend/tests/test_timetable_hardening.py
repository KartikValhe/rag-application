"""
Regression tests for the timetable upload hardening (findings 1, 2, 3 and 8).

Finding 1: parse child isolation (env, cwd, limits, tree kill)
Finding 2: leaked Redis slots must expire on their own
Finding 3: Redis checked per call, fail closed outside development, atomic rate limits
Finding 8: large child results must not cause a false timeout
"""

import ctypes
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.upload_security import UploadSecurityError
from app.services import timetable_parser_runner as tpr

CHILD = "tests._tt_children"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as dead when /proc says so
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


async def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    import asyncio

    end = time.time() + seconds
    while time.time() < end:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not _pid_alive(pid)


# ─── Finding 1: child isolation ───────────────────────────────────────────────

async def test_child_cannot_see_secret_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "DATABASE_URL": "postgresql://x:y@h/db",
        "MIGRATION_DATABASE_URL": "postgresql://x:y@h/db",
        "SECRET_KEY": "s" * 40,
        "PLATFORM_SECRET_KEY": "p" * 40,
        "REDIS_URL": "redis://h:6379/0",
        "REDIS_PASSWORD": "pw",
        "SOME_API_TOKEN": "tok",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)

    records = await tpr.run_killable_parse(b"", "env", uuid.uuid4(), child_module=CHILD)
    seen = {k.upper() for k in records[0]["env"]}
    for name in secrets:
        assert name not in seen, f"{name} leaked into the parse child"
    assert not any("SECRET" in k or "PASSWORD" in k or "TOKEN" in k or "DATABASE" in k for k in seen)


async def test_child_runs_in_empty_temp_working_directory() -> None:
    records = await tpr.run_killable_parse(b"", "env", uuid.uuid4(), child_module=CHILD)
    assert records[0]["listing"] == []
    assert Path(records[0]["cwd"]).resolve() != Path.cwd().resolve()
    # The temp dir is removed once the parse is over
    assert not Path(records[0]["cwd"]).exists()


async def test_input_bytes_reach_child_over_stdin() -> None:
    payload = b"a" * 300_000
    records = await tpr.run_killable_parse(payload, "echo_len", uuid.uuid4(), child_module=CHILD)
    assert records[0]["n"] == len(payload)


async def test_timeout_kills_hung_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid.txt"
    t0 = time.time()
    with pytest.raises(UploadSecurityError) as exc:
        await tpr.run_killable_parse(
            str(pid_file).encode(), "sleep", uuid.uuid4(), timeout_seconds=2.0, child_module=CHILD
        )
    elapsed = time.time() - t0
    assert exc.value.status_code == 408
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "parse_timeout"
    assert 1.5 <= elapsed < 8.0, f"elapsed {elapsed:.2f}s"
    assert await _wait_dead(int(pid_file.read_text()))


async def test_timeout_kills_whole_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    with pytest.raises(UploadSecurityError):
        await tpr.run_killable_parse(
            str(pid_file).encode(), "tree", uuid.uuid4(), timeout_seconds=2.0, child_module=CHILD
        )
    child_pid, grandchild_pid = (int(p) for p in pid_file.read_text().split(","))
    assert await _wait_dead(child_pid)
    assert await _wait_dead(grandchild_pid), "grandchild survived the timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="rlimits are not available on Windows")
async def test_child_applies_rlimits() -> None:
    records = await tpr.run_killable_parse(b"", "rlimit", uuid.uuid4(), child_module=CHILD)
    assert 0 < records[0]["as"] <= 1024 * 1024 * 1024
    assert 0 < records[0]["cpu"] <= 30


# ─── Finding 8: large results ─────────────────────────────────────────────────

async def test_500kb_result_returns_correctly() -> None:
    t0 = time.time()
    records = await tpr.run_killable_parse(b"", "big", uuid.uuid4(), child_module=CHILD)
    assert len(records) == 1
    assert len(records[0]["raw_cell"]) == 500_000
    assert time.time() - t0 < 8.0


async def test_output_over_cap_is_rejected() -> None:
    t0 = time.time()
    with pytest.raises(UploadSecurityError) as exc:
        await tpr.run_killable_parse(b"", "huge", uuid.uuid4(), child_module=CHILD)
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "parse_output_too_large"
    assert time.time() - t0 < 9.0


async def test_child_crash_without_output_is_reported() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        await tpr.run_killable_parse(b"", "crash", uuid.uuid4(), child_module=CHILD)
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "worker_crash"


# ─── Slot manager (findings 2 and 3) ──────────────────────────────────────────

@pytest.fixture(params=["redis", "memory"])
async def mgr(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from app.core import redis_client
    from app.core.slots import SlotManager

    if request.param == "redis":
        try:
            r = await redis_client.get_redis()
            await r.ping()
        except Exception:
            pytest.skip("Redis not reachable")
    else:
        async def _down() -> Any:
            raise ConnectionError("redis down")

        monkeypatch.setattr(redis_client, "get_redis", _down)
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    yield SlotManager()


def _limit(key: str, limit: int, ttl: float = 30.0) -> Any:
    from app.core.slots import SlotLimit

    return SlotLimit(key=key, limit=limit, ttl_seconds=ttl)


async def test_slots_enforce_cap_and_release(mgr: Any) -> None:
    from app.core.slots import SlotRejected

    key = f"test:slots:{uuid.uuid4()}"
    a = await mgr.acquire([_limit(key, 2)])
    b = await mgr.acquire([_limit(key, 2)])
    with pytest.raises(SlotRejected):
        await mgr.acquire([_limit(key, 2)])
    await mgr.release(a)
    c = await mgr.acquire([_limit(key, 2)])
    await mgr.release(b)
    await mgr.release(c)
    assert await mgr.count(key) == 0


async def test_leaked_slot_expires_on_its_own(mgr: Any) -> None:
    import asyncio

    from app.core.slots import SlotRejected

    key = f"test:slots:{uuid.uuid4()}"
    await mgr.acquire([_limit(key, 1, ttl=0.4)])  # never released: a crashed worker
    with pytest.raises(SlotRejected):
        await mgr.acquire([_limit(key, 1, ttl=0.4)])
    await asyncio.sleep(0.7)
    lease = await mgr.acquire([_limit(key, 1, ttl=0.4)])
    await mgr.release(lease)


async def test_no_permanent_lockout_when_rejected_attempts_keep_coming(mgr: Any) -> None:
    """Old code refreshed the key TTL on every attempt, so leaked slots never expired."""
    import asyncio

    from app.core.slots import SlotRejected

    key = f"test:slots:{uuid.uuid4()}"
    for _ in range(5):
        await mgr.acquire([_limit(key, 5, ttl=0.6)])  # five leaked slots
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            lease = await mgr.acquire([_limit(key, 5, ttl=0.6)])
        except SlotRejected:
            await asyncio.sleep(0.1)  # hammering the key must not extend the leaked slots
            continue
        await mgr.release(lease)
        return
    pytest.fail("Leaked slots never expired: permanent lockout")


async def test_rejection_on_later_key_consumes_nothing_on_earlier_keys(mgr: Any) -> None:
    from app.core.slots import SlotRejected

    user_key = f"test:user:{uuid.uuid4()}"
    tenant_key = f"test:tenant:{uuid.uuid4()}"
    holder = await mgr.acquire([_limit(tenant_key, 1)])
    for _ in range(5):
        with pytest.raises(SlotRejected) as exc:
            await mgr.acquire([_limit(user_key, 3), _limit(tenant_key, 1)])
        assert exc.value.index == 1
    assert await mgr.count(user_key) == 0
    await mgr.release(holder)


async def test_concurrent_acquires_never_exceed_cap(mgr: Any) -> None:
    import asyncio

    from app.core.slots import SlotRejected

    key = f"test:slots:{uuid.uuid4()}"

    async def attempt() -> bool:
        try:
            await mgr.acquire([_limit(key, 3)])
            return True
        except SlotRejected:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(results) == 3
    assert await mgr.count(key) == 3


# ─── Redis failure handling ───────────────────────────────────────────────────

class _BrokenRedis:
    async def eval(self, *_: Any, **__: Any) -> Any:
        raise ConnectionError("redis went away")

    async def zrem(self, *_: Any, **__: Any) -> Any:
        raise ConnectionError("redis went away")


async def test_redis_failure_fails_closed_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import redis_client
    from app.core.slots import SlotManager

    async def _broken() -> Any:
        return _BrokenRedis()

    monkeypatch.setattr(redis_client, "get_redis", _broken)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(HTTPException) as exc:
        await SlotManager().acquire([_limit(f"test:{uuid.uuid4()}", 1)])
    assert exc.value.status_code == 503
    assert isinstance(exc.value.detail, dict)
    assert "message" in exc.value.detail


async def test_redis_down_at_startup_is_rechecked_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis was down earlier, now up: the next call must use Redis again (no cached probe)."""
    from app.core import redis_client
    from app.core.slots import SlotManager

    real = redis_client.get_redis
    state = {"down": True}

    async def _flaky() -> Any:
        if state["down"]:
            raise ConnectionError("down")
        return await real()

    monkeypatch.setattr(redis_client, "get_redis", _flaky)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    m = SlotManager()
    key = f"test:{uuid.uuid4()}"
    with pytest.raises(HTTPException):
        await m.acquire([_limit(key, 1)])
    state["down"] = False
    try:
        lease = await m.acquire([_limit(key, 1)])
    except Exception:
        pytest.skip("Redis not reachable")
    await m.release(lease)


async def test_development_falls_back_to_memory_with_loud_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core import redis_client
    from app.core.slots import SlotManager

    async def _broken() -> Any:
        return _BrokenRedis()

    monkeypatch.setattr(redis_client, "get_redis", _broken)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    with caplog.at_level(logging.ERROR):
        lease = await SlotManager().acquire([_limit(f"test:{uuid.uuid4()}", 1)])
    assert any("REDIS UNAVAILABLE" in r.getMessage() for r in caplog.records)
    assert lease is not None


async def test_redis_failure_between_acquire_and_release_does_not_lock_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acquire works, then Redis dies before release: release must not raise, slot expires."""
    import asyncio

    from app.core import redis_client
    from app.core.slots import SlotManager

    try:
        r = await redis_client.get_redis()
        await r.ping()
    except Exception:
        pytest.skip("Redis not reachable")

    m = SlotManager()
    key = f"test:slots:{uuid.uuid4()}"
    lease = await m.acquire([_limit(key, 1, ttl=0.5)])

    async def _broken() -> Any:
        return _BrokenRedis()

    monkeypatch.setattr(redis_client, "get_redis", _broken)
    await m.release(lease)  # must swallow the failure
    monkeypatch.undo()
    await asyncio.sleep(0.8)
    lease2 = await m.acquire([_limit(key, 1, ttl=0.5)])
    await m.release(lease2)


# ─── Rate limits through the runner API ───────────────────────────────────────

async def test_tenant_rate_rejection_does_not_burn_user_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.slots import SlotManager

    monkeypatch.setattr(tpr, "TENANT_RATE_LIMIT_COUNT", 2)
    tenant_id = uuid.uuid4()
    for _ in range(2):  # other users fill the tenant window
        await tpr.check_and_record_rate_limit(uuid.uuid4(), tenant_id)

    user_id = uuid.uuid4()
    for _ in range(4):
        with pytest.raises(HTTPException) as exc:
            await tpr.check_and_record_rate_limit(user_id, tenant_id)
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["code"] == "tenant_rate_limit_exceeded"
        assert exc.value.status_code == 429

    assert await SlotManager().count(f"{tpr.USER_RATELIMIT_KEY_PREFIX}{user_id}") == 0


async def test_user_rate_limit_still_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tpr, "USER_RATE_LIMIT_COUNT", 3)
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await tpr.check_and_record_rate_limit(user_id, tenant_id)
    with pytest.raises(HTTPException) as exc:
        await tpr.check_and_record_rate_limit(user_id, tenant_id)
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "rate_limit_exceeded"
    assert int(exc.value.headers["Retry-After"]) > 0  # type: ignore[index]


# ─── Slots released by the runner ─────────────────────────────────────────────

async def _tenant_slots(tenant_id: uuid.UUID) -> int:
    from app.core.slots import SlotManager

    return await SlotManager().count(f"{tpr.TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}")


async def test_slots_released_after_parse_failure() -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(UploadSecurityError):
        await tpr.run_killable_parse(b"", "fail", tenant_id, child_module=CHILD)
    assert await _tenant_slots(tenant_id) == 0
    async with tpr.parse_concurrency_guard(tenant_id), tpr.parse_concurrency_guard(tenant_id):
        pass


async def test_slots_released_after_exception_in_guard_body() -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        async with tpr.parse_concurrency_guard(tenant_id):
            raise RuntimeError("handler blew up")
    assert await _tenant_slots(tenant_id) == 0


async def test_slots_released_after_killed_child(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(UploadSecurityError):
        await tpr.run_killable_parse(
            str(tmp_path / "p.txt").encode(), "sleep", tenant_id, timeout_seconds=1.0, child_module=CHILD
        )
    assert await _tenant_slots(tenant_id) == 0
    async with tpr.parse_concurrency_guard(tenant_id), tpr.parse_concurrency_guard(tenant_id):
        pass


async def test_concurrency_guard_tenant_rejection_keeps_global_slot_free() -> None:
    from app.core.slots import SlotManager

    tenant_id = uuid.uuid4()
    before = await SlotManager().count(tpr.GLOBAL_CONCURRENCY_KEY)
    async with tpr.parse_concurrency_guard(tenant_id), tpr.parse_concurrency_guard(tenant_id):
        with pytest.raises(HTTPException) as exc:
            async with tpr.parse_concurrency_guard(tenant_id):
                pass
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["code"] == "tenant_concurrent_limit_exceeded"
        assert await SlotManager().count(tpr.GLOBAL_CONCURRENCY_KEY) == before + 2
    assert await SlotManager().count(tpr.GLOBAL_CONCURRENCY_KEY) == before


def test_child_pythonpath_includes_custom_module_root_when_app_is_installed_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    CI installs `app` into site-packages, so the runner's package root does not contain the
    tests directory. The child's PYTHONPATH must still reach a non-default child module,
    and must stay limited to the package root for the default module.
    """
    fake_site_packages = tmp_path / "site-packages"
    fake_site_packages.mkdir()
    monkeypatch.setattr(tpr, "_BACKEND_ROOT", fake_site_packages)

    tests_parent = str(Path(__file__).resolve().parent.parent)
    custom = tpr._child_env(str(tmp_path), CHILD)["PYTHONPATH"].split(os.pathsep)
    assert str(fake_site_packages) in custom
    assert tests_parent in custom

    default = tpr._child_env(str(tmp_path), tpr.DEFAULT_CHILD_MODULE)["PYTHONPATH"].split(os.pathsep)
    assert default == [str(fake_site_packages)]
