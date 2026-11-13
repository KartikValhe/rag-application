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
import logging
import re
import zipfile
from typing import Any
from urllib.parse import quote

import defusedxml.common as defused_common
import defusedxml.ElementTree as ET
from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger(__name__)

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

# OpenXML package types: the main part that proves the type
OPENXML_MAIN_PARTS = {
    "xlsx": "xl/workbook.xml",
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
}

# PDF best-effort limits (the sandboxed subprocess is the real protection)
MAX_PDF_OBJECTS = 20000
MAX_PDF_PAGES = 100
PDF_FORBIDDEN_NAMES = ("JavaScript", "JS", "Launch", "EmbeddedFile", "OpenAction", "AA")
# A PDF name ends at whitespace or a delimiter, so /JSON or /AAA must not match /JS or /AA
_PDF_NAME_END = rb"(?![^\s()<>\[\]{}/%])"
PDF_FORBIDDEN_RE = re.compile(rb"/(?:" + b"|".join(n.encode() for n in PDF_FORBIDDEN_NAMES) + rb")" + _PDF_NAME_END)
PDF_HEX_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
PDF_OBJ_RE = re.compile(rb"\d+\s+\d+\s+obj\b")
PDF_PAGE_RE = re.compile(rb"/Type\s*/Page" + _PDF_NAME_END)

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


def attachment_header(base_name: str, extension: str) -> str:
    """
    Build a safe Content-Disposition value for a download named after user-controlled text.
    The name goes through sanitize_filename, then an ASCII fallback is used for filename=
    and the full name is added as RFC 5987 filename*. No quotes, CR/LF or non-latin-1 bytes
    can reach the header.
    """
    safe = sanitize_filename(base_name, max_length=80).replace(" ", "_")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe)
    utf8_name = quote(f"{safe}.{extension}", safe="")
    return f"attachment; filename=\"{ascii_name}.{extension}\"; filename*=UTF-8''{utf8_name}"


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
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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
        validate_pdf_bytes(data)
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


def _parse_xml(zf: zipfile.ZipFile, name: str) -> Any:
    """Parse one package part with defusedxml. Any failure rejects the file (fail closed)."""
    try:
        return ET.fromstring(zf.read(name))
    except defused_common.DefusedXmlException as err:
        raise UploadSecurityError(
            code="xml_entity_expansion_forbidden",
            message="XML entity expansion (XXE/billion-laughs) detected and blocked.",
        ) from err
    except Exception as err:
        # Log only the exception type: no file names, content or paths
        logger.warning("openxml_part_unparseable error_type=%s", type(err).__name__)
        raise UploadSecurityError(
            code="invalid_xml", message="The document contains malformed or unreadable XML."
        ) from err


def _reject_macro() -> UploadSecurityError:
    return UploadSecurityError(
        code="macro_enabled_file",
        message="Macro-enabled workbooks/documents (.xlsm, .docm, .pptm) are not allowed.",
    )


def _reject_embedded() -> UploadSecurityError:
    return UploadSecurityError(
        code="embedded_content", message="Documents with embedded objects or files are not allowed."
    )


def _check_members(zf: zipfile.ZipFile) -> set[str]:
    """Reject macro parts, embeddings, nested archives and OLE blobs. Returns lower-case part names."""
    names: set[str] = set()
    for info in zf.infolist():
        name = info.filename.replace("\\", "/").lower()
        if name.startswith("/") or ".." in name.split("/"):
            raise UploadSecurityError(code="invalid_openxml_format", message="The archive contains invalid entries.")
        names.add(name)
        if info.is_dir():
            continue
        if name.rsplit("/", 1)[-1] == "vbaproject.bin":
            raise _reject_macro()
        if "/embeddings/" in "/" + name:
            raise _reject_embedded()
        with zf.open(info) as member:
            head = member.read(8)
        if head.startswith(b"PK\x03\x04") or head.startswith(OLE2_MAGIC):
            raise _reject_embedded()
    return names


def _sniff_openxml_format(data: bytes, filename: str) -> str:
    """
    Validate a ZIP-based OpenXML container (XLSX, DOCX, PPTX).
    The type comes from the CONTENT; the extension must agree with it.
    Every XML check fails closed.
    """
    validate_zip_archive(data)

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            names = _check_members(zf)

            if "[content_types].xml" not in names:
                raise UploadSecurityError(code="invalid_openxml_format", message="File is not a valid Office document.")
            real = {i.filename.lower(): i.filename for i in zf.infolist()}
            ct_root = _parse_xml(zf, real["[content_types].xml"])
            for el in ct_root.iter():
                content_type = (el.get("ContentType") or "").lower()
                if "macroenabled" in content_type or "vbaproject" in content_type:
                    raise _reject_macro()

            # Relationship parts must parse and must not point outside the package
            for lower, original in real.items():
                if lower.endswith(".rels"):
                    for rel in _parse_xml(zf, original).iter():
                        # Only plain hyperlinks may be external; the target is never fetched
                        rel_type = (rel.get("Type") or "").rstrip("/").lower()
                        is_hyperlink = rel_type.endswith("/relationships/hyperlink")
                        if (rel.get("TargetMode") or "").lower() == "external" and not is_hyperlink:
                            raise UploadSecurityError(
                                code="external_relationship",
                                message="Documents with links to external resources are not allowed.",
                            )

            detected = [kind for kind, part in OPENXML_MAIN_PARTS.items() if part in names]
            if len(detected) != 1:
                raise UploadSecurityError(
                    code="invalid_openxml_format",
                    message="File is not a valid Excel (.xlsx), Word (.docx) or PowerPoint (.pptx) document.",
                )
            kind = detected[0]
            # The main part must parse too (this also blocks entity expansion in it)
            _parse_xml(zf, real[OPENXML_MAIN_PARTS[kind]])

            ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
            if ext != kind:
                raise UploadSecurityError(
                    code="file_type_mismatch",
                    message="The file extension does not match the file content.",
                )
            return kind

    except zipfile.BadZipFile as err:
        raise UploadSecurityError(
            code="corrupt_archive", message="Uploaded archive is corrupted or not a valid zip container."
        ) from err


def validate_pdf_bytes(data: bytes) -> None:
    """
    Best-effort scan of the raw PDF bytes for active content and excessive size.
    It cannot see inside compressed object streams; the real protection is the sandboxed parser.
    """
    # Decode #xx name escapes first, so /J#61vaScript is seen as /JavaScript
    plain = PDF_HEX_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 16)]), data)
    if PDF_FORBIDDEN_RE.search(plain):
        raise UploadSecurityError(
            code="pdf_active_content",
            message="PDFs with scripts, launch actions or embedded files are not allowed.",
        )
    if len(PDF_OBJ_RE.findall(plain)) > MAX_PDF_OBJECTS or len(PDF_PAGE_RE.findall(plain)) > MAX_PDF_PAGES:
        raise UploadSecurityError(code="pdf_too_complex", message="The PDF is too large or complex to process.")


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
            message="Archive contains too many files.",
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
                message=f"An archive member exceeds the maximum size of {MAX_ZIP_MEMBER_UNCOMPRESSED // (1024*1024)} MB.",
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
                    message="An archive member has an abnormally high compression ratio.",
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
