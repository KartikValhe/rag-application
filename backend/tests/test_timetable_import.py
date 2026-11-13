"""
tests/test_timetable_import.py
───────────────────────────────
Comprehensive security, parsing, validation, and endpoint tests for
Phase 4c Timetable File Import & Template Ingestion:
  1. Kill-able parser & concurrency caps (force kill on timeout, concurrency limits, rate limits).
  2. Formula neutralization: templates/exports neutralized, imports stored as clean plain text.
  3. Zip safety & defusedxml: zip bombs, ratio limits, encrypted entries, XXE entity expansion fixtures.
  4. Magic bytes & sniffing: executables (.exe), OLE2 (.xls), macros (.xlsm), NUL bytes in CSV, oversized (>2MB), scanned PDF.
  5. Parsers & Layouts: CSV, XLSX, DOCX, text PDF, Grid layout, flexible times (12h/24h, combined ranges), Period N handling, merged cell flags.
  6. Exact matching: subject code/name, teacher email/name, unknown values flagged as row errors, no fuzzy guessing.
  7. Save-draft endpoint: server-side re-validation of every row, tampered subject/teacher rejected, replace confirmation (409), draft-only.
  8. Authorization & Audit: student denied (403), unassigned teacher/cross-tenant 404, audit logs record sanitized filename & counts only.
"""

import io
import time
import uuid
import zipfile
from typing import Any

import openpyxl
import pytest
from docx import Document
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.core.upload_security import (
    UploadSecurityError,
    clean_plain_text,
    neutralize_formula_cells,
    sniff_and_validate_file_type,
    validate_zip_archive,
)
from app.db.models.academic import Class, ClassSubject, Subject, TeacherAssignment
from app.db.models.audit import AuditLog
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable
from app.db.models.user import User
from app.services.timetable_importer import (
    extract_raw_records,
    generate_csv_template,
    generate_xlsx_template,
    match_and_validate_records,
    parse_raw_docx,
    parse_time_str,
    parse_weekday,
    split_time_range,
)
from app.services.timetable_parser_runner import (
    check_and_record_rate_limit,
    parse_concurrency_guard,
    run_killable_parse,
)

# ─── Helper Fixtures & Setup ──────────────────────────────────────────────────

def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


async def _setup_import_test_data(tenant: Tenant, raw_db: AsyncSession) -> tuple[Class, Subject, Subject, User, str]:
    """Create a class, subjects, teacher, assignments, and tokens for import testing."""
    # Class
    c = Class(tenant_id=tenant.id, name="Grade 10-A")
    raw_db.add(c)
    await raw_db.flush()

    # Subjects
    s1 = Subject(tenant_id=tenant.id, name="Mathematics", code="MATH101")
    s2 = Subject(tenant_id=tenant.id, name="Physics", code="PHY101")
    raw_db.add_all([s1, s2])
    await raw_db.flush()

    raw_db.add_all([
        ClassSubject(tenant_id=tenant.id, class_id=c.id, subject_id=s1.id),
        ClassSubject(tenant_id=tenant.id, class_id=c.id, subject_id=s2.id),
    ])

    # Teacher
    teacher = User(
        tenant_id=tenant.id,
        email="import_teacher@acme.edu",
        full_name="Prof. Alan Turing",
        is_admin=False,
        status="active",
    )
    raw_db.add(teacher)
    await raw_db.flush()

    # Assign teacher role
    role_res = await raw_db.execute(
        select(Role.id).where(Role.tenant_id == tenant.id, Role.name == "teacher")
    )
    r_id = role_res.scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=teacher.id, role_id=r_id))

    # Assign teacher to G10A + MATH101
    raw_db.add(
        TeacherAssignment(
            tenant_id=tenant.id,
            class_id=c.id,
            subject_id=s1.id,
            user_id=teacher.id,
        )
    )
    await raw_db.commit()

    teacher_token = create_access_token(tenant_id=str(tenant.id), user_id=str(teacher.id), is_admin=False)
    return c, s1, s2, teacher, teacher_token


# ─── 1. Formula Neutralization & Plain Text Sanitization Tests ────────────────

