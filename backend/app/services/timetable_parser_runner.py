"""
app/services/timetable_parser_runner.py
───────────────────────────────────────
Hardened runner for timetable parsing: limits, slots and a sandboxed child process.

Each parse runs in a FRESH subprocess (`python -m <child module>`):
- minimal environment: no DATABASE_URL, SECRET_KEY, REDIS_* or any other secret;
- empty temporary working directory, deleted afterwards;
- file bytes in over stdin, one JSON result out over stdout (capped at 5 MB);
- hard wall-clock timeout that kills the whole process tree;
- the parent reads output while it waits, so large results never look like a timeout;
- on Linux the child applies RLIMIT_AS / RLIMIT_CPU (see timetable_parse_child.py).

Concurrency caps and rate limits use the atomic slot manager in app/core/slots.py.
"""

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.core.slots import SlotLease, SlotLimit, SlotManager, SlotRejected, default_manager
from app.core.upload_security import UploadSecurityError

logger = logging.getLogger(__name__)

# ─── Concurrency Limits ───────────────────────────────────────────────────────
MAX_GLOBAL_CONCURRENT_PARSES = 5
MAX_TENANT_CONCURRENT_PARSES = 2
# A slot token expires by itself after this long, so a crashed worker cannot block anyone.
SLOT_LEASE_SECONDS = 30.0

GLOBAL_CONCURRENCY_KEY = "concurrency:timetable_parse:global"
TENANT_CONCURRENCY_KEY_PREFIX = "concurrency:timetable_parse:tenant:"

# ─── Rate Limits (count / window_seconds) ──────────────────────────────────────
USER_RATE_LIMIT_COUNT = 10
USER_RATE_LIMIT_WINDOW = 600  # 10 minutes
USER_RATELIMIT_KEY_PREFIX = "ratelimit:timetable_parse:user:"

TENANT_RATE_LIMIT_COUNT = 30
TENANT_RATE_LIMIT_WINDOW = 600  # 10 minutes
TENANT_RATELIMIT_KEY_PREFIX = "ratelimit:timetable_parse:tenant:"

# ─── Child process ────────────────────────────────────────────────────────────
DEFAULT_CHILD_MODULE = "app.services.timetable_parse_child"
MAX_OUTPUT_BYTES = 5 * 1024 * 1024

# Fixed client-facing messages keyed by stable error code. Anything else becomes "parse_failed".
CHILD_ERROR_MESSAGES: dict[str, str] = {
    "parse_failed": "The file could not be processed. Please check it and try again.",
    "unsupported_format": "This file type cannot be imported here.",
    "nul_byte_in_csv": "CSV file contains forbidden NUL bytes.",
    "invalid_encoding": "CSV file encoding not supported (must be UTF-8 or CP1252).",
    "empty_workbook": "Workbook contains no sheets.",
    "scanned_pdf": (
        "This PDF contains scanned images or no selectable text. "
        "Please use the CSV/XLSX template or manual entry."
    ),
    "password_protected_pdf": (
        "Password-protected PDFs cannot be imported. "
        "Please provide an unencrypted PDF or use the CSV/XLSX template."
    ),
    "corrupt_pdf": "The PDF document could not be read.",
}
_BACKEND_ROOT = Path(__file__).resolve().parents[2]  # directory that contains the `app` package


def _slots() -> SlotManager:
    return default_manager


