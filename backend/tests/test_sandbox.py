"""
The generic sandbox (app/core/sandbox.py, step 5b-2): a fresh child process per call with a stripped
environment, an empty temp working directory, framed JSON over stdin/stdout, a hard wall clock that
kills the whole process tree, a bounded output, crash logging that leaks no content, cancellation
that kills the child, and (Linux only) resource limits and the parent-death signal.

The test child is tests/_sandbox_children.py, started through the production spawn path.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.core import sandbox
from app.core.sandbox_child import set_parent_death_signal
from tests.test_timetable_hardening import _pid_alive, _wait_dead

CHILD = "tests._sandbox_children"
LIMITS = sandbox.SandboxLimits(timeout_seconds=10.0, max_output_bytes=1024 * 1024)


async def _run(mode: str, *, params: dict[str, object] | None = None, data: bytes = b"", limits: sandbox.SandboxLimits = LIMITS) -> dict[str, object]:
    raw = await sandbox.run_child(CHILD, data, args=(mode,), params=params or {}, limits=limits)
    return sandbox.parse_child_json(raw)


# ── Environment and working directory ────────────────────────────────────────


async def test_child_environment_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "DATABASE_URL": "postgresql://x:y@h/db_test",
        "WORKER_DATABASE_URL": "postgresql://x:y@h/db_test",
        "MIGRATION_DATABASE_URL": "postgresql://x:y@h/db_test",
        "SECRET_KEY": "s" * 40,
        "REDIS_URL": "redis://h:6379/1",
        "SOME_API_TOKEN": "tok",
        "AWS_SECRET_ACCESS_KEY": "aws",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    result = await _run("env")
    env = result["env"]
    assert isinstance(env, dict)
    seen = {k.upper() for k in env}
    for name in secrets:
        assert name not in seen, f"{name} leaked into the sandbox child"
    assert not any(word in k for k in seen for word in ("SECRET", "PASSWORD", "TOKEN", "DATABASE", "REDIS"))
    # The child never loads the app settings (they would read .env and need secrets)
    assert result["config_loaded"] is False


async def test_child_runs_in_an_empty_temp_dir_that_is_removed() -> None:
    result = await _run("env")
    assert result["listing"] == []
    cwd = Path(str(result["cwd"]))
    assert cwd.resolve() != Path.cwd().resolve()
    assert not cwd.exists(), "the sandbox temp dir was left behind"


async def test_params_and_bytes_travel_over_framed_stdin() -> None:
    payload = b"%PDF-1.4 hello" + b"a" * 300_000
    result = await _run("echo", params={"file_kind": "pdf", "max_pdf_pages": 7}, data=payload)
    assert result["request"] == {"file_kind": "pdf", "max_pdf_pages": 7}
    assert result["n"] == len(payload)
    assert result["head"] == payload[:16].decode("latin-1")


async def test_library_prints_cannot_corrupt_the_json_result() -> None:
    assert (await _run("noisy"))["success"] is True


# ── Timeouts, output cap, crashes ────────────────────────────────────────────


async def test_timeout_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid.txt"
    t0 = time.monotonic()
    with pytest.raises(sandbox.SandboxTimeout) as exc:
        await _run("sleep", params={"pid_file": str(pid_file)}, limits=sandbox.SandboxLimits(1.5, 1024))
    assert exc.value.code == "parse_timeout"
    assert time.monotonic() - t0 < 8.0
    assert await _wait_dead(int(pid_file.read_text()))


async def _both_pids(pid_file: Path) -> tuple[int, int]:
    """Wait (up to 30 s) until the test child has written its own pid and its grandchild's pid."""
    for _ in range(600):
        if pid_file.exists() and "," in pid_file.read_text():
            child, grandchild = (int(p) for p in pid_file.read_text().split(","))
            return child, grandchild
        await asyncio.sleep(0.05)
    raise AssertionError("the test child never reported its pids")