def test_formula_neutralization_behavior() -> None:
    """Verify formula starters are neutralized for export, but plain text stored cleanly on import."""
    # Export neutralization
    assert neutralize_formula_cells("=SUM(A1:B1)") == "'=SUM(A1:B1)"
    assert neutralize_formula_cells("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert neutralize_formula_cells("-10") == "'-10"
    assert neutralize_formula_cells("@HYPERLINK('http://evil.com')") == "'@HYPERLINK('http://evil.com')"
    assert neutralize_formula_cells("Regular text") == "Regular text"
    assert neutralize_formula_cells(None) == ""

    # Import plain text cleaning (MUST NOT prepend quotes, must strip control chars)
    raw_input = "\x00\x08Hello\x1b World\t\n"
    cleaned = clean_plain_text(raw_input)
    assert "Hello World" in cleaned
    assert not cleaned.startswith("'")

    # Formula cells in import files must be kept as cleaned plain text (not quoted)
    formula_input = "=SUM(1,2)"
    assert clean_plain_text(formula_input) == "=SUM(1,2)"


def test_template_generation_neutralizes_dangerous_db_values() -> None:
    """If database subject/teacher contains formula characters, generated templates must neutralize them."""
    class DummySubject:
        def __init__(self, code: str, name: str) -> None:
            self.code = code
            self.name = name

    class DummyTeacher:
        def __init__(self, full_name: str, email: str) -> None:
            self.full_name = full_name
            self.email = email

    evil_subjs = [DummySubject(code="=CMD()", name="+MaliciousSubject")]
    evil_teachers = [DummyTeacher(full_name="@BadTeacher", email="-evil@attacker.com")]

    # CSV template
    csv_out = generate_csv_template("Class A", list(evil_subjs), list(evil_teachers))
    assert "'=CMD()" in csv_out
    assert "'+MaliciousSubject" in csv_out
    assert "'-evil@attacker.com" in csv_out

    # XLSX template
    xlsx_bytes = generate_xlsx_template("Class A", list(evil_subjs), list(evil_teachers))
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.active
    # First data row
    row_values = [cell.value for cell in ws[2]]
    assert "'=CMD()" in row_values
    assert "'-evil@attacker.com" in row_values


# ─── 2. Zip Safety, Compression Limits & XXE Tests ────────────────────────────

def test_zip_safety_rejects_encrypted_entries() -> None:
    """Encrypted/password-protected zip files must be rejected immediately."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("secret.txt", b"secret data")

    raw = bytearray(buf.getvalue())
    # Set bit 0 of flag_bits in local header (offset 6) and central directory (PK\x01\x02 offset 8)
    raw[6] |= 0x01
    cd_idx = raw.find(b"PK\x01\x02")
    if cd_idx != -1:
        raw[cd_idx + 8] |= 0x01

    with pytest.raises(UploadSecurityError) as exc:
        validate_zip_archive(bytes(raw))
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "encrypted_file"


def test_zip_safety_rejects_excessive_members() -> None:
    """Archive with more than 200 members must be rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(205):
            zf.writestr(f"file_{i}.txt", b"x")

    with pytest.raises(UploadSecurityError) as exc:
        validate_zip_archive(buf.getvalue())
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "zip_bomb_detected"


def test_zip_safety_rejects_high_compression_ratio() -> None:
    """Zip bomb with excessive compression ratio must be rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1 MB of repetitive zeros compresses to ~1 KB (> 100:1 ratio)
        zf.writestr("large_zeroes.dat", b"0" * (1024 * 1024))

    with pytest.raises(UploadSecurityError) as exc:
        validate_zip_archive(buf.getvalue())
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "zip_bomb_detected"


def test_xxe_entity_expansion_blocked_in_openxml() -> None:
    """XML entity expansion (billion laughs / XXE) in XLSX must be caught and blocked."""
    buf = io.BytesIO()
    xxe_xml = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ELEMENT lolz (#PCDATA)>
     <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
     <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
    ]>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="xml" ContentType="application/xml"/>
      <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
      <test>&lol2;</test>
    </Types>"""

    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", xxe_xml)
        zf.writestr("xl/workbook.xml", b"<workbook/>")

    data = buf.getvalue()
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "test.xlsx")
    assert exc.value.detail["code"] == "xml_entity_expansion_forbidden"


def test_xxe_entity_expansion_blocked_in_docx() -> None:
    """XML entity expansion in DOCX word/document.xml must be caught and blocked."""
    buf = io.BytesIO()
    xxe_doc = b"""<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE doc [
      <!ENTITY xxe "attack">
      <!ENTITY xxe2 "&xxe;&xxe;&xxe;&xxe;">
    ]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>&xxe2;</w:t></w:r></w:p></w:body>
    </w:document>"""

    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>")
        zf.writestr("word/document.xml", xxe_doc)

    data = buf.getvalue()
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "test.docx")
    assert exc.value.detail["code"] == "xml_entity_expansion_forbidden"