def _too_many(code: str, message: str, retry_after: int | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        detail["retry_after_seconds"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail, headers=headers)


async def check_and_record_rate_limit(user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """
    Sliding-window limits per user and per tenant. Both windows are checked and recorded in one
    atomic step, so a tenant-level rejection never consumes the user's quota.
    Fails closed with 503 when Redis is unreachable outside development.
    """
    limits = [
        SlotLimit(f"{USER_RATELIMIT_KEY_PREFIX}{user_id}", USER_RATE_LIMIT_COUNT, USER_RATE_LIMIT_WINDOW),
        SlotLimit(f"{TENANT_RATELIMIT_KEY_PREFIX}{tenant_id}", TENANT_RATE_LIMIT_COUNT, TENANT_RATE_LIMIT_WINDOW),
    ]
    try:
        await _slots().acquire(limits)
    except SlotRejected as rej:
        if rej.index == 0:
            raise _too_many(
                "rate_limit_exceeded",
                f"User upload rate limit exceeded ({USER_RATE_LIMIT_COUNT} uploads per 10 minutes). "
                "Please wait before importing again.",
                rej.retry_after_seconds,
            ) from None
        raise _too_many(
            "tenant_rate_limit_exceeded",
            f"Organization upload rate limit exceeded ({TENANT_RATE_LIMIT_COUNT} uploads per 10 minutes). "
            "Please wait before importing again.",
            rej.retry_after_seconds,
        ) from None


@asynccontextmanager
async def parse_concurrency_guard(
    tenant_id: uuid.UUID, lease_seconds: float = SLOT_LEASE_SECONDS
) -> AsyncGenerator[None, None]:
    """
    Global and per-tenant concurrency caps. The slot is released in a finally block; if that
    fails or the process dies, the token expires on its own after `lease_seconds`.
    """
    manager = _slots()
    lease: SlotLease
    try:
        lease = await manager.acquire(
            [
                SlotLimit(GLOBAL_CONCURRENCY_KEY, MAX_GLOBAL_CONCURRENT_PARSES, lease_seconds),
                SlotLimit(
                    f"{TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}", MAX_TENANT_CONCURRENT_PARSES, lease_seconds
                ),
            ]
        )
    except SlotRejected as rej:
        if rej.index == 0:
            raise _too_many(
                "too_many_concurrent_parses",
                "The system is currently busy parsing files. Please retry in a few moments.",
            ) from None
        raise _too_many(
            "tenant_concurrent_limit_exceeded",
            "Your organization already has concurrent timetable imports running. Please wait for them to finish.",
        ) from None
    try:
        yield
    finally:
        await manager.release(lease)


# ─── Sandboxed child process ──────────────────────────────────────────────────

def _child_python_path(child_module: str) -> str:
    """
    Import roots for the child. Normally only the directory that holds the `app` package.
    The package may be installed into site-packages, so a non-default child module (used by
    tests) is looked up in the parent and the directory that holds it is added too.
    """
    roots = [str(_BACKEND_ROOT)]
    if child_module != DEFAULT_CHILD_MODULE:
        top = child_module.split(".")[0]
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


def _child_env(workdir: str, child_module: str = DEFAULT_CHILD_MODULE) -> dict[str, str]:
    """Minimal environment. Nothing is inherited from the parent except what Python itself needs."""
    env = {
        "PYTHONPATH": _child_python_path(child_module),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": workdir,
        "TEMP": workdir,
        "TMP": workdir,
    }
    if sys.platform == "win32":
        # Windows cannot start Python (winsock, random) without SYSTEMROOT
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
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
            os.killpg(proc.pid, signal.SIGKILL)  # child is its own session/process group leader
    with suppress(Exception):
        proc.kill()


def _run_child_blocking(
    data: bytes, format_type: str, child_module: str, timeout_seconds: float, max_output_bytes: int
) -> bytes:
    workdir = tempfile.mkdtemp(prefix="ttparse_")
    proc: subprocess.Popen[bytes] | None = None
    try:
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", child_module, format_type],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=workdir,
            env=_child_env(workdir, child_module),
            close_fds=True,
            **popen_kwargs,
        )
        child = proc
        chunks: list[bytes] = []
        overflow = threading.Event()

        def write_input() -> None:
            try:
                assert child.stdin is not None
                child.stdin.write(data)
                child.stdin.close()
            except (OSError, ValueError):
                pass  # child died or was killed before reading everything

        def read_output() -> None:
            total = 0
            assert child.stdout is not None
            try:
                while True:
                    chunk = child.stdout.read(65536)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > max_output_bytes:
                        overflow.set()
                        _kill_tree(child)
                        return
                    chunks.append(chunk)
            except (OSError, ValueError):
                return

        writer = threading.Thread(target=write_input, daemon=True)
        reader = threading.Thread(target=read_output, daemon=True)
        writer.start()
        reader.start()

        deadline = time.monotonic() + timeout_seconds
        reader.join(timeout_seconds)
        timed_out = reader.is_alive()
        if not timed_out and not overflow.is_set():
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True

        if overflow.is_set():
            raise UploadSecurityError(
                code="parse_output_too_large",
                message="The parsed result is too large to process.",
            )
        if timed_out:
            _kill_tree(child)
            raise UploadSecurityError(
                code="parse_timeout",
                message="File parsing exceeded the 10-second timeout and was terminated.",
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
            )
        writer.join(timeout=1.0)
        return b"".join(chunks)
    finally:
        if proc is not None:
            if proc.poll() is None:
                _kill_tree(proc)
            with suppress(Exception):
                proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    with suppress(Exception):
                        pipe.close()
        shutil.rmtree(workdir, ignore_errors=True)


async def run_killable_parse(
    data: bytes,
    format_type: str,
    tenant_id: uuid.UUID,
    timeout_seconds: float = 10.0,
    child_module: str = DEFAULT_CHILD_MODULE,
) -> list[dict[str, Any]]:
    """
    Parse an uploaded file in a fresh, sandboxed subprocess with a hard timeout.
    `child_module` is the module started with `python -m`; it must follow the stdin/stdout
    contract described in timetable_parse_child.py.
    """
    async with parse_concurrency_guard(tenant_id, lease_seconds=max(SLOT_LEASE_SECONDS, timeout_seconds + 20)):
        raw = await asyncio.to_thread(
            _run_child_blocking, data, format_type, child_module, timeout_seconds, MAX_OUTPUT_BYTES
        )

    if not raw:
        raise UploadSecurityError(
            code="worker_crash",
            message="Parsing process exited unexpectedly without returning data.",
        )
    try:
        res = json.loads(raw)
    except ValueError:
        raise UploadSecurityError(
            code="worker_crash",
            message="Parsing process returned an unreadable result.",
        ) from None
    if not isinstance(res, dict):
        raise UploadSecurityError(code="worker_crash", message="Parsing process returned an unreadable result.")
    if not res.get("success"):
        child_code = res.get("error_code")
        if child_code == "parse_failure":
            # Server-side detail only: the exception class, never its text, file names or content
            logger.warning("timetable_parse_failed error_type=%s", str(res.get("error_type", "unknown"))[:80])
        code = child_code if child_code in CHILD_ERROR_MESSAGES else "parse_failed"
        # The child's own free-text message is ignored on purpose
        raise UploadSecurityError(code=code, message=CHILD_ERROR_MESSAGES[code])
    records = res.get("records", [])
    return records if isinstance(records, list) else []
