"""
app/services/document_parse_child.py
────────────────────────────────────
Entry point of the sandboxed document child: `python -m app.services.document_parse_child`.
Started only through app/core/sandbox.py by the ingestion worker (app/workers/stages.py).

Every request first re-runs the shared upload validation (the DOCUMENT profile of
upload_security.inspect_upload, including the PDF byte scan, backlog B37) on the stored bytes HERE,
where a runaway scan can be killed (wall clock everywhere; memory and CPU limits and the parent-death
signal on Linux, applied by sandbox_child.run before this code runs). Then, by mode:

  "validate" (default, step 5b-2): report the page count the checks saw.
      {"success": true, "file_kind": "pdf", "page_count": 12}
  "parse" (step 5b-3): real text extraction and the quality gates (document_extractors).
      {"success": true, "document": {...ParsedDocument...}, "peak_memory_kb": 51234}
  A failure in either mode:
      {"success": false, "error_code": "pdf_active_content"}

Parameters (one JSON line before the bytes, sandbox_child.read_request):
  {"mode": "parse", "file_kind": "pdf", "max_file_bytes": ..., "max_pdf_pages": ...,
   "max_pptx_slides": ..., "max_text_chars": ..., "time_budget_seconds": ...}
`time_budget_seconds` is a soft deadline checked between pages and blocks: the child then answers
parse_timeout itself; the parent's wall clock still kills it if a single step hangs.

The parent does not trust this output: it validates `document` against app/services/parsed_document.py.
This module must not import app.core.config or anything that needs secrets.
"""

import importlib
import sys
import time
from typing import Any

from app.core.sandbox_child import run

FILE_KINDS = ("pdf", "docx", "pptx", "txt", "md")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def peak_memory_kb() -> int | None:
    """This process's peak resident memory in KiB (Linux, macOS, Windows), or None if unknown."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            get_info = kernel32.K32GetProcessMemoryInfo  # argtypes matter: the handle is pointer-sized
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return None
            return int(counters.PeakWorkingSetSize // 1024)
        if sys.platform.startswith("linux"):
            # VmHWM belongs to this program's memory map. getrusage's ru_maxrss would not do: Linux keeps
            # it across execve, so the child would report the PARENT's peak at the time of the fork.
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1])
        resource = importlib.import_module("resource")  # POSIX only; importlib keeps mypy happy on Windows
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak // 1024) if sys.platform == "darwin" else int(peak)
    except Exception:
        return None


def handle(params: dict[str, Any], data: bytes) -> dict[str, Any]:
    from app.core import upload_security
    from app.core.upload_security import UploadProfile, UploadSecurityError

    kind = params.get("file_kind")
    if kind not in FILE_KINDS:
        return {"success": False, "error_code": "unsupported_format"}
    max_pdf_pages = _int_or_none(params.get("max_pdf_pages"))
    max_pptx_slides = _int_or_none(params.get("max_pptx_slides"))
    try:
        # The stored version already passed the route's checks; its kind decides the extension, so
        # bytes that turned into something else are a file_type_mismatch here as well.
        found = upload_security.inspect_upload(
            data,
            f"document.{kind}",
            UploadProfile.DOCUMENT,
            max_file_bytes=_int_or_none(params.get("max_file_bytes")),
            max_pdf_pages=max_pdf_pages,
            max_pptx_slides=max_pptx_slides,
        )
    except UploadSecurityError as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return {"success": False, "error_code": str(detail.get("code", "parse_failed"))}
    if found.kind != kind:
        return {"success": False, "error_code": "file_type_mismatch"}

    mode = params.get("mode", "validate")
    if mode == "validate":
        return {"success": True, "file_kind": found.kind, "page_count": found.page_count}
    if mode != "parse":
        return {"success": False, "error_code": "parse_failed"}
    return _parse(kind, data, params, max_pdf_pages, max_pptx_slides)


def _parse(
    kind: str, data: bytes, params: dict[str, Any], max_pdf_pages: int | None, max_pptx_slides: int | None
) -> dict[str, Any]:
    from app.core.upload_security import DOC_MAX_PDF_PAGES, DOC_MAX_PPTX_SLIDES
    from app.services.document_extractors import extract_document
    from app.services.document_gates import GateError
    from app.services.parsed_document import DEFAULT_MAX_TEXT_CHARS, MAX_PAGES

    max_text_chars = min(_int_or_none(params.get("max_text_chars")) or DEFAULT_MAX_TEXT_CHARS, DEFAULT_MAX_TEXT_CHARS)
    budget = params.get("time_budget_seconds")
    deadline = (
        time.monotonic() + float(budget)
        if isinstance(budget, int | float) and not isinstance(budget, bool) and budget >= 0
        else None
    )
    try:
        document = extract_document(
            data,
            kind,
            max_pdf_pages=min(max_pdf_pages or DOC_MAX_PDF_PAGES, MAX_PAGES),
            max_pptx_slides=min(max_pptx_slides or DOC_MAX_PPTX_SLIDES, MAX_PAGES),
            max_text_chars=max_text_chars,
            deadline=deadline,
        )
    except GateError as exc:
        return {"success": False, "error_code": exc.code}
    return {"success": True, "document": document, "peak_memory_kb": peak_memory_kb()}


if __name__ == "__main__":
    run(handle)