async def test_the_whole_process_tree_is_killed_while_both_are_alive(tmp_path: Path) -> None:
    """
    Deterministic (review of 5b-1/5b-2, finding 11): the kill is triggered only after the test has SEEN
    both processes alive, so it does not depend on how long Python takes to start, and a tree kill that
    only works while the parent is still alive is exactly what this covers.
    """
    pid_file = tmp_path / "pids.txt"
    task = asyncio.create_task(_run("tree", params={"pid_file": str(pid_file)}, limits=sandbox.SandboxLimits(60.0, 1024)))
    child, grandchild = await _both_pids(pid_file)
    assert _pid_alive(child) and _pid_alive(grandchild), "precondition: both processes must be alive at kill time"
    task.cancel()  # the sandbox kills the tree when its caller is cancelled (same kill_tree as a timeout)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(child)
    assert await _wait_dead(grandchild), "grandchild survived the kill of its parent's tree"


async def test_a_timeout_kills_the_tree_that_was_seen_alive(tmp_path: Path) -> None:
    """The wall clock path: the limit is long, the test fires it deterministically through the deadline."""
    pid_file = tmp_path / "pids.txt"
    limits = sandbox.SandboxLimits(timeout_seconds=15.0, max_output_bytes=1024)
    task = asyncio.create_task(_run("tree", params={"pid_file": str(pid_file)}, limits=limits))
    child, grandchild = await _both_pids(pid_file)
    assert _pid_alive(child) and _pid_alive(grandchild)
    with pytest.raises(sandbox.SandboxTimeout):
        await asyncio.wait_for(task, 40)
    assert await _wait_dead(child)
    assert await _wait_dead(grandchild), "grandchild survived the sandbox timeout"


def _spawn_like_the_sandbox(mode: str, pid_file: Path, workdir: Path) -> "subprocess.Popen[bytes]":
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-P", "-m", CHILD, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(workdir), env=sandbox.child_env(str(workdir), CHILD), **kwargs,  # type: ignore[call-overload]
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"pid_file": str(pid_file)}).encode() + b"\n")
    proc.stdin.close()
    return proc


@pytest.mark.xfail(
    sys.platform == "win32", strict=True,
    reason="KNOWN GAP (backlog B44): taskkill /T cannot find a grandchild whose parent already exited; "
    "a Windows Job Object set to kill on close would. Strict: this test must start passing when that lands.",
)
def test_a_grandchild_whose_parent_already_exited_is_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    proc = _spawn_like_the_sandbox("orphan", pid_file, tmp_path)
    grandchild = 0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (pid_file.exists() and "," in pid_file.read_text()):
            time.sleep(0.05)
        child, grandchild = (int(p) for p in pid_file.read_text().split(","))
        proc.wait(timeout=30)  # the child returned its result and exited; its grandchild lives on
        assert not _pid_alive(child) and _pid_alive(grandchild), "precondition: parent gone, grandchild alive"
        sandbox.kill_tree(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(grandchild):
            time.sleep(0.1)
        assert not _pid_alive(grandchild), "an orphaned grandchild survived kill_tree"
    finally:
        if grandchild and _pid_alive(grandchild):  # never leave a sleeper behind
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(grandchild)], capture_output=True, check=False)
            else:
                os.kill(grandchild, 9)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


async def test_output_over_the_cap_is_refused_and_the_child_killed() -> None:
    with pytest.raises(sandbox.SandboxOutputTooLarge) as exc:
        await _run("huge", params={"size": 300_000}, limits=sandbox.SandboxLimits(10.0, 100_000))
    assert exc.value.code == "parse_output_too_large"


async def test_output_just_under_the_cap_is_returned() -> None:
    result = await _run("huge", params={"size": 50_000}, limits=sandbox.SandboxLimits(10.0, 100_000))
    assert len(str(result["blob"])) == 50_000