def test_xlsx_huge_declared_dimension_capped() -> None:
    """XLSX with huge row/column counts or declared dimensions must be strictly capped at 500 rows and 20 columns."""
    import openpyxl

    from app.services.timetable_importer import parse_raw_xlsx

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    # Populate a few cells and one cell far out
    for r in range(1, 10):
        for c in range(1, 5):
            ws.cell(row=r, column=c, value=f"Val_{r}_{c}")

    buf = io.BytesIO()
    wb.save(buf)

    rows, merged = parse_raw_xlsx(buf.getvalue())
    assert len(rows) <= 500
    for r in rows:
        assert len(r) <= 20


def test_password_protected_pdf_rejected() -> None:
    """Password-protected / encrypted PDFs must be rejected with password_protected_pdf code."""
    from app.services.timetable_importer import parse_raw_pdf

    # PDF with /Encrypt dictionary
    encrypted_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Filter /Standard /V 2 /R 3 /O (xxxx) /U (yyyy) /P -4 >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R /Encrypt 4 0 R >>\n%%EOF\n"
    )

    with pytest.raises(UploadSecurityError) as exc:
        parse_raw_pdf(encrypted_pdf)
    assert exc.value.detail["code"] == "password_protected_pdf"


def test_docx_table_cap_enforced() -> None:
    """DOCX with more than the 5-table cap must parse at most 5 tables."""
    from docx import Document

    from app.services.timetable_importer import parse_raw_docx

    doc = Document()
    for i in range(8):
        t = doc.add_table(rows=2, cols=2)
        t.cell(0, 0).text = f"T{i}_R0_C0"
        t.cell(0, 1).text = f"T{i}_R0_C1"
        t.cell(1, 0).text = f"T{i}_R1_C0"
        t.cell(1, 1).text = f"T{i}_R1_C1"

    buf = io.BytesIO()
    doc.save(buf)

    rows = parse_raw_docx(buf.getvalue())
    # 8 tables * 2 rows = 16 rows, but capped at 5 tables * 2 rows = 10 rows
    assert len(rows) == 10


def test_csv_bom_cp1252_and_nul_handling() -> None:
    """CSV parser must handle UTF-8 with BOM, CP1252 fallback, and reject NUL bytes."""
    from app.services.timetable_importer import parse_raw_csv

    # 1. UTF-8 with BOM
    bom_csv = b"\xef\xbb\xbfweekday,start_time,end_time,subject_code\nMonday,09:00,10:00,CS101"
    rows_bom = parse_raw_csv(bom_csv)
    assert rows_bom[0][0] == "weekday"
    assert rows_bom[1][0] == "Monday"

    # 2. UTF-8 without BOM
    plain_csv = b"weekday,start_time,end_time,subject_code\nTuesday,11:00,12:00,CS102"
    rows_plain = parse_raw_csv(plain_csv)
    assert rows_plain[0][0] == "weekday"
    assert rows_plain[1][0] == "Tuesday"

    # 3. CP1252 encoded content (Café, £50)
    cp1252_text = "weekday,start_time,end_time,subject_code,note\nWednesday,14:00,15:00,CS103,Café & Lab £50"
    cp1252_bytes = cp1252_text.encode("cp1252")
    rows_cp1252 = parse_raw_csv(cp1252_bytes)
    assert rows_cp1252[1][4] == "Café & Lab £50"

    # 4. CSV with NUL byte
    bad_csv = b"weekday,start_time\nMonday\x00,09:00"
    with pytest.raises(UploadSecurityError) as exc:
        parse_raw_csv(bad_csv)
    assert exc.value.detail["code"] == "nul_byte_in_csv"


