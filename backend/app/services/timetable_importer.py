"""
app/services/timetable_importer.py
───────────────────────────────────
Core parsing, normalization, layout detection, and entity matching
for Timetable file imports (CSV, XLSX, DOCX, text-based PDF).

Strict rules:
- Pure plain-text extraction (no prepended quotes on import)
- Caps: 500 rows, 20 XLSX columns, 20 PDF pages, 5 DOCX tables
- Rejection of scanned / raster-only PDFs (< 30 characters across document)
- List and Grid layout detection
- Flexible time parsing (12h/24h, combined intervals, Excel time objects, Period N labels)
- Exact single-entity matching for subjects and teachers (no guessing / fuzzy matching)
- Formula neutralization for exported CSV/XLSX templates
"""

import csv
import datetime
import io
import re
from typing import Any

import openpyxl
import pdfplumber
from docx import Document

from app.core.upload_security import (
    UploadSecurityError,
    clean_plain_text,
    neutralize_formula_cells,
)

# ─── Synonyms and Dictionaries ────────────────────────────────────────────────

WEEKDAYS_MAP: dict[str, int] = {
    # Full names
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    # Abbreviations
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

DAY_NAMES: dict[int, str] = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

SYNONYMS: dict[str, set[str]] = {
    "weekday": {"weekday", "day", "day of week", "days", "day_of_week"},
    "start_time": {"start", "start time", "start_time", "from", "start-time", "begin", "starts"},
    "end_time": {"end", "end time", "end_time", "to", "end-time", "ends"},
    "time": {"time", "times", "time slot", "slot", "period", "duration", "hours", "timing"},
    "subject": {"subject", "subject code", "subject_code", "course", "course code", "code", "paper", "subject name"},
    "teacher": {"teacher", "teacher email", "teacher_email", "faculty", "instructor", "lecturer", "professor", "email", "staff"},
    "room": {"room", "hall", "venue", "classroom", "lab", "location", "room no", "room_number"},
    "note": {"note", "notes", "remark", "remarks", "comment", "comments", "description"},
    "class": {"class", "section", "cohort", "batch", "class name", "grade"},
}

PERIOD_RE = re.compile(r"^(?:period|slot|session|p)\s*(\d+)$", re.IGNORECASE)
TIME_RANGE_RE = re.compile(r"[-–—]|(?:\s+to\s+)|(?:\s+till\s+)", re.IGNORECASE)

# ─── Time and Weekday Parsing ─────────────────────────────────────────────────

def parse_weekday(val: Any) -> int | None:
    """Normalize weekday from integer, full name, or abbreviation to 0..6 (0=Monday)."""
    if val is None:
        return None
    if isinstance(val, int) and 0 <= val <= 6:
        return val

    cleaned = clean_plain_text(val).lower()
    if cleaned.isdigit():
        idx = int(cleaned)
        if 0 <= idx <= 6:
            return idx
        return None

    return WEEKDAYS_MAP.get(cleaned)


def parse_time_str(val: Any) -> str | None:
    """
    Parse a time representation into normalized 'HH:MM' string.
    Supports:
    - datetime.time / datetime.datetime
    - float (Excel day fraction: 0.375 -> 09:00)
    - 12-hour strings ('9:00 AM', '01:30 pm', '9 AM')
    - 24-hour strings ('09:00', '14:30', '9:00')
    """
    if val is None:
        return None

    if isinstance(val, datetime.time):
        return f"{val.hour:02d}:{val.minute:02d}"

    if isinstance(val, datetime.datetime):
        return f"{val.hour:02d}:{val.minute:02d}"

    if isinstance(val, (int, float)) and not isinstance(val, bool) and 0.0 <= val <= 1.0:
        # Excel fraction of 24h day (0.0 .. 1.0)
        total_minutes = int(round(val * 24 * 60))
        hours = (total_minutes // 60) % 24
        minutes = total_minutes % 60
        return f"{hours:02d}:{minutes:02d}"

    text = clean_plain_text(val).strip()
    if not text:
        return None

    # Check 12-hour AM/PM format
    m12 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", text, re.IGNORECASE)
    if m12:
        hour = int(m12.group(1))
        minute = int(m12.group(2) or 0)
        meridiem = m12.group(3).lower()
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"

    # Check 24-hour HH:MM format
    m24 = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m24:
        hour = int(m24.group(1))
        minute = int(m24.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    return None


def split_time_range(val: str) -> tuple[str | None, str | None]:
    """
    Split a combined time interval cell (e.g. '09:00-10:00', '9:00 AM - 10:30 AM')
    into normalized (start_time, end_time) tuple.
    """
    cleaned = clean_plain_text(val)
    parts = TIME_RANGE_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2:
        start = parse_time_str(parts[0])
        end = parse_time_str(parts[1])
        return start, end
    return None, None


# ─── Raw Extraction Parsers ───────────────────────────────────────────────────

def parse_raw_csv(data: bytes) -> list[list[str]]:
    """Parse raw table grid from CSV data using stdlib csv module with UTF-8 BOM, UTF-8, and CP1252 fallback."""
    if b"\x00" in data:
        raise UploadSecurityError("nul_byte_in_csv", "CSV file contains forbidden NUL bytes.")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as err:
            raise UploadSecurityError("invalid_encoding", "CSV file encoding not supported (must be UTF-8 or CP1252).") from err

    reader = csv.reader(io.StringIO(text))
    rows: list[list[str]] = []
    for row in reader:
        if len(rows) >= 500:
            break
        cleaned_row = [clean_plain_text(cell)[:500] for cell in row[:20]]
        if any(cleaned_row):
            rows.append(cleaned_row)
    return rows


def parse_raw_xlsx(data: bytes) -> tuple[list[list[Any]], set[tuple[int, int]]]:
    """
    Parse raw cell matrix from the first worksheet of an XLSX file using openpyxl.
    Strictly caps at 20 columns and 500 rows even if the file declares massive dimensions.
    Returns (rows, merged_cells_set).
    """
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    if not wb.worksheets:
        raise UploadSecurityError("empty_workbook", "Workbook contains no sheets.")

    ws = wb.worksheets[0]
    merged_cells: set[tuple[int, int]] = set()
    for rng in list(ws.merged_cells.ranges)[:500]:
        for r in range(rng.min_row, min(rng.max_row, 500) + 1):
            for c in range(rng.min_col, min(rng.max_col, 20) + 1):
                # 0-indexed coordinates
                merged_cells.add((r - 1, c - 1))

    rows: list[list[Any]] = []
    max_r = min(ws.max_row or 0, 500)
    max_c = min(ws.max_column or 0, 20)

    for r_idx in range(1, max_r + 1):
        row_vals = []
        for c_idx in range(1, max_c + 1):
            cell_val = ws.cell(row=r_idx, column=c_idx).value
            row_vals.append(cell_val)
        if any(v is not None and str(v).strip() != "" for v in row_vals):
            rows.append(row_vals)

    return rows, merged_cells


def parse_raw_docx(data: bytes) -> list[list[str]]:
    """Extract table rows from DOCX document, capped at 5 tables and 500 rows."""
    doc = Document(io.BytesIO(data))
    rows: list[list[str]] = []
    for table in doc.tables[:5]:
        for r in table.rows:
            if len(rows) >= 500:
                break
            cells = [clean_plain_text(c.text)[:500] for c in r.cells[:20]]
            if any(cells):
                rows.append(cells)
    return rows


def parse_raw_pdf(data: bytes) -> list[list[str]]:
    """
    Extract table rows from a text-based PDF using pdfplumber.
    Rejects scanned/raster-only PDFs (< 30 characters) and password-protected PDFs.
    Capped at 20 pages and 500 total rows.
    """
    rows: list[list[str]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:20]
            total_chars = sum(len(p.chars) for p in pages)
            if total_chars < 30:
                raise UploadSecurityError(
                    code="scanned_pdf",
                    message="This PDF contains scanned images or no selectable text. Please use the CSV/XLSX template or manual entry.",
                )

            for p in pages:
                tables = p.extract_tables() or []
                for t in tables:
                    for r in t:
                        if len(rows) >= 500:
                            break
                        cells = [clean_plain_text(c or "")[:500] for c in r[:20]]
                        if any(cells):
                            rows.append(cells)
    except UploadSecurityError:
        raise
    except Exception as exc:
        err_msg = f"{exc!s} {exc!r}".lower()
        if "password" in err_msg or "encrypt" in err_msg or "authenticate" in err_msg:
            raise UploadSecurityError(
                code="password_protected_pdf",
                message="Password-protected PDFs cannot be imported. Please provide an unencrypted PDF or use the CSV/XLSX template.",
            ) from exc
        raise UploadSecurityError("corrupt_pdf", f"Could not read PDF document: {exc}") from exc

    return rows


# ─── Layout Detection: List vs Grid ───────────────────────────────────────────

def _detect_list_headers(header_row: list[Any]) -> dict[str, int] | None:
    """Check if header_row contains column headings matching our synonyms."""
    mapping: dict[str, int] = {}
    for col_idx, cell in enumerate(header_row):
        val = clean_plain_text(cell).lower()
        if not val:
            continue
        for field, synonyms in SYNONYMS.items():
            if val in synonyms and field not in mapping:
                mapping[field] = col_idx

    # If at least 2 distinct timetable fields matched (e.g. weekday and subject or time)
    if len(mapping) >= 2 and ("weekday" in mapping or "time" in mapping or "start_time" in mapping):
        return mapping
    return None


def _detect_grid_layout(rows: list[list[Any]]) -> tuple[list[tuple[int, int]], int] | None:
    """
    Detect if rows form a weekly grid:
    - A header row has 3 or more weekday column names (Monday..Sunday)
    - Returns (list of (col_idx, weekday_int), header_row_idx)
    """
    for r_idx, row in enumerate(rows[:5]):
        weekday_cols: list[tuple[int, int]] = []
        for c_idx, cell in enumerate(row):
            w = parse_weekday(cell)
            if w is not None:
                weekday_cols.append((c_idx, w))
        if len(weekday_cols) >= 3:
            return weekday_cols, r_idx
    return None


def extract_raw_records(
    rows: list[list[Any]],
    merged_coords: set[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """
    Analyze extracted matrix and convert to normalized intermediate row dictionaries.
    Handles List format and Grid format automatically.
    """
    if not rows:
        return []

    merged = merged_coords or set()

    # 1. Try Grid Layout Detection
    grid_res = _detect_grid_layout(rows)
    if grid_res is not None:
        weekday_cols, header_r_idx = grid_res
        records: list[dict[str, Any]] = []

        for r_idx in range(header_r_idx + 1, len(rows)):
            row = rows[r_idx]
            if not row:
                continue

            # First column is usually the time slot or period
            slot_cell = clean_plain_text(row[0]) if len(row) > 0 else ""
            start_t, end_t = split_time_range(slot_cell)
            period_label = None
            if not start_t and PERIOD_RE.match(slot_cell):
                period_label = slot_cell

            for c_idx, w_day in weekday_cols:
                if c_idx >= len(row):
                    continue
                cell_val = clean_plain_text(row[c_idx])
                if not cell_val:
                    continue

                is_merged = (r_idx, c_idx) in merged

                # Grid cell often contains multiline or comma separated info: Subject \n Teacher \n Room
                lines = [ln.strip() for ln in cell_val.replace("\r", "").split("\n") if ln.strip()]
                subject_raw = lines[0] if len(lines) > 0 else None
                teacher_raw = lines[1] if len(lines) > 1 else None
                room_raw = lines[2] if len(lines) > 2 else None

                records.append({
                    "weekday_raw": DAY_NAMES[w_day],
                    "day_of_week": w_day,
                    "time_raw": slot_cell,
                    "start_time_raw": start_t,
                    "end_time_raw": end_t,
                    "period_label": period_label,
                    "subject_raw": subject_raw,
                    "teacher_raw": teacher_raw,
                    "room_raw": room_raw,
                    "note_raw": None,
                    "class_raw": None,
                    "merged_cell": is_merged,
                    "raw_cell": cell_val,
                })
        return records

    # 2. Try List Layout Detection
    # Look for header row in first 3 rows
    header_mapping = None
    data_start_idx = 0
    for r_idx in range(min(3, len(rows))):
        mapping = _detect_list_headers(rows[r_idx])
        if mapping:
            header_mapping = mapping
            data_start_idx = r_idx + 1
            break

    # If no explicit header found, default to canonical order:
    # weekday, start_time, end_time, subject, teacher, room, note
    if not header_mapping:
        header_mapping = {
            "weekday": 0,
            "start_time": 1,
            "end_time": 2,
            "subject": 3,
            "teacher": 4,
            "room": 5,
            "note": 6,
        }
        data_start_idx = 0

    records = []
    for r_idx in range(data_start_idx, len(rows)):
        row = rows[r_idx]
        if not any(row):
            continue

        def get_col(field: str, current_row: list[Any]) -> Any:
            idx = header_mapping.get(field)
            if idx is not None and idx < len(current_row):
                return current_row[idx]
            return None

        w_raw = get_col("weekday", row)
        s_raw = get_col("start_time", row)
        e_raw = get_col("end_time", row)
        t_slot = get_col("time", row)
        subj = get_col("subject", row)
        teach = get_col("teacher", row)
        rm = get_col("room", row)
        nt = get_col("note", row)
        cls_val = get_col("class", row)

        start_t = parse_time_str(s_raw)
        end_t = parse_time_str(e_raw)
        period_label = None

        if (not start_t or not end_t) and t_slot:
            split_s, split_e = split_time_range(str(t_slot))
            start_t = start_t or split_s
            end_t = end_t or split_e
            if not start_t and PERIOD_RE.match(clean_plain_text(t_slot)):
                period_label = clean_plain_text(t_slot)

        if not start_t and s_raw and PERIOD_RE.match(clean_plain_text(s_raw)):
            period_label = clean_plain_text(s_raw)

        is_merged = any((r_idx, c) in merged for c in range(len(row)))

        records.append({
            "weekday_raw": clean_plain_text(w_raw) if w_raw is not None else None,
            "day_of_week": parse_weekday(w_raw),
            "time_raw": clean_plain_text(t_slot) if t_slot is not None else None,
            "start_time_raw": start_t or clean_plain_text(s_raw),
            "end_time_raw": end_t or clean_plain_text(e_raw),
            "period_label": period_label,
            "subject_raw": clean_plain_text(subj) if subj is not None else None,
            "teacher_raw": clean_plain_text(teach) if teach is not None else None,
            "room_raw": clean_plain_text(rm)[:50] if rm is not None else None,
            "note_raw": clean_plain_text(nt)[:200] if nt is not None else None,
            "class_raw": clean_plain_text(cls_val) if cls_val is not None else None,
            "merged_cell": is_merged,
            "raw_cell": None,
        })

    return records


# ─── Entity Matching & Preview Generation ─────────────────────────────────────

def match_and_validate_records(
    records: list[dict[str, Any]],
    target_class_id: Any,
    target_class_name: str,
    class_subjects: list[Any],  # list of Subject DB models
    teacher_assignments: list[Any],  # list of TeacherAssignment with joined User
) -> dict[str, Any]:
    """
    Perform exact entity matching, constraint verification, and build preview rows.
    - Matches subject by normalized code or name (exact single match only).
    - Matches teacher by email or full name (exact single match only).
    - Checks target class consistency.
    - Identifies periods needing time definitions.
    """
    # Build lookup maps for subjects
    # Subject matching: exact code match (case-insensitive) OR exact name match (case-insensitive)
    code_to_subject: dict[str, Any] = {}
    name_to_subject: dict[str, Any] = {}
    for s in class_subjects:
        if s.code:
            code_to_subject[s.code.strip().upper()] = s
        if s.name:
            name_to_subject[s.name.strip().lower()] = s

    # Build teacher lookups (assigned to this class)
    # Map (subject_id, email) -> user and (subject_id, name) -> user
    email_to_teacher_by_subject: dict[tuple[Any, str], Any] = {}
    name_to_teacher_by_subject: dict[tuple[Any, str], Any] = {}
    all_assigned_teachers_by_email: dict[str, Any] = {}
    all_assigned_teachers_by_name: dict[str, Any] = {}

    for item in teacher_assignments:
        if hasattr(item, "__getitem__"):
            try:
                ta = item[0]
                user = item[1]
            except (IndexError, KeyError, TypeError):
                ta = item
                user = getattr(ta, "user", None)
        else:
            ta = item
            user = getattr(ta, "user", None)
        if not user or not ta:
            continue
        email_clean = user.email.strip().lower()
        name_clean = user.full_name.strip().lower()
        email_to_teacher_by_subject[(ta.subject_id, email_clean)] = user
        name_to_teacher_by_subject[(ta.subject_id, name_clean)] = user
        all_assigned_teachers_by_email[email_clean] = user
        all_assigned_teachers_by_name[name_clean] = user

    parsed_rows: list[dict[str, Any]] = []
    total_valid = 0
    total_errors = 0
    period_labels_found: set[str] = set()
    requires_period_times = False

    for idx, rec in enumerate(records):
        row_errors: list[str] = []
        row_warnings: list[str] = []

        # 1. Weekday validation
        day_of_week = rec.get("day_of_week")
        if day_of_week is None:
            raw_w = rec.get("weekday_raw") or "empty"
            row_errors.append(f"INVALID_WEEKDAY: '{raw_w}' is not a recognized day.")

        # 2. Time validation
        start_time = rec.get("start_time_raw")
        end_time = rec.get("end_time_raw")
        period_label = rec.get("period_label")
        requires_time_for_row = False

        if period_label:
            period_labels_found.add(period_label)
            if not start_time or not end_time:
                requires_time_for_row = True
                requires_period_times = True
                row_warnings.append(f"PERIOD_NEEDS_TIMES: '{period_label}' requires start/end times.")

        if not requires_time_for_row:
            if not start_time or not end_time:
                row_errors.append("MISSING_TIME: Start and end time are required.")
            else:
                try:
                    s_t = datetime.time.fromisoformat(start_time)
                    e_t = datetime.time.fromisoformat(end_time)
                    if e_t <= s_t:
                        row_errors.append(f"INVALID_TIME_ORDER: End time '{end_time}' must be after start '{start_time}'.")
                except ValueError:
                    row_errors.append(f"INVALID_TIME_FORMAT: '{start_time} - {end_time}' is not valid HH:MM.")

        # 3. Class validation (Requirement 8: one class per import; mismatch becomes error)
        class_raw = rec.get("class_raw")
        if class_raw:
            cleaned_cls = clean_plain_text(class_raw).lower()
            if cleaned_cls and cleaned_cls != target_class_name.lower():
                row_errors.append(f"CLASS_MISMATCH: Row indicates '{class_raw}', but importing into '{target_class_name}'.")

        # 4. Subject matching
        subject_raw = rec.get("subject_raw")
        matched_subject = None
        if not subject_raw:
            row_errors.append("MISSING_SUBJECT: Subject code or name is required.")
        else:
            subj_clean = clean_plain_text(subject_raw)
            # Try code first, then name
            matched_subject = code_to_subject.get(subj_clean.upper()) or name_to_subject.get(subj_clean.lower())
            if not matched_subject:
                row_errors.append(f"UNKNOWN_SUBJECT: '{subj_clean}' is not linked to this class.")

        # 5. Teacher matching
        teacher_raw = rec.get("teacher_raw")
        matched_teacher = None
        if teacher_raw:
            teach_clean = clean_plain_text(teacher_raw)
            # Check by email or full name
            subj_id = matched_subject.id if matched_subject else None

            # First check if assigned to this specific subject
            if subj_id:
                matched_teacher = (
                    email_to_teacher_by_subject.get((subj_id, teach_clean.lower()))
                    or name_to_teacher_by_subject.get((subj_id, teach_clean.lower()))
                )

            # If not matched to this subject, check if assigned to the class at all
            if not matched_teacher:
                general_match = (
                    all_assigned_teachers_by_email.get(teach_clean.lower())
                    or all_assigned_teachers_by_name.get(teach_clean.lower())
                )
                if general_match:
                    if matched_subject:
                        row_errors.append(
                            f"TEACHER_NOT_ASSIGNED_TO_SUBJECT: '{teach_clean}' is assigned to class, but not for '{matched_subject.name}'."
                        )
                    matched_teacher = general_match
                else:
                    row_errors.append(f"UNKNOWN_TEACHER: '{teach_clean}' is not assigned to this class.")

        # 6. Flag merged cells
        if rec.get("merged_cell"):
            row_warnings.append("MERGED_CELL: Entry was extracted from a merged cell.")

        if row_errors:
            total_errors += 1
        else:
            total_valid += 1

        parsed_rows.append({
            "row_index": idx,
            "day_of_week": day_of_week,
            "day_name": DAY_NAMES.get(day_of_week, "") if day_of_week is not None else None,
            "start_time": start_time,
            "end_time": end_time,
            "subject_id": matched_subject.id if matched_subject else None,
            "subject_code": matched_subject.code if matched_subject else None,
            "subject_name": matched_subject.name if matched_subject else None,
            "teacher_id": matched_teacher.id if matched_teacher else None,
            "teacher_email": matched_teacher.email if matched_teacher else None,
            "teacher_name": matched_teacher.full_name if matched_teacher else None,
            "room": rec.get("room_raw"),
            "note": rec.get("note_raw"),
            "raw_data": {
                "weekday": rec.get("weekday_raw"),
                "time": rec.get("time_raw"),
                "subject": rec.get("subject_raw"),
                "teacher": rec.get("teacher_raw"),
                "room": rec.get("room_raw"),
                "note": rec.get("note_raw"),
                "class": rec.get("class_raw"),
            },
            "period_label": period_label,
            "requires_period_time": requires_time_for_row,
            "merged_cell": rec.get("merged_cell", False),
            "errors": row_errors,
            "warnings": row_warnings,
        })

    return {
        "total_rows": len(records),
        "valid_rows": total_valid,
        "error_rows": total_errors,
        "requires_period_times": requires_period_times,
        "period_labels": sorted(period_labels_found),
        "rows": parsed_rows,
    }


# ─── Template Generation (with Formula Neutralization) ─────────────────────────

def generate_csv_template(
    class_name: str,
    subjects: list[Any],
    teachers: list[Any],
) -> str:
    """
    Generate downloadable CSV template pre-populated with headers and sample rows.
    Neutralizes formula-leading characters on all database values.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(["weekday", "start_time", "end_time", "subject_code", "teacher_email", "room", "note"])

    # Sample rows using actual class subjects & teachers
    if subjects and teachers:
        for i, s in enumerate(subjects[:3]):
            t = teachers[i % len(teachers)]
            writer.writerow([
                DAY_NAMES[i % 5],
                f"{9 + i:02d}:00",
                f"{10 + i:02d}:00",
                neutralize_formula_cells(s.code),
                neutralize_formula_cells(t.email),
                neutralize_formula_cells(f"Room {101 + i}"),
                neutralize_formula_cells(f"{s.name} lecture"),
            ])
    else:
        # Generic instruction row
        writer.writerow(["Monday", "09:00", "10:00", "SUBJ101", "teacher@example.com", "Room 101", "Sample Note"])

    return output.getvalue()


def generate_xlsx_template(
    class_name: str,
    subjects: list[Any],
    teachers: list[Any],
) -> bytes:
    """
    Generate downloadable XLSX template with headers, sample rows, and valid codes sheet.
    Neutralizes formula-leading characters on all database values.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Timetable Template"

    headers = ["weekday", "start_time", "end_time", "subject_code", "teacher_email", "room", "note"]
    ws.append(headers)

    # Style header row
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = openpyxl.styles.Font(bold=True)

    if subjects and teachers:
        for i, s in enumerate(subjects[:3]):
            t = teachers[i % len(teachers)]
            ws.append([
                DAY_NAMES[i % 5],
                f"{9 + i:02d}:00",
                f"{10 + i:02d}:00",
                neutralize_formula_cells(s.code),
                neutralize_formula_cells(t.email),
                neutralize_formula_cells(f"Room {101 + i}"),
                neutralize_formula_cells(f"{s.name} lecture"),
            ])
    else:
        ws.append(["Monday", "09:00", "10:00", "SUBJ101", "teacher@example.com", "Room 101", "Sample Note"])

    # Reference sheet with valid subjects & teachers for this class
    ws_ref = wb.create_sheet(title="Valid Subjects & Teachers")
    ws_ref.append(["SUBJECT CODE", "SUBJECT NAME"])
    for s in subjects:
        ws_ref.append([neutralize_formula_cells(s.code), neutralize_formula_cells(s.name)])

    ws_ref.append([])
    ws_ref.append(["TEACHER NAME", "TEACHER EMAIL"])
    for t in teachers:
        ws_ref.append([neutralize_formula_cells(t.full_name), neutralize_formula_cells(t.email)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
