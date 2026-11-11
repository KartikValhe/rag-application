"""
app/core/upload_security.py
───────────────────────────
Shared upload validation and security hardening module.
Reusable across Phase 4 (Timetables) and Phase 5 (Document Ingestion).

Provides:
- Streaming upload with strict size limit (hard 2 MB cap)
- Filename sanitization & length truncation
- Magic byte & MIME sniffing (rejects .xls, .xlsm, .docm, executables, scripts)
- Zip bomb & archive safety checks (absolute caps, ratio thresholds, encrypted entry rejection)
- XML entity-expansion defense using defusedxml
- Plain text cell sanitization (control characters stripped, no quotes prefixed on import)
- Formula neutralization for export and template generation
"""

import io
import re
import zipfile
from typing import Any

import defusedxml.common as defused_common  # type: ignore[import-untyped]
import defusedxml.ElementTree as ET  # type: ignore[import-untyped]
from fastapi import HTTPException, UploadFile, status

MAX_UPLOAD_SIZE = 2 * 1024 * 1024  # 2 MB hard cap
MAX_FILENAME_LENGTH = 100

# Archive safety limits
MAX_ZIP_MEMBERS = 200
MAX_ZIP_TOTAL_UNCOMPRESSED = 15 * 1024 * 1024  # 15 MB
MAX_ZIP_MEMBER_UNCOMPRESSED = 5 * 1024 * 1024  # 5 MB
MAX_ZIP_COMPRESSION_RATIO = 50.0  # Threshold for zip bomb detection

# Disallowed magic byte signatures
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Legacy .xls / .doc
EXE_MAGIC_MZ = b"MZ"
ELF_MAGIC = b"\x7fELF"
SHELL_MAGIC = b"#!"

# Macro identifiers in OpenXML packages
MACRO_PARTS = {
    "vbaproject.bin",
    "word/vbaproject.bin",
    "xl/vbaproject.bin",
}

# Control characters to strip from plain text (preserve \t, \n, \r)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

# Dangerous formula starters for CSV/Excel export
FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


class UploadSecurityError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def sanitize_filename(filename: str | None, max_length: int = MAX_FILENAME_LENGTH) -> str:
    """
    Sanitize filename: remove directory traversal, control characters,
    leading dots/spaces, and truncate to max_length.
    """
    if not filename:
        return "uploaded_file"

    # Remove directory paths (POSIX & Windows)
    name = filename.replace("\\", "/").split("/")[-1].strip()

    # Strip control characters
    name = CONTROL_CHARS_RE.sub("", name)

    # Allow alphanumeric, dot, underscore, hyphen, space
    name = re.sub(r"[^\w.\- ]", "_", name)

    # Avoid hidden files / empty names
    name = name.lstrip(". ")
    if not name:
        name = "uploaded_file"

    # Truncate keeping extension intact if possible
    if len(name) > max_length:
        parts = name.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) <= 10:
            ext = "." + parts[1]
            stem = parts[0][: max_length - len(ext)]
            name = stem + ext
        else:
            name = name[:max_length]

    return name


