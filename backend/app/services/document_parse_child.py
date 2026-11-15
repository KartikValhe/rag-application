"""
app/services/document_parse_child.py
────────────────────────────────────
Entry point of the sandboxed document child: `python -m app.services.document_parse_child`.
Started only through app/core/sandbox.py by the ingestion worker (app/workers/stages.py).

Step 5b-2 scope (backlog B37): re-run the shared upload validation (the DOCUMENT profile of
upload_security.inspect_upload, including the PDF byte scan) on the stored bytes HERE, where a
runaway scan can be killed (wall clock everywhere; memory and CPU limits and the parent-death
signal on Linux, applied by sandbox_child.run before this code runs). The API route keeps its own
validation. The child reports the page count the checks saw (PDF: uncompressed page objects;
PPTX: slides). Real text extraction (pypdfium2, python-docx, python-pptx) comes in step 5b-3.

Protocol (sandbox_child.run): one JSON line of parameters, then the file bytes, on stdin:
  {"file_kind": "pdf", "max_file_bytes": ..., "max_pdf_pages": ..., "max_pptx_slides": ...}
One JSON object on stdout:
  {"success": true, "file_kind": "pdf", "page_count": 12}
  {"success": false, "error_code": "pdf_active_content"}

This module must not import app.core.config or anything that needs secrets.
"""

from typing import Any

from app.core.sandbox_child import run

FILE_KINDS = ("pdf", "docx", "pptx", "txt", "md")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def handle(params: dict[str, Any], data: bytes) -> dict[str, Any]:
    from app.core.upload_security import UploadProfile, UploadSecurityError, inspect_upload

    kind = params.get("file_kind")
    if kind not in FILE_KINDS:
        return {"success": False, "error_code": "unsupported_format"}
    try:
        # The stored version already passed the route's checks; its kind decides the extension, so
        # bytes that turned into something else are a file_type_mismatch here as well.
        found = inspect_upload(
            data,
            f"document.{kind}",
            UploadProfile.DOCUMENT,
            max_file_bytes=_int_or_none(params.get("max_file_bytes")),
            max_pdf_pages=_int_or_none(params.get("max_pdf_pages")),
            max_pptx_slides=_int_or_none(params.get("max_pptx_slides")),
        )
    except UploadSecurityError as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return {"success": False, "error_code": str(detail.get("code", "parse_failed"))}
    if found.kind != kind:
        return {"success": False, "error_code": "file_type_mismatch"}
    return {"success": True, "file_kind": found.kind, "page_count": found.page_count}


if __name__ == "__main__":
    run(handle)
