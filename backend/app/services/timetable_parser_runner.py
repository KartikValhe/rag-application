"""
app/services/timetable_parser_runner.py
───────────────────────────────────────
Hardened runner for timetable parsing: limits, slots and a sandboxed child process.

The sandbox itself is generic and lives in app/core/sandbox.py (step 5b-2): a FRESH subprocess
(`python -m <child module>`) per parse with a minimal environment, an empty temporary working
directory, file bytes over stdin and one JSON result over stdout (capped at 5 MB here), a hard
wall clock that kills the whole process tree, and output read while waiting. On Linux the child
applies RLIMIT_AS / RLIMIT_CPU and the parent-death signal (see timetable_parse_child.py).

This module keeps what is timetable-specific: the slots, the rate limits, the client messages and
the mapping of sandbox errors to the Phase 4 error codes and status codes.
Concurrency caps and rate limits use the atomic slot manager in app/core/slots.py.
"""

import asyncio  # noqa: F401  (tests patch asyncio.to_thread through this module)
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.core import sandbox
from app.core.sandbox import describe_exit as _describe_exit  # noqa: F401  (kept for callers and tests)
from app.core.sandbox import kill_tree as _kill_tree  # noqa: F401  (kept for callers and tests)
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


# The text clients see when Redis is down; the slot manager itself has no timetable wording.
UNAVAILABLE_MESSAGE = "Timetable import is temporarily unavailable. Please try again in a few minutes."


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
        await _slots().acquire(limits, unavailable_message=UNAVAILABLE_MESSAGE)
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
            ],
            unavailable_message=UNAVAILABLE_MESSAGE,
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


# ─── Sandboxed child process (generic part: app/core/sandbox.py) ─────────────

def _child_env(workdir: str, child_module: str = DEFAULT_CHILD_MODULE) -> dict[str, str]:
    """The sandbox's minimal environment, rooted at this module's view of the backend directory."""
    return sandbox.child_env(workdir, child_module, backend_root=_BACKEND_ROOT)


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
    contract described in timetable_parse_child.py (raw file bytes on stdin, no framing).
    """
    limits = sandbox.SandboxLimits(timeout_seconds=timeout_seconds, max_output_bytes=MAX_OUTPUT_BYTES)
    async with parse_concurrency_guard(tenant_id, lease_seconds=max(SLOT_LEASE_SECONDS, timeout_seconds + 20)):
        try:
            raw = await sandbox.run_child(
                child_module,
                data,
                args=(format_type,),
                params=None,
                limits=limits,
                logger=logger,
                log_event="timetable_child_abnormal_exit",
            )
        except sandbox.SandboxOutputTooLarge:
            raise UploadSecurityError(
                code="parse_output_too_large",
                message="The parsed result is too large to process.",
            ) from None
        except sandbox.SandboxTimeout:
            raise UploadSecurityError(
                code="parse_timeout",
                message="File parsing exceeded the 10-second timeout and was terminated.",
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
            ) from None

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
