"""
app/services/timetable_parser_runner.py
───────────────────────────────────────
Kill-able process runner with concurrency and rate limits for timetable parsing.

Key security requirements:
- Runs parsing in a dedicated multiprocessing.Process that can be force-killed.
- Hard 10-second timeout.
- Global concurrency cap (max 5 simultaneous parses).
- Per-tenant concurrency cap (max 2 simultaneous parses).
- Per-user and per-tenant rate limits (sliding window).
"""

import asyncio
import multiprocessing
import os
import signal
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import HTTPException, status

from app.core import redis_client
from app.core.upload_security import UploadSecurityError
from app.services.timetable_importer import (
    extract_raw_records,
    parse_raw_csv,
    parse_raw_docx,
    parse_raw_pdf,
    parse_raw_xlsx,
)

# ─── Concurrency Limits ───────────────────────────────────────────────────────
MAX_GLOBAL_CONCURRENT_PARSES = 5
MAX_TENANT_CONCURRENT_PARSES = 2
CONCURRENCY_KEY_TTL = 30  # short 30s TTL prevents stuck counters if a worker crashes
REDIS_OP_TIMEOUT = 0.5  # fast timeout falls back cleanly to in-memory if Redis is offline

GLOBAL_CONCURRENCY_KEY = "concurrency:timetable_parse:global"
TENANT_CONCURRENCY_KEY_PREFIX = "concurrency:timetable_parse:tenant:"

# ─── Rate Limits (count / window_seconds) ──────────────────────────────────────
USER_RATE_LIMIT_COUNT = 10
USER_RATE_LIMIT_WINDOW = 600  # 10 minutes
USER_RATELIMIT_KEY_PREFIX = "ratelimit:timetable_parse:user:"

TENANT_RATE_LIMIT_COUNT = 30
TENANT_RATE_LIMIT_WINDOW = 600  # 10 minutes
TENANT_RATELIMIT_KEY_PREFIX = "ratelimit:timetable_parse:tenant:"

# In-memory tracking structures (fallback when Redis is unavailable)
_concurrency_lock: asyncio.Lock | None = None
_global_active_parses = 0
_tenant_active_parses: dict[uuid.UUID, int] = defaultdict(int)

_user_rate_history: dict[uuid.UUID, list[float]] = defaultdict(list)
_tenant_rate_history: dict[uuid.UUID, list[float]] = defaultdict(list)


def _check_and_record_rate_limit_mem(user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """In-memory fallback for rate limiting."""
    now = time.time()
    user_times = [t for t in _user_rate_history[user_id] if now - t < USER_RATE_LIMIT_WINDOW]
    if len(user_times) >= USER_RATE_LIMIT_COUNT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limit_exceeded",
                "message": f"User upload rate limit exceeded ({USER_RATE_LIMIT_COUNT} uploads per 10 minutes). Please wait before importing again.",
            },
        )
    user_times.append(now)
    _user_rate_history[user_id] = user_times

    tenant_times = [t for t in _tenant_rate_history[tenant_id] if now - t < TENANT_RATE_LIMIT_WINDOW]
    if len(tenant_times) >= TENANT_RATE_LIMIT_COUNT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "tenant_rate_limit_exceeded",
                "message": f"Organization upload rate limit exceeded ({TENANT_RATE_LIMIT_COUNT} uploads per 10 minutes). Please wait before importing again.",
            },
        )
    tenant_times.append(now)
    _tenant_rate_history[tenant_id] = tenant_times


def _probe_redis_available() -> bool:
    try:
        import socket
        import urllib.parse

        from app.core.config import settings

        parsed = urllib.parse.urlparse(settings.REDIS_URL)
        host = parsed.hostname or "127.0.0.1"
        if host == "localhost":
            host = "127.0.0.1"
        port = parsed.port or 6379
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.02)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


_redis_available: bool = _probe_redis_available()


def is_redis_available() -> bool:
    """Fast probe to verify Redis port is reachable before attempting async connection."""
    return _redis_available


