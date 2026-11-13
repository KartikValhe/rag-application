"""
Real-parse coverage and crash logging for the sandboxed timetable child.

The parse tests run generated xlsx, docx and PDF files through the REAL production child
(default child module) via run_killable_parse. On Linux the child's RLIMIT_AS / RLIMIT_CPU
limits apply automatically; on Windows they are skipped but the real parse still runs.
"""

import io
import logging
import uuid

import openpyxl
import pytest
from docx import Document

from app.core.upload_security import UploadSecurityError
from app.services import timetable_parser_runner as tpr

CHILD = "tests._tt_children"

HEADER = ["Day", "Start", "End", "Subject", "Teacher", "Room"]
ROWS = [
    ["Monday", "09:00", "10:00", "Mathematics", "Ada Lovelace", "R101"],
    ["Tuesday", "10:00", "11:00", "Physics", "Marie Curie", "R202"],
]


def _xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx() -> bytes:
    doc = Document()
    table = doc.add_table(rows=1 + len(ROWS), cols=len(HEADER))
    for c, text in enumerate(HEADER):
        table.cell(0, c).text = text
    for r, row in enumerate(ROWS, start=1):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pdf() -> bytes:
    """A one-page text PDF with a ruled table, built by hand (no extra dependency)."""
    all_rows = [HEADER, *ROWS]
    x0, col_w, row_h, top = 40, 80, 24, 700
    n_cols, n_rows = len(HEADER), len(all_rows)
    ops: list[str] = ["0.5 w"]
    for i in range(n_rows + 1):
        y = top - i * row_h
        ops.append(f"{x0} {y} m {x0 + n_cols * col_w} {y} l S")
    for j in range(n_cols + 1):
        x = x0 + j * col_w
        ops.append(f"{x} {top} m {x} {top - n_rows * row_h} l S")
    ops.append("BT /F1 9 Tf")
    for i, row in enumerate(all_rows):
        for j, text in enumerate(row):
            ops.append(f"1 0 0 1 {x0 + j * col_w + 4} {top - i * row_h - 15} Tm ({text}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.mark.parametrize(
    ("fmt", "builder"),
    [("xlsx", _xlsx), ("docx", _docx), ("pdf", _pdf)],
)
async def test_real_child_parses_generated_file(fmt: str, builder: "object") -> None:
    data = builder()  # type: ignore[operator]
    records = await tpr.run_killable_parse(data, fmt, uuid.uuid4())
    assert len(records) == len(ROWS)
    assert [r["subject_raw"] for r in records] == ["Mathematics", "Physics"]
    assert [r["teacher_raw"] for r in records] == ["Ada Lovelace", "Marie Curie"]
    assert [r["room_raw"] for r in records] == ["R101", "R202"]
    assert [r["day_of_week"] for r in records] == [0, 1]
    assert records[0]["start_time_raw"] == "09:00"
    assert records[0]["end_time_raw"] == "10:00"


async def test_abnormal_exit_is_logged_server_side_but_client_message_is_fixed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=tpr.logger.name), pytest.raises(UploadSecurityError) as exc:
        await tpr.run_killable_parse(b"", "boom", uuid.uuid4(), child_module=CHILD)
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "worker_crash"
    assert exc.value.detail["message"] == "Parsing process exited unexpectedly without returning data."
    text = caplog.text
    assert "exit_code=7" in text
    assert "signal=none" in text
    assert "error_type=ZeroDivisionError" in text
    # Free text from the child's stderr, including paths, must never be logged or returned
    assert "private" not in text
    assert "file.xlsx" not in text
    assert "private" not in str(exc.value.detail)


def test_describe_exit_reports_signal_name() -> None:
    assert tpr._describe_exit(-15, b"") == ("-15", "SIGTERM", "unknown")
    assert tpr._describe_exit(1, b"junk child_error_type=MemoryError\n") == ("1", "none", "MemoryError")
    assert tpr._describe_exit(1, b"child_error_type=../../etc/passwd") == ("1", "none", "unknown")
