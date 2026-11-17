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
- parses the child's answer in two ways: `parse_child_json` (timetable import: lenient) and
  `parse_child_json_strict` (document parser): the child is untrusted, so the strict one bounds the answer
  BEFORE anything is built (see its docstring);
- logs an abnormal exit with the exit code, the signal name and the exception CLASS name only
  (a strict marker line from sandbox_child), never stderr text, paths or content.

The memory (RLIMIT_AS) and CPU (RLIMIT_CPU) limits, the parent-death signal and the non-dumpable flag
are applied by the child itself (sandbox_child.run); they exist on Linux only. On Windows (development
only) the wall clock is the only limit.

What this module does NOT do (see "Worker deployment requirements" in README.md and design C3): the child
runs as the SAME user as its parent. The parent (the worker) makes itself non-dumpable at startup, so the
child cannot read /proc/<parent pid>/environ, but nothing here stops it from reading files that user can
read (the backend source folder, a .env file). That is deployment's job: a container with a non-root user,
no .env file in the image, filesystem permissions, no outbound network. Known gaps of the kill: a
grandchild whose parent already exited is missed by `taskkill /T`, `killpg` misses a grandchild that
called setsid, and the parent-death signal covers only the direct child (backlog B44).

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
import site
import subprocess
import sys
import sysconfig
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


class SandboxOutputInvalid(SandboxError):
    """The child answered, but not with JSON the parent accepts (syntax, duplicate keys, NaN, nesting ...).
    A hostile or broken answer is the FILE's problem and does not get better by trying again: permanent."""

    code = "parse_output_invalid"


# ── Environment ──────────────────────────────────────────────────────────────


def _is_interpreter_site_dir(path: Path) -> bool:
    """True for the interpreter's own site-packages (already on the child's path by default)."""
    resolved = path.resolve()
    candidates = {sysconfig.get_paths().get("purelib", ""), sysconfig.get_paths().get("platlib", "")}
    with suppress(AttributeError):  # some virtualenv builds of `site` have no getsitepackages
        candidates.update(site.getsitepackages())
    return any(c and Path(c).resolve() == resolved for c in candidates)


def child_python_path(child_module: str, backend_root: Path = BACKEND_ROOT) -> str:
    """
    Import roots for the child, as few as it needs. For a module of the `app` package only the
    directory that holds it, and not even that when the package is installed into site-packages (a
    production image): the child's default path reaches it already, so the backend source folder
    (with whatever else lies next to it) is not on the path at all. A module outside `app` (a test
    child) is looked up in the parent and the directory that holds it is added too.
    """
    roots = [] if _is_interpreter_site_dir(backend_root) else [str(backend_root)]
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
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": workdir,
        "TEMP": workdir,
        "TMP": workdir,
    }
    python_path = child_python_path(child_module, backend_root)
    if python_path:
        env["PYTHONPATH"] = python_path
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


def _decode(raw: bytes) -> str:
    """Strict UTF-8 (the child writes UTF-8). json.loads(bytes) would also guess UTF-16 and UTF-32."""
    return raw.decode("utf-8")


def parse_child_json(raw: bytes) -> dict[str, Any]:
    """The child's one JSON object, or SandboxCrash (empty or unreadable output). Lenient: used by the
    timetable import, whose child is bounded by its byte cap alone."""
    if not raw:
        raise SandboxCrash()
    try:
        result = json.loads(_decode(raw))
    except (ValueError, RecursionError):  # also UnicodeDecodeError; a deeply nested answer
        raise SandboxCrash() from None
    if not isinstance(result, dict):
        raise SandboxCrash()
    return result


# ── The strict parser for the document child ─────────────────────────────────


@dataclass(frozen=True)
class JsonLimits:
    """What a legitimate answer can never exceed (the caller derives both from its contract)."""

    max_structure: int  # commas + opening braces + opening brackets, outside strings
    max_depth: int  # nesting of objects and arrays


_MAX_INT_DIGITS = 18  # the answer holds small counters only
# One JSON string, skipped with possessive quantifiers (Python 3.11+): linear, no backtracking stack
_STRING_RE = re.compile(rb'"(?:[^"\\]++|\\.)*+"', re.DOTALL)
_BAD_STRING_RE = re.compile("[\x00\ud800-\udfff]")  # NUL and lone surrogates


def _check_structure(raw: bytes, limits: JsonLimits) -> None:
    """Phase 1, on the raw bytes and before anything is decoded or built: count the structural tokens. The
    strings are cut out first (one C-speed regex pass), so a comma inside a text does not count and the
    count is exact for a valid answer. A hostile `[{},{},{}, ...]` of 12 MB would otherwise cost
    hundreds of MB as Python objects."""
    bare = _STRING_RE.sub(b'""', raw)
    if bare.count(b",") + bare.count(b"{") + bare.count(b"[") > limits.max_structure:
        raise SandboxOutputTooLarge()


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")  # {"success": false, "success": true} is ambiguous
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN or Infinity")


def _reject_float(_text: str) -> Any:
    raise ValueError("float")  # the answer has none


def _parse_int(text: str) -> int:
    if len(text) > _MAX_INT_DIGITS:
        raise ValueError("integer too long")
    return int(text)


def _depth_ok(depth: int, limits: JsonLimits) -> bool:
    return depth <= limits.max_depth


def _string_ok(value: str) -> bool:
    return _BAD_STRING_RE.search(value) is None


def _check_tree(root: object, limits: JsonLimits) -> None:
    """Phase 3, on the parsed value with an explicit stack: nesting depth, and no NUL or lone surrogate in
    any string or key. Bounded by phase 1, so the walk is bounded too."""
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if not _depth_ok(depth, limits):
                raise SandboxOutputInvalid()
            for key, value in item.items():
                if not _string_ok(key):
                    raise SandboxOutputInvalid()
                stack.append((value, depth + 1))
        elif isinstance(item, list):
            if not _depth_ok(depth, limits):
                raise SandboxOutputInvalid()
            stack.extend((value, depth + 1) for value in item)
        elif isinstance(item, str) and not _string_ok(item):
            raise SandboxOutputInvalid()


def parse_child_json_strict(raw: bytes, *, limits: JsonLimits) -> dict[str, Any]:
    """
    The document child's one JSON object. The child is untrusted (a parser exploit makes it write anything),
    so this is a two-phase parse, every phase bounded:

      1. `_check_structure` on the raw bytes: the number of structural tokens is capped
         (SandboxOutputTooLarge, parse_output_too_large) before a single object exists;
      2. strict UTF-8, then json.loads with hooks: duplicate keys, NaN and Infinity, floats and integers of
         more than 18 digits are refused; nesting beyond the interpreter's recursion limit raises
         RecursionError, and any ValueError (syntax, huge ints, bad UTF-8, a BOM, trailing text) is mapped;
         both become SandboxOutputInvalid (parse_output_invalid);
      3. `_check_tree` on the result: nesting depth and no NUL or lone surrogate anywhere.

    Empty output is still a crash (the child never answered). The byte cap was applied while reading.
    """
    if not raw:
        raise SandboxCrash()
    try:
        _check_structure(raw, limits)
        result = json.loads(
            _decode(raw),
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_int,
        )
    except SandboxError:
        raise
    except (ValueError, RecursionError):
        raise SandboxOutputInvalid() from None
    if not isinstance(result, dict):
        raise SandboxOutputInvalid()
    _check_tree(result, limits)
    return result