async def check_and_record_rate_limit(user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """
    Enforce sliding window rate limits for imports per-user and per-tenant using Redis.
    Falls back to in-memory tracking if Redis is unreachable.
    """
    user_key = f"{USER_RATELIMIT_KEY_PREFIX}{user_id}"
    tenant_key = f"{TENANT_RATELIMIT_KEY_PREFIX}{tenant_id}"

    if is_redis_available():
        try:
            redis = await redis_client.get_redis()
            if redis is not None:
                async with asyncio.timeout(REDIS_OP_TIMEOUT):
                    u_count = await redis.incr(user_key)
                    if u_count == 1:
                        await redis.expire(user_key, USER_RATE_LIMIT_WINDOW)
                    elif u_count > USER_RATE_LIMIT_COUNT:
                        ttl = await redis.ttl(user_key)
                        raise HTTPException(
                            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={
                                "code": "rate_limit_exceeded",
                                "message": f"User upload rate limit exceeded ({USER_RATE_LIMIT_COUNT} uploads per 10 minutes). Please wait before importing again.",
                                "retry_after_seconds": max(ttl, 0),
                            },
                            headers={"Retry-After": str(max(ttl, 0))},
                        )

                    t_count = await redis.incr(tenant_key)
                    if t_count == 1:
                        await redis.expire(tenant_key, TENANT_RATE_LIMIT_WINDOW)
                    elif t_count > TENANT_RATE_LIMIT_COUNT:
                        ttl = await redis.ttl(tenant_key)
                        raise HTTPException(
                            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={
                                "code": "tenant_rate_limit_exceeded",
                                "message": f"Organization upload rate limit exceeded ({TENANT_RATE_LIMIT_COUNT} uploads per 10 minutes). Please wait before importing again.",
                                "retry_after_seconds": max(ttl, 0),
                            },
                            headers={"Retry-After": str(max(ttl, 0))},
                        )
                    return
        except HTTPException:
            raise
        except Exception:
            pass

    _check_and_record_rate_limit_mem(user_id, tenant_id)


@asynccontextmanager
async def _parse_concurrency_guard_mem(tenant_id: uuid.UUID) -> AsyncGenerator[None, None]:
    """In-memory fallback for concurrency caps."""
    global _global_active_parses, _concurrency_lock
    if _concurrency_lock is None:
        _concurrency_lock = asyncio.Lock()

    async with _concurrency_lock:
        if _global_active_parses >= MAX_GLOBAL_CONCURRENT_PARSES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "too_many_concurrent_parses",
                    "message": "The system is currently busy parsing files. Please retry in a few moments.",
                },
            )

        if _tenant_active_parses[tenant_id] >= MAX_TENANT_CONCURRENT_PARSES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "tenant_concurrent_limit_exceeded",
                    "message": "Your organization already has concurrent timetable imports running. Please wait for them to finish.",
                },
            )

        _global_active_parses += 1
        _tenant_active_parses[tenant_id] += 1

    try:
        yield
    finally:
        async with _concurrency_lock:
            _global_active_parses = max(0, _global_active_parses - 1)
            _tenant_active_parses[tenant_id] = max(0, _tenant_active_parses[tenant_id] - 1)
            if _tenant_active_parses[tenant_id] == 0:
                _tenant_active_parses.pop(tenant_id, None)


@asynccontextmanager
async def parse_concurrency_guard(tenant_id: uuid.UUID) -> AsyncGenerator[None, None]:
    """
    Guarantees global and per-tenant concurrency caps during parsing using Redis.
    Uses short TTL (30s) so worker crashes never leave stuck counters.
    Decrements and releases keys in a finally block.
    Falls back to in-memory tracking if Redis is unreachable.
    """
    global_key = GLOBAL_CONCURRENCY_KEY
    tenant_key = f"{TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}"

    redis = None
    if is_redis_available():
        try:
            redis = await redis_client.get_redis()
        except Exception:
            redis = None

    if redis is not None:
        acquired = False
        try:
            async with asyncio.timeout(REDIS_OP_TIMEOUT):
                g_count = await redis.incr(global_key)
                await redis.expire(global_key, CONCURRENCY_KEY_TTL)
                if g_count > MAX_GLOBAL_CONCURRENT_PARSES:
                    await redis.decr(global_key)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={
                            "code": "too_many_concurrent_parses",
                            "message": "The system is currently busy parsing files. Please retry in a few moments.",
                        },
                    )

                t_count = await redis.incr(tenant_key)
                await redis.expire(tenant_key, CONCURRENCY_KEY_TTL)
                if t_count > MAX_TENANT_CONCURRENT_PARSES:
                    await redis.decr(tenant_key)
                    await redis.decr(global_key)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={
                            "code": "tenant_concurrent_limit_exceeded",
                            "message": "Your organization already has concurrent timetable imports running. Please wait for them to finish.",
                        },
                    )

                acquired = True
        except HTTPException:
            raise
        except Exception:
            # Redis operation failed or timed out during acquisition: fallback to in-memory
            redis = None

        if acquired and redis is not None:
            active_redis = redis
            try:
                yield
            finally:
                with suppress(Exception):
                    g_rem = await active_redis.decr(global_key)
                    if g_rem <= 0:
                        await active_redis.delete(global_key)
                with suppress(Exception):
                    t_rem = await active_redis.decr(tenant_key)
                    if t_rem <= 0:
                        await active_redis.delete(tenant_key)
            return

    async with _parse_concurrency_guard_mem(tenant_id):
        yield