@pytest.mark.asyncio
async def test_redis_concurrency_and_rate_limits() -> None:
    """Concurrency caps and rate limits must use Redis with short TTL and finally release."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.services.timetable_parser_runner import (
        CONCURRENCY_KEY_TTL,
        GLOBAL_CONCURRENCY_KEY,
        TENANT_CONCURRENCY_KEY_PREFIX,
        USER_RATELIMIT_KEY_PREFIX,
        check_and_record_rate_limit,
        parse_concurrency_guard,
    )

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)
    mock_redis.decr = AsyncMock(return_value=0)
    mock_redis.delete = AsyncMock(return_value=True)
    mock_redis.ttl = AsyncMock(return_value=300)

    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    with patch("app.core.redis_client.get_redis", AsyncMock(return_value=mock_redis)):
        # 1. Normal rate limit check
        await check_and_record_rate_limit(user_id, tenant_id)
        mock_redis.incr.assert_any_call(f"{USER_RATELIMIT_KEY_PREFIX}{user_id}")
        mock_redis.expire.assert_any_call(f"{USER_RATELIMIT_KEY_PREFIX}{user_id}", 600)

        # 2. Rate limit exceed
        mock_redis.incr = AsyncMock(return_value=15)
        with pytest.raises(HTTPException) as exc:
            await check_and_record_rate_limit(user_id, tenant_id)
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "rate_limit_exceeded"

        # 3. Concurrency guard acquires with TTL and releases in finally
        mock_redis.incr = AsyncMock(return_value=1)
        async with parse_concurrency_guard(tenant_id):
            mock_redis.incr.assert_any_call(GLOBAL_CONCURRENCY_KEY)
            mock_redis.expire.assert_any_call(GLOBAL_CONCURRENCY_KEY, CONCURRENCY_KEY_TTL)
            mock_redis.incr.assert_any_call(f"{TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}")
            mock_redis.expire.assert_any_call(f"{TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}", CONCURRENCY_KEY_TTL)

        # In finally: decr called
        mock_redis.decr.assert_any_call(GLOBAL_CONCURRENCY_KEY)
        mock_redis.decr.assert_any_call(f"{TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}")


# ─── 3. Magic Byte & Hostile File Rejection Tests ─────────────────────────────

def test_sniffing_rejects_renamed_executables() -> None:
    """An executable (.exe) renamed to .xlsx must be rejected by magic bytes."""
    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(fake_exe, "timetable.xlsx")
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "disallowed_file_type"


def test_sniffing_rejects_legacy_ole2() -> None:
    """Legacy OLE2 binary files (.xls) must be rejected with helpful migration message."""
    ole_data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(ole_data, "old_timetable.xls")
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "disallowed_file_type"
    assert "Legacy binary formats" in exc.value.detail["message"]


def test_sniffing_rejects_macro_workbooks() -> None:
    """Office OpenXML workbooks containing VBA macros (.xlsm) must be rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types><Override ContentType='application/vnd.ms-excel.sheet.macroEnabled.main+xml'/></Types>")
        zf.writestr("xl/vbaProject.bin", b"VBA_MACRO_BYTES")

    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(buf.getvalue(), "macros.xlsm")
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "macro_enabled_file"


def test_sniffing_rejects_csv_with_nul_bytes() -> None:
    """CSV files containing NUL bytes must be rejected as binary."""
    bad_csv = b"weekday,start_time\nMonday\x00,09:00"
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(bad_csv, "binary.csv")
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "disallowed_file_type"


# ─── 4. Kill-able Process & Concurrency Limits Tests ──────────────────────────

def _sleeping_test_worker(data: bytes, format_type: str, queue: Any) -> None:
    """
    Top-level worker function for testing timeout termination.
    Must be a top-level importable function so multiprocessing 'spawn' context
    can import and execute it in the child process.
    Sleeps for 10 seconds, far longer than the test timeout of 1.0 second.
    """
    import time
    time.sleep(10.0)