async def stream_upload_with_limit(
    upload_file: UploadFile,
    max_bytes: int = MAX_UPLOAD_SIZE,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """
    Stream upload bytes from FastAPI UploadFile enforcing a hard size limit.
    Stops reading immediately and raises HTTP 413 if max_bytes is exceeded.
    """
    buffer = io.BytesIO()
    total_bytes = 0

    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise UploadSecurityError(
                code="file_too_large",
                message=f"File exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB.",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        buffer.write(chunk)

    return buffer.getvalue()


def sniff_and_validate_file_type(data: bytes, filename: str) -> str:
    """
    Validate file content (magic bytes) against allow-listed formats:
    CSV, XLSX, DOCX, PDF.
    Explicitly rejects OLE2 (.xls, .doc), macro containers (.xlsm, .docm),
    executables, and shell scripts.
    Returns format string: 'csv', 'xlsx', 'docx', or 'pdf'.
    """
    if not data:
        raise UploadSecurityError(code="empty_file", message="Uploaded file is empty.")

    # 1. Reject binary executables and scripts
    if data.startswith(EXE_MAGIC_MZ):
        raise UploadSecurityError(code="disallowed_file_type", message="Executable files (.exe) are strictly prohibited.")
    if data.startswith(ELF_MAGIC):
        raise UploadSecurityError(code="disallowed_file_type", message="Binary executable files are strictly prohibited.")
    if data.startswith(SHELL_MAGIC):
        raise UploadSecurityError(code="disallowed_file_type", message="Script files are strictly prohibited.")

    # 2. Reject legacy OLE2 formats (.xls, .doc)
    if data.startswith(OLE2_MAGIC):
        raise UploadSecurityError(
            code="disallowed_file_type",
            message="Legacy binary formats (.xls, .doc) are not supported. Please save as modern .xlsx, .docx, or .csv.",
        )

    # 3. Check PDF magic
    if data.startswith(b"%PDF-"):
        return "pdf"

    # 4. Check Zip-based formats (XLSX, DOCX)
    if data.startswith(b"PK\x03\x04"):
        return _sniff_openxml_format(data, filename)

    # 5. Check CSV (Plain text / UTF-8 / cp1252)
    # Reject files containing NUL bytes (binary indicators)
    if b"\x00" in data:
        raise UploadSecurityError(
            code="disallowed_file_type",
            message="File contains binary characters and is not a valid CSV or text file.",
        )

    # Verify text decodability
    try:
        data.decode("utf-8-sig")
        return "csv"
    except UnicodeDecodeError:
        try:
            data.decode("cp1252")
            return "csv"
        except UnicodeDecodeError as err:
            raise UploadSecurityError(
                code="disallowed_file_type",
                message="File encoding is not supported. Please upload UTF-8 or CP1252 encoded CSV.",
            ) from err


def _sniff_openxml_format(data: bytes, filename: str) -> str:
    """
    Validate and inspect ZIP-based OpenXML container (XLSX or DOCX).
    Enforces zip safety and macro rejection.
    """
    validate_zip_archive(data)

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            namelist_lower = [name.lower() for name in zf.namelist()]

            # Reject macro-enabled projects
            for macro_part in MACRO_PARTS:
                if macro_part in namelist_lower:
                    raise UploadSecurityError(
                        code="macro_enabled_file",
                        message="Macro-enabled workbooks/documents (.xlsm, .docm) are not allowed.",
                    )

            # Check [Content_Types].xml for macro types
            # Parse key XML package parts with defusedxml to block XXE / entity expansion
            for xml_name in ("[Content_Types].xml", "xl/workbook.xml", "word/document.xml"):
                if xml_name in zf.namelist():
                    xml_data = zf.read(xml_name)
                    try:
                        root = ET.fromstring(xml_data)
                        if xml_name == "[Content_Types].xml":
                            xml_str = ET.tostring(root, encoding="utf-8").decode("utf-8").lower()
                            if "macroenabled" in xml_str or "vba" in xml_str:
                                raise UploadSecurityError(
                                    code="macro_enabled_file",
                                    message="Macro-enabled workbooks/documents (.xlsm, .docm) are not allowed.",
                                )
                    except defused_common.DefusedXmlException as err:
                        raise UploadSecurityError(
                            code="xml_entity_expansion_forbidden",
                            message="XML entity expansion (XXE/billion-laughs) detected and blocked.",
                        ) from err
                    except UploadSecurityError:
                        raise
                    except Exception:
                        pass

            is_xlsx = any(name.startswith("xl/") for name in namelist_lower)
            is_docx = any(name.startswith("word/") for name in namelist_lower)

            if is_xlsx and not is_docx:
                return "xlsx"
            if is_docx and not is_xlsx:
                return "docx"

            # Fallback to extension if structure is ambiguous
            ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
            if ext == "xlsx":
                return "xlsx"
            if ext == "docx":
                return "docx"

            raise UploadSecurityError(
                code="invalid_openxml_format",
                message="File is not a valid Excel (.xlsx) or Word (.docx) document.",
            )

    except zipfile.BadZipFile as err:
        raise UploadSecurityError(code="corrupt_archive", message="Uploaded archive is corrupted or not a valid zip container.") from err


def validate_zip_archive(data: bytes) -> None:
    """
    Perform zip safety and anti-zip-bomb checks:
    - Verifies zip integrity
    - Rejects password-protected / encrypted entries
    - Checks absolute member count cap
    - Checks total uncompressed size cap
    - Checks per-member uncompressed size cap
    - Checks compression ratio threshold
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile as err:
        raise UploadSecurityError(code="corrupt_archive", message="File is not a valid zip archive.") from err

    infolist = zf.infolist()
    if len(infolist) > MAX_ZIP_MEMBERS:
        raise UploadSecurityError(
            code="zip_bomb_detected",
            message=f"Archive contains too many files ({len(infolist)} > {MAX_ZIP_MEMBERS}).",
        )

    total_uncompressed = 0
    for info in infolist:
        # Check encrypted flag (bit 0 of flag_bits)
        if info.flag_bits & 0x1:
            raise UploadSecurityError(
                code="encrypted_file",
                message="Password-protected or encrypted documents are not supported.",
            )

        # Check per-member uncompressed size
        if info.file_size > MAX_ZIP_MEMBER_UNCOMPRESSED:
            raise UploadSecurityError(
                code="zip_member_too_large",
                message=f"Archive member {info.filename} exceeds maximum size of {MAX_ZIP_MEMBER_UNCOMPRESSED // (1024*1024)} MB.",
            )

        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED:
            raise UploadSecurityError(
                code="zip_bomb_detected",
                message=f"Archive total uncompressed size exceeds {MAX_ZIP_TOTAL_UNCOMPRESSED // (1024*1024)} MB.",
            )

        # Check compression ratio for entries larger than 10KB
        if info.compress_size > 0 and info.file_size > 10 * 1024:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_ZIP_COMPRESSION_RATIO:
                raise UploadSecurityError(
                    code="zip_bomb_detected",
                    message=f"Archive member {info.filename} has an abnormally high compression ratio ({ratio:.1f}:1).",
                )


def clean_plain_text(val: Any) -> str:
    """
    Sanitize text read from files during import:
    - Strips control characters
    - Strips leading and trailing whitespace
    - Does NOT prepend quotes (store cleaned plain text)
    """
    if val is None:
        return ""
    text = str(val)
    text = CONTROL_CHARS_RE.sub("", text)
    return text.strip()


def neutralize_formula_cells(val: Any) -> str:
    """
    Neutralize formula-leading characters for export/template generation only:
    If text starts with '=', '+', '-', '@', '\\t', '\\r', prepend single quote "'".
    """
    if val is None:
        return ""
    text = str(val)
    if text.startswith(FORMULA_STARTERS):
        return f"'{text}"
    return text