# ─── Worker Process Function ──────────────────────────────────────────────────

def _isolated_worker(data: bytes, format_type: str, queue: Any) -> None:
    """
    Isolated worker function running inside a separate multiprocessing.Process.
    Catches all exceptions and sends back structured result through the queue.
    """
    try:
        merged_cells = None
        if format_type == "csv":
            rows = parse_raw_csv(data)
        elif format_type == "xlsx":
            rows, merged_cells = parse_raw_xlsx(data)
        elif format_type == "docx":
            rows = parse_raw_docx(data)
        elif format_type == "pdf":
            rows = parse_raw_pdf(data)
        else:
            queue.put({"success": False, "error_code": "unsupported_format", "error_msg": f"Unsupported format: {format_type}"})
            return

        records = extract_raw_records(rows, merged_cells)
        queue.put({"success": True, "records": records})
    except UploadSecurityError as ue:
        detail = ue.detail if isinstance(ue.detail, dict) else {"message": str(ue.detail)}
        queue.put({
            "success": False,
            "error_code": detail.get("code", "security_error"),
            "error_msg": detail.get("message", str(ue)),
        })
    except Exception as e:
        queue.put({"success": False, "error_code": "parse_failure", "error_msg": f"Failed to parse file: {str(e)}"})


WorkerCallable = Callable[[bytes, str, Any], None]
isolated_worker = _isolated_worker


# ─── Kill-able Process Execution ──────────────────────────────────────────────

async def run_killable_parse(
    data: bytes,
    format_type: str,
    tenant_id: uuid.UUID,
    timeout_seconds: float = 10.0,
    worker_fn: WorkerCallable = _isolated_worker,
    _process_holder: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Execute parsing in a multiprocessing.Process with a strict force-killable timeout.
    Guarantees that hung or malicious processes are forcefully terminated.
    Uses explicit 'spawn' start method on both Linux and Windows to prevent deadlocks
    caused by forking in multi-threaded async event loops.
    """
    async with parse_concurrency_guard(tenant_id):
        # Create a multiprocessing Queue and Process using explicit spawn context
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()

        process = ctx.Process(
            target=worker_fn,
            args=(data, format_type, queue),
        )

        if _process_holder is not None:
            _process_holder.append(process)

        process.start()
        start_time = time.time()
        completed = False

        try:
            while (time.time() - start_time) < timeout_seconds:
                if not process.is_alive():
                    completed = True
                    break
                await asyncio.sleep(0.05)

            if not completed:
                # Force kill the hung process
                with suppress(Exception):
                    process.terminate()

                with suppress(Exception):
                    process.kill()

                # If pid still exists, attempt OS-level kill
                if process.pid:
                    with suppress(Exception):
                        if hasattr(signal, "SIGKILL"):
                            os.kill(process.pid, signal.SIGKILL)

                process.join(timeout=1.0)
                raise UploadSecurityError(
                    code="parse_timeout",
                    message="File parsing exceeded the 10-second timeout and was terminated.",
                    status_code=status.HTTP_408_REQUEST_TIMEOUT,
                )

            process.join(timeout=1.0)

            if queue.empty():
                raise UploadSecurityError(
                    code="worker_crash",
                    message="Parsing process exited unexpectedly without returning data.",
                )

            res = queue.get_nowait()
            if not res.get("success"):
                code = res.get("error_code", "parse_failed")
                msg = res.get("error_msg", "Failed to parse file content.")
                raise UploadSecurityError(code=code, message=msg)

            records_res = res.get("records", [])
            if not isinstance(records_res, list):
                return []
            return records_res

        finally:
            # Guarantee process is cleaned up
            if process.is_alive():
                with suppress(Exception):
                    process.kill()
                with suppress(Exception):
                    process.join(timeout=0.5)
            with suppress(Exception):
                queue.close()
            with suppress(Exception):
                queue.cancel_join_thread()