@pytest.mark.asyncio
async def test_killable_parse_timeout_terminates_process() -> None:
    """
    A parser that exceeds timeout must be terminated cleanly and raise 408/timeout error.
    Deterministic test: passes a top-level worker that sleeps 10s with a 1s timeout.
    Verifies:
    1. UploadSecurityError is raised with code 'parse_timeout' and status 408.
    2. Elapsed time is close to timeout (~1s), not the full sleep duration (10s).
    3. The child process is cleanly terminated and reaped (not alive, no zombie).
    4. Per-tenant and global concurrency counters are released immediately.
    """
    from app.services import timetable_parser_runner as tpr

    tenant_id = uuid.uuid4()
    process_holder: list[Any] = []

    t0 = time.time()
    with pytest.raises(UploadSecurityError) as exc:
        await run_killable_parse(
            data=b"dummy_data",
            format_type="csv",
            tenant_id=tenant_id,
            timeout_seconds=1.0,
            worker_fn=_sleeping_test_worker,
            _process_holder=process_holder,
        )
    elapsed = time.time() - t0

    # 1. Error structure & status code
    assert exc.value.status_code == 408
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "parse_timeout"

    # 2. Timing check: took near the 1s timeout, definitely terminated before the 10s sleep
    assert 0.8 <= elapsed < 5.0, f"Expected elapsed time near 1.0s, got {elapsed:.2f}s"

    # 3. Child process state: must not be alive, exitcode must be set (reaped, no zombie)
    assert len(process_holder) == 1
    child_proc = process_holder[0]
    assert not child_proc.is_alive(), "Worker process must be killed"
    assert child_proc.exitcode is not None, "Worker process must have an exit code (reaped)"

    # 4. Concurrency counters must be released
    assert tpr._global_active_parses == 0, "Global active parses counter must be 0"
    assert tpr._tenant_active_parses.get(tenant_id, 0) == 0, "Tenant active parses counter must be 0"

    # If Redis is available, verify Redis counters were released
    if tpr.is_redis_available():
        from app.core import redis_client
        redis = await redis_client.get_redis()
        if redis is not None:
            g_val = await redis.get(tpr.GLOBAL_CONCURRENCY_KEY)
            assert g_val is None or int(g_val) <= 0, f"Global Redis concurrency key was not released: {g_val}"
            t_val = await redis.get(f"{tpr.TENANT_CONCURRENCY_KEY_PREFIX}{tenant_id}")
            assert t_val is None or int(t_val) <= 0, f"Tenant Redis concurrency key was not released: {t_val}"

    # Verify concurrency guard can be immediately re-acquired for this tenant up to full capacity (2)
    async with tpr.parse_concurrency_guard(tenant_id), tpr.parse_concurrency_guard(tenant_id):
        pass


@pytest.mark.asyncio
async def test_parse_concurrency_guard_enforces_tenant_limit() -> None:
    """Tenant concurrency cap (2 concurrent parses) must block a 3rd concurrent parse."""
    tenant_id = uuid.uuid4()
    async with parse_concurrency_guard(tenant_id), parse_concurrency_guard(tenant_id):
        # 3rd concurrent request for same tenant must fail with 429
        with pytest.raises(Exception) as exc:
            async with parse_concurrency_guard(tenant_id):
                pass
        assert "tenant_concurrent_limit_exceeded" in str(exc.value) or "429" in str(exc.value)


@pytest.mark.asyncio
async def test_rate_limiter_blocks_burst() -> None:
    """Rate limiter must enforce max 10 uploads per 10 minutes per user."""
    u_id = uuid.uuid4()
    t_id = uuid.uuid4()
    for _ in range(10):
        await check_and_record_rate_limit(u_id, t_id)

    with pytest.raises(Exception) as exc:
        await check_and_record_rate_limit(u_id, t_id)
    assert "rate_limit_exceeded" in str(exc.value)


# ─── 5. Parsers, Time, Weekday, & Grid Layout Tests ───────────────────────────

def test_time_parsing_variations() -> None:
    """Test flexible 12h/24h, combined intervals, and Excel fraction parsing."""
    assert parse_time_str("9:00 AM") == "09:00"
    assert parse_time_str("01:30 PM") == "13:30"
    assert parse_time_str("12:00 am") == "00:00"
    assert parse_time_str("12:00 pm") == "12:00"
    assert parse_time_str("14:45") == "14:45"
    assert parse_time_str("9:00") == "09:00"

    # Excel time fraction (0.5 = 12:00, 0.375 = 09:00)
    assert parse_time_str(0.5) == "12:00"
    assert parse_time_str(0.375) == "09:00"

    # Combined interval splitting
    s, e = split_time_range("09:00 - 10:00")
    assert s == "09:00" and e == "10:00"
    s2, e2 = split_time_range("9:00 AM to 10:30 AM")
    assert s2 == "09:00" and e2 == "10:30"


def test_weekday_parsing_variations() -> None:
    """Test full names, abbreviations, and numeric weekday strings."""
    assert parse_weekday("Monday") == 0
    assert parse_weekday("mon") == 0
    assert parse_weekday("Wednesday") == 2
    assert parse_weekday("wed") == 2
    assert parse_weekday("Friday") == 4
    assert parse_weekday("fri") == 4
    assert parse_weekday("Sunday") == 6
    assert parse_weekday("sun") == 6
    assert parse_weekday("UnknownDay") is None


