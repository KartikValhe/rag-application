"""
app/core/sandbox.py
───────────────────
Run untrusted parsing in a FRESH, killable child process (`python -P -m <module> [args]`).

Shared by the timetable import (services/timetable_parser_runner.py) and the document worker
(workers/stages.py). Each call:

- starts the child with a minimal environment: no DATABASE_URL, SECRET_KEY, REDIS_* or any other
  secret, only what Python needs plus the sandbox limits;
- runs it in an empty temporary working directory that is deleted afterwards;
- sends the input over stdin (optionally framed: one JSON line of parameters, then the bytes, see
  sandbox_child.read_request) and reads ONE JSON document from stdout, capped at max_output_bytes;
- reads stdout and stderr while it waits, so a large result never looks like a timeout and a
  chatty child never blocks on a full pipe (only the last 4 KB of stderr are kept);
- enforces a hard wall clock and kills the whole process tree (process group on POSIX,
  `taskkill /T` on Windows);
- kills the child as well when the calling task is cancelled (a stopped job must not leave a parser
  running);
- logs an abnormal exit with the exit code, the signal name and the exception CLASS name only
  (a strict marker line from sandbox_child), never stderr text, paths or content.

The memory (RLIMIT_AS) and CPU (RLIMIT_CPU) limits and the parent-death signal are applied by the
child itself (sandbox_child.run); RLIMIT_* and PR_SET_PDEATHSIG exist on Linux only. On Windows
(development only) the wall clock is the only limit.

This module does not import the app settings: callers pass every limit.
"""

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.sandbox_child import CPU_ENV, MEMORY_ENV, PARENT_PID_ENV

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[2]  # directory that contains the `app` package
STDERR_TAIL_BYTES = 4096
_POLL_SECONDS = 0.1
# The child may print one marker line with the class name of an uncaught exception to stderr.
# Only a strict identifier match is ever read from stderr; nothing else is kept or logged.
_ERROR_TYPE_MARKER = re.compile(rb"child_error_type=([A-Za-z_][A-Za-z0-9_]{0,60})")


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float
    max_output_bytes: int
    memory_limit_bytes: int | None = None  # RLIMIT_AS in the child (Linux)
    cpu_seconds: int | None = None  # RLIMIT_CPU in the child (Linux)


class SandboxError(Exception):
    """A sandbox failure with a stable error code (never any child text)."""

    code = "worker_crash"

    def __init__(self) -> None:
        super().__init__(self.code)


class SandboxTimeout(SandboxError):
    code = "parse_timeout"


class SandboxOutputTooLarge(SandboxError):
    code = "parse_output_too_large"


class SandboxCrash(SandboxError):
    """The child exited without a usable result (crash, kill, garbage on stdout)."""

    code = "worker_crash"


# ── Environment ──────────────────────────────────────────────────────────────


def child_python_path(child_module: str, backend_root: Path = BACKEND_ROOT) -> str:
    """
    Import roots for the child. For a module of the `app` package only the directory that holds
    it. The package may be installed into site-packages, so another module (a test child) is looked
    up in the parent and the directory that holds it is added too.
    """
    roots = [str(backend_root)]
    top = child_module.split(".")[0]
    if top != "app":
        try:
            spec = importlib.util.find_spec(top)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            locations = list(spec.submodule_search_locations or [])
            if not locations and spec.origin:
                locations = [spec.origin]
            for loc in locations:
                root = str(Path(loc).resolve().parent)
                if root not in roots:
                    roots.append(root)
    return os.pathsep.join(roots)


def child_env(
    workdir: str,
    child_module: str,
    *,
    backend_root: Path = BACKEND_ROOT,
    memory_limit_bytes: int | None = None,
    cpu_seconds: int | None = None,
) -> dict[str, str]:
    """Minimal environment. Nothing is inherited from the parent except what Python itself needs."""
    env = {
        "PYTHONPATH": child_python_path(child_module, backend_root),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": workdir,
        "TEMP": workdir,
        "TMP": workdir,
    }
    if memory_limit_bytes:
        env[MEMORY_ENV] = str(int(memory_limit_bytes))
    if cpu_seconds:
        env[CPU_ENV] = str(int(cpu_seconds))
    # Lets the child notice a parent that died before PR_SET_PDEATHSIG was set
    env[PARENT_PID_ENV] = str(os.getpid())
    if sys.platform == "win32":
        # Windows cannot start Python (winsock, random) without SYSTEMROOT
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


# ── Process control ──────────────────────────────────────────────────────────


def kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    """Kill the child and everything it started."""
    if sys.platform == "win32":
        with suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    else:
        with suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session/process group
    with suppress(Exception):
        proc.kill()


def describe_exit(returncode: int | None, stderr_tail: bytes) -> tuple[str, str, str]:
    """Exit code, signal name and exception class name for server-side logs. No paths or text."""
    exit_code = "none" if returncode is None else str(returncode)
    signal_name = "none"
    if returncode is not None and returncode < 0:
        try:
            signal_name = signal.Signals(-returncode).name
        except ValueError:
            signal_name = f"SIG{-returncode}"
    match = _ERROR_TYPE_MARKER.search(stderr_tail)
    error_type = match.group(1).decode("ascii") if match else "unknown"
    return exit_code, signal_name, error_type