async def test_crash_is_logged_without_any_content(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("tests.sandbox.crash")
    with caplog.at_level(logging.WARNING, logger=log.name):
        raw = await sandbox.run_child(
            CHILD, b"", args=("boom",), params={}, limits=LIMITS, logger=log, log_event="sandbox_test_crash"
        )
    assert raw == b""
    with pytest.raises(sandbox.SandboxCrash) as exc:
        sandbox.parse_child_json(raw)
    assert exc.value.code == "worker_crash"
    text = caplog.text
    assert "sandbox_test_crash" in text
    assert "exit_code=1" in text
    assert "error_type=ZeroDivisionError" in text
    for leak in ("CANARY", "private", "syllabus.pdf"):
        assert leak not in text, f"child content leaked into the log: {leak}"


def test_unreadable_output_is_a_crash() -> None:
    for raw in (b"not json", b"[1, 2]", b'"text"'):
        with pytest.raises(sandbox.SandboxCrash):
            sandbox.parse_child_json(raw)


def test_describe_exit_reads_only_a_strict_marker() -> None:
    assert sandbox.describe_exit(-15, b"") == ("-15", "SIGTERM", "unknown")  # SIGKILL does not exist on Windows
    assert sandbox.describe_exit(1, b"x child_error_type=MemoryError\n") == ("1", "none", "MemoryError")
    assert sandbox.describe_exit(1, b"child_error_type=../../etc/passwd") == ("1", "none", "unknown")


# ── Cancellation kills the child (a stopped job must not leave a parser running) ─


async def test_cancelling_the_caller_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid.txt"
    task = asyncio.create_task(_run("sleep", params={"pid_file": str(pid_file)}, limits=sandbox.SandboxLimits(30.0, 1024)))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    assert _pid_alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(pid), "a cancelled sandbox call left its child running"


# ── Linux only: resource limits and the parent-death signal ──────────────────


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="rlimits are applied on Linux only")
async def test_child_applies_memory_and_cpu_limits() -> None:
    limits = sandbox.SandboxLimits(10.0, 1024 * 1024, memory_limit_bytes=512 * 1024 * 1024, cpu_seconds=20)
    result = await _run("rlimit", limits=limits)
    assert result["as"] == 512 * 1024 * 1024
    assert result["cpu"] == 20


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")
async def test_child_sets_the_parent_death_signal() -> None:
    result = await _run("pdeathsig")
    assert result["pdeathsig"] == result["sigkill"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")
def test_killed_parent_does_not_orphan_the_child(tmp_path: Path) -> None:
    """SIGKILL the process that runs the sandbox: the child (its own session) must die too."""
    import signal

    pid_file = tmp_path / "pid.txt"
    backend = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        f"""
        from app.core import sandbox
        sandbox.run_child_blocking(
            {CHILD!r}, b"", args=("sleep",), params={{"pid_file": {str(pid_file)!r}}},
            limits=sandbox.SandboxLimits(60.0, 1024),
        )
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", script], cwd=str(backend), env={**os.environ, "PYTHONPATH": str(backend)})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text()):
        time.sleep(0.05)
    child = int(pid_file.read_text())
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(child):
        time.sleep(0.05)
    assert not _pid_alive(child), "the sandbox child was orphaned when its parent was killed"


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the non-Linux fallback")
def test_parent_death_signal_is_a_safe_no_op_off_linux() -> None:
    assert set_parent_death_signal() is False


# ── The environment builder ──────────────────────────────────────────────────


def test_child_env_is_minimal_and_limits_travel_in_it(tmp_path: Path) -> None:
    env = sandbox.child_env(str(tmp_path), "app.services.document_parse_child", memory_limit_bytes=1024, cpu_seconds=5)
    allowed = {
        "PYTHONPATH", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "TMPDIR", "TEMP", "TMP",
        "SYSTEMROOT", "SANDBOX_MEMORY_LIMIT_BYTES", "SANDBOX_CPU_SECONDS", "SANDBOX_PARENT_PID",
    }
    assert set(env) <= allowed
    assert env["SANDBOX_MEMORY_LIMIT_BYTES"] == "1024"
    assert env["SANDBOX_CPU_SECONDS"] == "5"
    assert json.loads(env["SANDBOX_PARENT_PID"]) == os.getpid()
