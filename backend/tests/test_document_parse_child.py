"""
The document parse child (app/services/document_parse_child.py, step 5b-2, backlog B37/B33) and its
parent side in the worker (app/workers/stages.validate_in_sandbox).

5b-2 scope: the child re-runs the shared upload validation (DOCUMENT profile, including the PDF
byte scan) on the stored bytes INSIDE the sandbox, where it can be killed, and reports the page
count it saw. Real text extraction comes in 5b-3. Sandbox failures map to the design's codes:
timeout and oversized output are permanent, a crash is transient (worker_crash).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.services.document_limits import default_limits
from app.workers import errors, stages
from tests.security.upload_assertions import CT, CT_XML, make_pdf, make_zip, presentation_xml

FAST = stages.ParseSettings(timeout_seconds=20.0, max_output_bytes=1024 * 1024, memory_limit_mb=1024, cpu_seconds=60)


def _pptx(slides: int) -> bytes:
    return make_zip({CT: CT_XML, "ppt/presentation.xml": presentation_xml(slides)})


async def _validate(data: bytes, kind: str, **limit_overrides: int) -> stages.SandboxValidation:
    limits = default_limits()
    if limit_overrides:
        from dataclasses import replace

        limits = replace(limits, **limit_overrides)
    return await stages.validate_in_sandbox(data, file_kind=kind, limits=limits, parse=FAST)


async def test_valid_text_passes_without_a_page_count() -> None:
    result = await _validate("Unit 1: Algebra\n\nलघुगणक\n".encode(), "txt")
    assert result == stages.SandboxValidation(file_kind="txt", page_count=None)


async def test_pdf_page_count_comes_from_the_sandboxed_scan() -> None:
    result = await _validate(make_pdf(pages=3), "pdf")
    assert result == stages.SandboxValidation(file_kind="pdf", page_count=3)


async def test_pptx_slide_count_is_reported_as_pages() -> None:
    assert (await _validate(_pptx(4), "pptx")).page_count == 4


@pytest.mark.parametrize(
    ("data", "kind", "overrides", "code"),
    [
        (make_pdf(b"/OpenAction 3 0 R /JavaScript (x)"), "pdf", {}, "pdf_active_content"),
        (make_pdf(b"/J#61vaScript (x)"), "pdf", {}, "pdf_active_content"),
        (make_pdf(pages=5), "pdf", {"max_pdf_pages": 4}, "too_many_pages"),
        (_pptx(3), "pptx", {"max_pptx_slides": 2}, "too_many_slides"),
        ("नमस्ते".encode("utf-16"), "txt", {}, "invalid_encoding"),
        (b"plain text, stored as a pdf version", "pdf", {}, "file_type_mismatch"),
        (b"", "txt", {}, "empty_file"),
    ],
)
async def test_hostile_or_over_limit_bytes_fail_permanently(
    data: bytes, kind: str, overrides: dict[str, int], code: str
) -> None:
    with pytest.raises(errors.PermanentIngestError) as exc:
        await _validate(data, kind, **overrides)
    assert exc.value.code == code


async def test_unknown_child_codes_become_parse_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_child(*_a: object, **_k: object) -> bytes:
        return b'{"success": false, "error_code": "C:\\\\secret\\\\path"}'

    monkeypatch.setattr(stages.sandbox, "run_child", fake_run_child)
    with pytest.raises(errors.PermanentIngestError) as exc:
        await _validate(b"x", "txt")
    assert exc.value.code == "parse_failed"


async def test_hung_child_is_a_permanent_parse_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid.txt"
    settings = stages.ParseSettings(timeout_seconds=1.0, max_output_bytes=1024, memory_limit_mb=1024, cpu_seconds=60)
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=default_limits(), parse=settings,
            child_module="tests._sandbox_children", child_args=("sleep",), extra_params={"pid_file": str(pid_file)},
        )
    assert exc.value.code == "parse_timeout"


async def test_crashing_child_is_a_transient_worker_crash() -> None:
    with pytest.raises(errors.TransientIngestError) as exc:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=default_limits(), parse=FAST,
            child_module="tests._sandbox_children", child_args=("boom",),
        )
    assert exc.value.code == "worker_crash"


async def test_oversized_output_is_permanent() -> None:
    settings = stages.ParseSettings(timeout_seconds=10.0, max_output_bytes=10_000, memory_limit_mb=1024, cpu_seconds=60)
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.validate_in_sandbox(
            b"x", file_kind="txt", limits=default_limits(), parse=settings,
            child_module="tests._sandbox_children", child_args=("huge",), extra_params={"size": 50_000},
        )
    assert exc.value.code == "parse_output_too_large"


def test_document_child_does_not_load_the_app_settings() -> None:
    """Importing the child must not import app.core.config (it reads .env and needs secrets)."""
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", "import sys, app.services.document_parse_child; print('app.core.config' in sys.modules)"],
        cwd=str(backend), env={"PYTHONPATH": str(backend), "SYSTEMROOT": "C:\\Windows"} if sys.platform == "win32"
        else {"PYTHONPATH": str(backend)},
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert out.stdout.strip() == "False"