def _stdin_bytes(data: bytes, params: Mapping[str, Any] | None) -> bytes:
    if params is None:
        return data  # raw protocol (the timetable child reads all of stdin as the file)
    return json.dumps(dict(params), separators=(",", ":")).encode() + b"\n" + data


def run_child_blocking(
    module: str,
    data: bytes,
    *,
    args: Sequence[str] = (),
    params: Mapping[str, Any] | None = None,
    limits: SandboxLimits,
    logger: logging.Logger | None = None,
    log_event: str = "sandbox_child_abnormal_exit",
    cancel: threading.Event | None = None,
) -> bytes:
    """
    Run one child and return its raw stdout (b"" when it produced nothing).
    Raises SandboxTimeout, SandboxOutputTooLarge, or SandboxCrash when `cancel` is set.
    Blocking: call it from a thread (run_child does).
    """
    log = logger or globals()["logger"]
    workdir = tempfile.mkdtemp(prefix="sandbox_")
    proc: subprocess.Popen[bytes] | None = None
    try:
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", module, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=child_env(
                workdir,
                module,
                memory_limit_bytes=limits.memory_limit_bytes,
                cpu_seconds=limits.cpu_seconds,
            ),
            close_fds=True,
            **popen_kwargs,
        )
        child = proc
        payload = _stdin_bytes(data, params)
        chunks: list[bytes] = []
        overflow = threading.Event()

        def write_input() -> None:
            try:
                assert child.stdin is not None
                child.stdin.write(payload)
                child.stdin.close()
            except (OSError, ValueError):
                pass  # the child died or was killed before reading everything

        def read_output() -> None:
            total = 0
            assert child.stdout is not None
            try:
                while True:
                    chunk = child.stdout.read(65536)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > limits.max_output_bytes:
                        overflow.set()
                        kill_tree(child)
                        return
                    chunks.append(chunk)
            except (OSError, ValueError):
                return

        stderr_tail = bytearray()

        def read_stderr() -> None:
            # Drain stderr so the child never blocks on it; keep only the last 4 KB
            assert child.stderr is not None
            try:
                while True:
                    chunk = child.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-STDERR_TAIL_BYTES]
            except (OSError, ValueError):
                return

        writer = threading.Thread(target=write_input, daemon=True)
        reader = threading.Thread(target=read_output, daemon=True)
        err_reader = threading.Thread(target=read_stderr, daemon=True)
        writer.start()
        reader.start()
        err_reader.start()

        deadline = time.monotonic() + limits.timeout_seconds
        timed_out = False
        # Wait for stdout to close, waking up often enough to notice a cancellation
        while reader.is_alive():
            if cancel is not None and cancel.is_set():
                kill_tree(child)
                raise SandboxCrash()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            reader.join(min(_POLL_SECONDS, remaining))
        if not timed_out and not overflow.is_set():
            while True:
                try:
                    child.wait(timeout=min(_POLL_SECONDS, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        kill_tree(child)
                        raise SandboxCrash() from None
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break

        if overflow.is_set():
            raise SandboxOutputTooLarge()
        if timed_out:
            kill_tree(child)
            raise SandboxTimeout()
        writer.join(timeout=1.0)
        err_reader.join(timeout=1.0)
        output = b"".join(chunks)
        if proc.returncode not in (0, None) or not output:
            exit_code, signal_name, error_type = describe_exit(proc.returncode, bytes(stderr_tail))
            log.warning(
                "%s exit_code=%s signal=%s error_type=%s", log_event, exit_code, signal_name, error_type
            )
        return output
    finally:
        if proc is not None:
            if proc.poll() is None:
                kill_tree(proc)
            with suppress(Exception):
                proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    with suppress(Exception):
                        pipe.close()
        shutil.rmtree(workdir, ignore_errors=True)


async def run_child(
    module: str,
    data: bytes,
    *,
    args: Sequence[str] = (),
    params: Mapping[str, Any] | None = None,
    limits: SandboxLimits,
    logger: logging.Logger | None = None,
    log_event: str = "sandbox_child_abnormal_exit",
) -> bytes:
    """
    run_child_blocking in a worker thread. If the calling task is cancelled, the child (and its
    tree) is killed within about 0.1 s and CancelledError propagates.
    """
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(
            run_child_blocking,
            module,
            data,
            args=args,
            params=params,
            limits=limits,
            logger=logger,
            log_event=log_event,
            cancel=cancel,
        )
    except asyncio.CancelledError:
        cancel.set()
        raise


def parse_child_json(raw: bytes) -> dict[str, Any]:
    """The child's one JSON object, or SandboxCrash (empty or unreadable output)."""
    if not raw:
        raise SandboxCrash()
    try:
        result = json.loads(raw)
    except ValueError:
        raise SandboxCrash() from None
    if not isinstance(result, dict):
        raise SandboxCrash()
    return result