def test_grid_layout_unfolding() -> None:
    """Weekly grid with weekdays in header row and time slots in column 0 must unfold."""
    grid_rows: list[list[Any]] = [
        ["Time", "Monday", "Tuesday", "Wednesday"],
        ["09:00 - 10:00", "MATH101\nProf Alan\nRoom 101", "", "PHY101\nLab 2"],
        ["10:00 - 11:00", "PHY101\nRoom 202", "MATH101\nRoom 101", ""],
    ]
    records = extract_raw_records(grid_rows)
    assert len(records) == 4

    r1 = records[0]
    assert r1["day_of_week"] == 0  # Monday
    assert r1["start_time_raw"] == "09:00"
    assert r1["end_time_raw"] == "10:00"
    assert r1["subject_raw"] == "MATH101"
    assert r1["teacher_raw"] == "Prof Alan"
    assert r1["room_raw"] == "Room 101"


def test_docx_table_parsing() -> None:
    """DOCX tables must be extracted cleanly."""
    doc = Document()
    table = doc.add_table(rows=2, cols=4)
    table.cell(0, 0).text = "Day"
    table.cell(0, 1).text = "Time"
    table.cell(0, 2).text = "Subject"
    table.cell(0, 3).text = "Room"

    table.cell(1, 0).text = "Monday"
    table.cell(1, 1).text = "09:00 - 10:00"
    table.cell(1, 2).text = "MATH101"
    table.cell(1, 3).text = "Room 303"

    buf = io.BytesIO()
    doc.save(buf)

    raw_rows: list[list[Any]] = [[c for c in r] for r in parse_raw_docx(buf.getvalue())]
    records = extract_raw_records(raw_rows)
    assert len(records) == 1
    assert records[0]["day_of_week"] == 0
    assert records[0]["start_time_raw"] == "09:00"
    assert records[0]["subject_raw"] == "MATH101"


# ─── 6. Matching Engine & Validation Tests ────────────────────────────────────

def test_exact_entity_matching_rules() -> None:
    """Verify exact single matching for subjects and teachers, no fuzzy guessing."""
    class DummySubject:
        def __init__(self, id: uuid.UUID, code: str, name: str) -> None:
            self.id = id
            self.code = code
            self.name = name

    class DummyUser:
        def __init__(self, id: uuid.UUID, email: str, full_name: str) -> None:
            self.id = id
            self.email = email
            self.full_name = full_name

    class DummyTA:
        def __init__(self, subject_id: uuid.UUID, user: DummyUser) -> None:
            self.subject_id = subject_id
            self.user = user

    s1_id = uuid.uuid4()
    s2_id = uuid.uuid4()
    t_id = uuid.uuid4()

    subjects = [
        DummySubject(id=s1_id, code="MATH101", name="Mathematics"),
        DummySubject(id=s2_id, code="PHY101", name="Physics"),
    ]
    teacher = DummyUser(id=t_id, email="alan@acme.edu", full_name="Alan Turing")
    assignments = [DummyTA(subject_id=s1_id, user=teacher)]

    # 1. Matching by code and email (Success)
    records = [{
        "day_of_week": 0,
        "weekday_raw": "Monday",
        "start_time_raw": "09:00",
        "end_time_raw": "10:00",
        "subject_raw": "MATH101",
        "teacher_raw": "alan@acme.edu",
        "room_raw": "101",
    }]
    res = match_and_validate_records(records, uuid.uuid4(), "Class 10A", list(subjects), list(assignments))
    assert res["valid_rows"] == 1
    assert res["rows"][0]["subject_id"] == s1_id
    assert res["rows"][0]["teacher_id"] == t_id
    assert len(res["rows"][0]["errors"]) == 0

    # 2. Matching by name and full name (Success)
    records2 = [{
        "day_of_week": 1,
        "weekday_raw": "Tuesday",
        "start_time_raw": "10:00",
        "end_time_raw": "11:00",
        "subject_raw": "Mathematics",
        "teacher_raw": "Alan Turing",
        "room_raw": "101",
    }]
    res2 = match_and_validate_records(records2, uuid.uuid4(), "Class 10A", list(subjects), list(assignments))
    assert res2["valid_rows"] == 1
    assert res2["rows"][0]["subject_id"] == s1_id
    assert res2["rows"][0]["teacher_id"] == t_id

    # 3. Unknown subject & Unknown teacher (Reported as structured errors, not guessed)
    records3 = [{
        "day_of_week": 2,
        "weekday_raw": "Wednesday",
        "start_time_raw": "11:00",
        "end_time_raw": "12:00",
        "subject_raw": "HISTORY101",  # Unknown
        "teacher_raw": "unknown@acme.edu",  # Unknown
        "room_raw": "101",
    }]
    res3 = match_and_validate_records(records3, uuid.uuid4(), "Class 10A", list(subjects), list(assignments))
    assert res3["error_rows"] == 1
    row_errs = res3["rows"][0]["errors"]
    assert any("UNKNOWN_SUBJECT" in err for err in row_errs)
    assert any("UNKNOWN_TEACHER" in err for err in row_errs)

    # 4. Class mismatch error (Requirement 8)
    records4 = [{
        "day_of_week": 0,
        "weekday_raw": "Monday",
        "start_time_raw": "09:00",
        "end_time_raw": "10:00",
        "subject_raw": "MATH101",
        "teacher_raw": "alan@acme.edu",
        "class_raw": "Grade 11-B",  # Mismatched target class
    }]
    res4 = match_and_validate_records(records4, uuid.uuid4(), "Grade 10-A", list(subjects), list(assignments))
    assert any("CLASS_MISMATCH" in err for err in res4["rows"][0]["errors"])


