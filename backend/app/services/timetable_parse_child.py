"""
app/services/timetable_parse_child.py
─────────────────────────────────────
Entry point of the fresh parse subprocess: `python -m app.services.timetable_parse_child <format>`.

Contract with the runner (timetable_parser_runner.py):
- the uploaded file bytes arrive on stdin (raw, no framing);
- one JSON document is written to stdout (nothing else may reach stdout);
- on Linux, the parent-death signal, then memory and CPU limits are applied before any parser
  code is imported (shared helpers in app/core/sandbox_child.py).

This module must not import app.core.config or anything that needs secrets: the
runner starts the child with a minimal environment.
"""

import json
import sys
from typing import Any

from app.core import sandbox_child

# Address space and CPU limits (Linux only). The runner also enforces a 10 s wall clock.
MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024  # ~1 GB
MAX_CPU_SECONDS = 15


def apply_resource_limits() -> None:
    """Apply RLIMIT_AS and RLIMIT_CPU. Skipped where the resource module does not exist (Windows)."""
    sandbox_child.apply_resource_limits(MAX_ADDRESS_SPACE_BYTES, MAX_CPU_SECONDS)


def _parse(data: bytes, format_type: str) -> dict[str, Any]:
    from app.core.upload_security import UploadSecurityError
    from app.services.timetable_importer import (
        extract_raw_records,
        parse_raw_csv,
        parse_raw_docx,
        parse_raw_pdf,
        parse_raw_xlsx,
    )

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
            return {"success": False, "error_code": "unsupported_format"}
        return {"success": True, "records": extract_raw_records(rows, merged_cells)}
    except UploadSecurityError as ue:
        detail: dict[str, Any] = ue.detail if isinstance(ue.detail, dict) else {}
        return {"success": False, "error_code": detail.get("code", "security_error")}
    except Exception as e:
        # Only the exception class name leaves the child; the runner logs it and the client
        # gets a fixed message. The text of the exception may contain paths or library details.
        return {"success": False, "error_code": "parse_failure", "error_type": type(e).__name__}


def main() -> None:
    try:
        _main()
    except BaseException as exc:
        # Only the class name is reported, for the runner's server-side log
        sys.stderr.write(f"child_error_type={type(exc).__name__}\n")
        sys.stderr.flush()
        sys.exit(1)


def _main() -> None:
    sandbox_child.set_non_dumpable()  # Linux: the parser cannot read its parent's environment or memory
    sandbox_child.set_parent_death_signal()  # Linux: a killed API process cannot orphan the parser
    apply_resource_limits()
    # Library prints must never corrupt the JSON result
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    data = sys.stdin.buffer.read()
    result = _parse(data, sys.argv[1] if len(sys.argv) > 1 else "")
    real_stdout.write(json.dumps(result))
    real_stdout.flush()


if __name__ == "__main__":
    main()
