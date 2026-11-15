"""
Finding 1 of the 5a-3 security review: PDF scan ReDoS and validation on the event loop.

- Each hostile 2 MB payload behind a %PDF- header is accepted or rejected in under 2 s, for the
  timetable AND the document profile. Every run happens in a child process that is killed after
  25 s, so a quadratic regex fails the test instead of hanging it.
- validate_upload runs in a worker thread (validate_upload_async): the event loop keeps serving
  other coroutines while a large validation runs, for both profiles and in the real import route.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import timetable as timetable_api
from app.core import upload_security
from app.core.security import create_access_token
from app.core.upload_security import UploadProfile, UploadSecurityError
from app.db.models.academic import Class
from app.db.models.tenant import Tenant
from app.db.models.user import User
from tests.security.pdf_scan_child import PAYLOADS
from tests.security.upload_assertions import assert_pdf_scan_fast, make_pdf

BLOCK_SECONDS = 1.5  # how long the slowed-down validation blocks its thread
MAX_LOOP_GAP_SECONDS = 0.5  # the loop must tick far more often than that meanwhile


@pytest.mark.parametrize("profile", list(UploadProfile), ids=lambda p: p.value)
@pytest.mark.parametrize("payload", sorted(PAYLOADS))
def test_hostile_pdf_payload_is_scanned_in_linear_time(payload: str, profile: UploadProfile) -> None:
    outcome = assert_pdf_scan_fast(payload, profile)
    assert outcome  # accepted or a fixed error code; only the time matters here


def test_pdf_scan_patterns_have_no_unbounded_quantifiers() -> None:
    """Every regex of the module uses bounded repetition only ({m,n}), never *, + or {m,}."""
    import re

    for name in dir(upload_security):
        value = getattr(upload_security, name)
        if isinstance(value, re.Pattern):
            pattern = value.pattern if isinstance(value.pattern, str) else value.pattern.decode("latin-1")
            # Strip escaped characters and character classes before looking for quantifiers
            stripped = re.sub(r"\\.", "", pattern)
            stripped = re.sub(r"\[[^\]]*\]", "", stripped)
            assert not re.search(r"[*+]|\{\d*,\}", stripped), f"{name} has an unbounded quantifier: {pattern}"


def test_bounded_obj_regex_still_counts_real_objects() -> None:
    """The bounded object regex keeps the object cap working on ordinary PDFs."""
    many = b"%PDF-1.4\n" + b"".join(b"%d 0 obj\n<< >>\nendobj\n" % i for i in range(1, 25000))
    for profile in UploadProfile:
        with pytest.raises(UploadSecurityError) as exc:
            upload_security.validate_upload(many, "a.pdf", profile)
        assert isinstance(exc.value.detail, dict) and exc.value.detail["code"] == "pdf_too_complex"
    pages = make_pdf(pages=150)
    with pytest.raises(UploadSecurityError):
        upload_security.validate_upload(pages, "a.pdf", UploadProfile.TIMETABLE)
    assert upload_security.validate_upload(pages, "a.pdf", UploadProfile.DOCUMENT) == "pdf"
    spaced = b"%PDF-1.4\n" + b"".join(b"%d 0 obj << /Type   /Page >> endobj\n" % i for i in range(1, 160))
    with pytest.raises(UploadSecurityError):
        upload_security.validate_upload(spaced, "a.pdf", UploadProfile.TIMETABLE)


# ─── Event loop stays responsive ─────────────────────────────────────────────

async def _with_loop_gap[T](awaitable: Awaitable[T]) -> tuple[T | BaseException, float]:
    """Run `awaitable` while a ticker measures the longest pause of the event loop."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    result: T | BaseException
    try:
        result = await awaitable
    except BaseException as exc:  # the caller decides which outcome is expected
        result = exc
    finally:
        done.set()
        await task
    return result, max(gaps, default=0.0)


def _slow_validate(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make validate_upload block its thread for BLOCK_SECONDS before doing the real work."""
    real = upload_security.validate_upload
    calls: list[float] = []

    def slow(*args: Any, **kwargs: Any) -> str:
        calls.append(time.perf_counter())
        time.sleep(BLOCK_SECONDS)  # stands in for a long, CPU-bound validation
        return real(*args, **kwargs)

    monkeypatch.setattr(upload_security, "validate_upload", slow)
    # The import route used to import validate_upload by name; patch that copy too if it exists
    monkeypatch.setattr(timetable_api, "validate_upload", slow, raising=False)
    return calls


@pytest.mark.parametrize("profile", list(UploadProfile), ids=lambda p: p.value)
async def test_validate_upload_async_keeps_the_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch, profile: UploadProfile
) -> None:
    validate_async = getattr(upload_security, "validate_upload_async", None)
    assert validate_async is not None, "upload_security has no validate_upload_async helper"
    calls = _slow_validate(monkeypatch)
    big_pdf = make_pdf(pages=50) + b"%" + b" " * (1024 * 1024)
    result, gap = await _with_loop_gap(validate_async(big_pdf, "big.pdf", profile))
    assert calls, "the validation did not run"
    assert result == "pdf", result
    assert gap < MAX_LOOP_GAP_SECONDS, f"the event loop stalled for {gap:.2f} s during validation"


@pytest.fixture
async def import_ctx(tenant_a: Tenant, admin_a: User, raw_db: AsyncSession) -> AsyncIterator[tuple[str, dict[str, str]]]:
    c = Class(tenant_id=tenant_a.id, name="Grade 9")
    raw_db.add(c)
    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True)
    yield f"/api/v1/classes/{c.id}/timetables/import-preview", {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant_a.slug,
    }


async def test_timetable_import_route_validates_off_the_event_loop(
    client: AsyncClient, import_ctx: tuple[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, headers = import_ctx
    calls = _slow_validate(monkeypatch)
    files = {"file": ("t.pdf", b"%PDF-1.4 /JavaScript (x)", "application/pdf")}
    res, gap = await _with_loop_gap(client.post(url, headers=headers, files=files))
    assert calls, "the route did not validate the upload"
    assert not isinstance(res, BaseException), res
    assert res.status_code == 422 and res.json()["detail"]["code"] == "pdf_active_content", res.text
    assert gap < MAX_LOOP_GAP_SECONDS, f"the import route blocked the event loop for {gap:.2f} s"