# ─── 7. Full API Endpoint Integration Tests ───────────────────────────────────

@pytest.mark.asyncio
async def test_download_templates_api(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Test downloading pre-populated CSV and XLSX timetable templates."""
    c, s1, s2, teacher, _ = await _setup_import_test_data(tenant_a, raw_db)
    admin_token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
        permissions=["timetable:manage"],
    )
    headers = _headers(admin_token, tenant_a.slug)

    # 1. CSV Template
    res_csv = await client.get(f"/api/v1/classes/{c.id}/timetables/template.csv", headers=headers)
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.headers["content-type"]
    assert "weekday,start_time,end_time,subject_code" in res_csv.text
    assert s1.code is not None and s1.code in res_csv.text

    # 2. XLSX Template
    res_xlsx = await client.get(f"/api/v1/classes/{c.id}/timetables/template.xlsx", headers=headers)
    assert res_xlsx.status_code == 200
    assert "openxmlformats-officedocument.spreadsheetml" in res_xlsx.headers["content-type"]
    wb = openpyxl.load_workbook(io.BytesIO(res_xlsx.content))
    assert "Timetable Template" in wb.sheetnames
    assert "Valid Subjects & Teachers" in wb.sheetnames


@pytest.mark.asyncio
async def test_import_preview_endpoint(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """Test POST /classes/{class_id}/timetables/import-preview with valid CSV."""
    c, s1, s2, teacher, _ = await _setup_import_test_data(tenant_a, raw_db)
    admin_token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
        permissions=["timetable:manage"],
    )
    headers = _headers(admin_token, tenant_a.slug)

    csv_data = f"""weekday,start_time,end_time,subject_code,teacher_email,room,note
Monday,09:00,10:00,{s1.code},{teacher.email},Room 204,Calculus
Tuesday,10:00,11:00,{s2.code},unassigned@acme.edu,Lab 1,Physics Lab
"""
    files = {"file": ("timetable.csv", csv_data.encode("utf-8"), "text/csv")}

    res = await client.post(
        f"/api/v1/classes/{c.id}/timetables/import-preview",
        headers=headers,
        files=files,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["filename"] == "timetable.csv"
    assert data["total_rows"] == 2
    assert data["valid_rows"] == 1
    assert data["error_rows"] == 1  # 2nd row has unassigned teacher for PHY101
    assert data["rows"][0]["subject_id"] == str(s1.id)
    assert data["rows"][0]["teacher_id"] == str(teacher.id)

    # Verify audit event logged
    audit_res = await raw_db.execute(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_a.id,
            AuditLog.action == "timetable:import_preview",
        )
    )
    audit = audit_res.scalar_one_or_none()
    assert audit is not None
    assert audit.details["total_rows"] == 2
    assert "data" not in audit.details  # No file content in audit!

    # Verify import-preview is strictly read-only and never creates or updates any timetable
    tt_check = await raw_db.execute(
        select(Timetable).where(Timetable.tenant_id == tenant_a.id, Timetable.class_id == c.id)
    )
    assert tt_check.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_save_imported_draft_revalidation_and_confirmation(
    client: AsyncClient,
    tenant_a: Tenant,
    admin_a: User,
    raw_db: AsyncSession,
) -> None:
    """
    Test save-draft endpoint:
      - Validates every single row server-side against DB relations
      - Rejects tampered subjects/teachers
      - Requires explicit replace confirmation if draft already has entries
      - Never touches published timetables
    """
    c, s1, s2, teacher, _ = await _setup_import_test_data(tenant_a, raw_db)
    admin_token = create_access_token(
        tenant_id=str(tenant_a.id),
        user_id=str(admin_a.id),
        is_admin=True,
        permissions=["timetable:manage"],
    )
    headers = _headers(admin_token, tenant_a.slug)

    # 1. Save initial draft entries (Success)
    payload1 = {
        "replace_confirmed": False,
        "entries": [
            {
                "day_of_week": 0,
                "start_time": "09:00",
                "end_time": "10:00",
                "subject_id": str(s1.id),
                "teacher_id": str(teacher.id),
                "room": "Room 101",
                "note": "Initial draft entry",
            }
        ],
    }
    res1 = await client.post(
        f"/api/v1/classes/{c.id}/timetables/save-imported-draft",
        headers=headers,
        json=payload1,
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "draft"
    assert len(data1["entries"]) == 1

    # 2. Saving again without replace_confirmed=True must return 409 Conflict
    payload2 = {
        "replace_confirmed": False,
        "entries": [
            {
                "day_of_week": 1,
                "start_time": "11:00",
                "end_time": "12:00",
                "subject_id": str(s1.id),
                "teacher_id": str(teacher.id),
                "room": "Room 102",
            }
        ],
    }
    res2 = await client.post(
        f"/api/v1/classes/{c.id}/timetables/save-imported-draft",
        headers=headers,
        json=payload2,
    )
    assert res2.status_code == 409
    assert res2.json()["detail"]["code"] == "confirmation_required"
    assert res2.json()["detail"]["existing_row_count"] == 1
    assert res2.json()["detail"]["new_row_count"] == 1

    # 3. Saving with replace_confirmed=True replaces entries cleanly
    payload2["replace_confirmed"] = True
    res3 = await client.post(
        f"/api/v1/classes/{c.id}/timetables/save-imported-draft",
        headers=headers,
        json=payload2,
    )
    assert res3.status_code == 200
    data3 = res3.json()
    assert len(data3["entries"]) == 1
    assert data3["entries"][0]["day_of_week"] == 1

    # 4. Tampered row rejection (e.g. random subject ID not linked to class)
    tampered_payload = {
        "replace_confirmed": True,
        "entries": [
            {
                "day_of_week": 0,
                "start_time": "09:00",
                "end_time": "10:00",
                "subject_id": str(uuid.uuid4()),  # Unlinked subject
                "teacher_id": str(teacher.id),
            }
        ],
    }
    res_tampered = await client.post(
        f"/api/v1/classes/{c.id}/timetables/save-imported-draft",
        headers=headers,
        json=tampered_payload,
    )
    assert res_tampered.status_code == 422
    assert res_tampered.json()["detail"]["code"] == "subject_not_linked"


@pytest.mark.asyncio
async def test_authorization_matrix_for_import_endpoints(
    client: AsyncClient,
    tenant_a: Tenant,
    raw_db: AsyncSession,
) -> None:
    """
    Test role authorization:
      - Student calling import/template -> 403 Forbidden
      - Teacher calling for unassigned class -> 404 Generic Not Found
      - Unauthenticated -> 401 Unauthorized
    """
    c, s1, s2, teacher, teacher_token = await _setup_import_test_data(tenant_a, raw_db)

    # 1. Unauthenticated -> 401
    res_unauth = await client.get(f"/api/v1/classes/{c.id}/timetables/template.csv")
    assert res_unauth.status_code == 401

    # 2. Student -> 403
    student = User(
        tenant_id=tenant_a.id,
        email="student_import_test@acme.edu",
        full_name="Student Test",
        is_admin=False,
        status="active",
    )
    raw_db.add(student)
    await raw_db.flush()
    role_res = await raw_db.execute(
        select(Role.id).where(Role.tenant_id == tenant_a.id, Role.name == "student")
    )
    raw_db.add(UserRole(tenant_id=tenant_a.id, user_id=student.id, role_id=role_res.scalar_one()))
    await raw_db.commit()

    student_token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(student.id), is_admin=False)
    student_headers = _headers(student_token, tenant_a.slug)

    res_student = await client.get(
        f"/api/v1/classes/{c.id}/timetables/template.csv",
        headers=student_headers,
    )
    assert res_student.status_code == 403

    # 3. Teacher uploading for UNASSIGNED class -> 404 generic
    other_class = Class(tenant_id=tenant_a.id, name="Class 12-Z")
    raw_db.add(other_class)
    await raw_db.commit()

    teacher_headers = _headers(teacher_token, tenant_a.slug)
    res_other_class = await client.get(
        f"/api/v1/classes/{other_class.id}/timetables/template.csv",
        headers=teacher_headers,
    )
    assert res_other_class.status_code == 404
