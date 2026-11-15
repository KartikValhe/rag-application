"""
Child process for the PDF scan timing tests (tests/security/upload_assertions.assert_pdf_scan_fast).

Usage: python -m tests.security.pdf_scan_child <profile> <payload>
Builds a 2 MB hostile payload behind a %PDF- header, runs validate_upload once and prints one
JSON line: {"elapsed": seconds, "outcome": "accepted" | error code}.

The regexes of the PDF scan are taken from the PDF_SCAN_PATTERNS environment variable (hex of each
pattern), so a prove-it-fails test can switch a protection off in the parent process.
"""

import json
import os
import re
import sys
import time

from app.core import upload_security
from app.core.upload_security import UploadProfile, UploadSecurityError

SIZE = 2 * 1024 * 1024  # total file size, within the 2 MB timetable limit
HEADER = b"%PDF-1.4\n"


def _fill(unit: bytes) -> bytes:
    n = SIZE - len(HEADER)
    return HEADER + (unit * (n // len(unit) + 1))[:n]


def _digit_runs() -> bytes:
    half = (SIZE - len(HEADER) - 1) // 2
    return (HEADER + b"1" * half + b" " + b"1" * half).ljust(SIZE, b"1")


PAYLOADS = {
    "digits": lambda: _fill(b"1"),
    "obj_repeats": lambda: _fill(b"obj "),
    "whitespace": lambda: _fill(b" "),
    "mixed_whitespace": lambda: _fill(b" \t\r\n\x0c"),
    "hash4_escapes": lambda: _fill(b"#4"),
    "hash41_escapes": lambda: _fill(b"#41"),
    "digit_runs_split_by_space": _digit_runs,
    "type_page_whitespace": lambda: _fill(b"/Type" + b" " * 61),
}


def main() -> None:
    profile = UploadProfile(sys.argv[1])
    data = PAYLOADS[sys.argv[2]]()
    assert len(data) == SIZE
    for name, hex_pattern in json.loads(os.environ.get("PDF_SCAN_PATTERNS", "{}")).items():
        setattr(upload_security, name, re.compile(bytes.fromhex(hex_pattern)))
    start = time.perf_counter()
    try:
        upload_security.validate_upload(data, "scan.pdf", profile)
        outcome = "accepted"
    except UploadSecurityError as exc:
        detail = exc.detail
        outcome = str(detail["code"]) if isinstance(detail, dict) else "error"
    elapsed = time.perf_counter() - start
    print(json.dumps({"elapsed": elapsed, "outcome": outcome}), flush=True)


if __name__ == "__main__":
    main()
