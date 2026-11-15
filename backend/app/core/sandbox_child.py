"""
app/core/sandbox_child.py
─────────────────────────
The child side of the sandbox (app/core/sandbox.py). A child module calls `run(handler)` from its
`__main__` block and gets, before any of its own code runs:

  - the parent-death signal (Linux): prctl(PR_SET_PDEATHSIG, SIGKILL), so a worker that is killed
    cannot leave its parser running. If the parent already died before the call, the child exits;
  - the memory (RLIMIT_AS) and CPU (RLIMIT_CPU) limits the parent passed in the environment
    (POSIX only; skipped where the resource module does not exist, i.e. Windows);
  - the framed request: one JSON line of parameters, then the raw input bytes, read from stdin;
  - a stdout guard: anything the libraries print goes to stderr, so stdout carries exactly one JSON
    document, the handler's result;
  - crash reporting without content: an uncaught exception writes only
    `child_error_type=<ClassName>` to stderr and exits 1. The exception text (which may hold paths
    or document content) never leaves the child.

This module imports only the standard library: the child runs with a minimal environment and must
never load app.core.config (which reads .env and needs secrets).
"""

import importlib
import json
import os
import signal
import sys
from collections.abc import Callable
from typing import Any, BinaryIO

PR_SET_PDEATHSIG = 1
MEMORY_ENV = "SANDBOX_MEMORY_LIMIT_BYTES"
CPU_ENV = "SANDBOX_CPU_SECONDS"
PARENT_PID_ENV = "SANDBOX_PARENT_PID"
MAX_HEADER_BYTES = 64 * 1024

Handler = Callable[[dict[str, Any], bytes], dict[str, Any]]


def set_parent_death_signal() -> bool:
    """
    Ask the Linux kernel to SIGKILL this process when its parent dies. Returns True when it is set.
    Off Linux (Windows development, macOS) this is a safe no-op that returns False; the parent's
    wall clock still kills the child there.

    The "parent" for PR_SET_PDEATHSIG is the THREAD that spawned the child. The sandbox spawns and
    waits from the same thread for the whole life of the child, so that thread only ends when the
    process ends or the child is done.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
            return False
    except (OSError, AttributeError):
        return False
    # The parent may have died before prctl ran: then the signal never comes, so exit now.
    expected = os.environ.get(PARENT_PID_ENV)
    if expected and expected.isdigit() and os.getppid() != int(expected):
        os._exit(1)
    return True


def apply_resource_limits(memory_bytes: int | None, cpu_seconds: int | None) -> None:
    """RLIMIT_AS and RLIMIT_CPU. Skipped where the resource module does not exist (Windows)."""
    try:
        # importlib keeps mypy happy on platforms without the resource module
        resource = importlib.import_module("resource")
    except ImportError:
        return
    if memory_bytes:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    if cpu_seconds:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


def _int_env(name: str) -> int | None:
    value = os.environ.get(name, "")
    return int(value) if value.isdigit() and int(value) > 0 else None


def read_request(stream: BinaryIO) -> tuple[dict[str, Any], bytes]:
    """One JSON line of parameters (bounded), then the raw input bytes."""
    header = stream.readline(MAX_HEADER_BYTES + 1)
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
        raise ValueError("sandbox request header is missing or too long")
    params = json.loads(header)
    if not isinstance(params, dict):
        raise ValueError("sandbox request header is not an object")
    return params, stream.read()


def run(handler: Handler) -> None:
    """The whole child bootstrap. Exits 0 after writing the result, 1 on any uncaught error."""
    try:
        set_parent_death_signal()
        apply_resource_limits(_int_env(MEMORY_ENV), _int_env(CPU_ENV))
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # library prints must never corrupt the JSON result
        params, data = read_request(sys.stdin.buffer)
        result = handler(params, data)
        real_stdout.write(json.dumps(result))
        real_stdout.flush()
    except BaseException as exc:
        # Only the class name is reported, for the parent's server-side log
        sys.stderr.write(f"child_error_type={type(exc).__name__}\n")
        sys.stderr.flush()
        sys.exit(1)
