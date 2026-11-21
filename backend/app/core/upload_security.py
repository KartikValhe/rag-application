"""
app/core/upload_security.py
───────────────────────────
Shared upload validation and security hardening module.
Reusable across Phase 4 (Timetables) and Phase 5 (Document Ingestion).

Provides:
- Upload profiles (TIMETABLE, DOCUMENT): one validate_upload() for every concept, with
  per-profile formats and archive caps. The per-file size is a parameter, capped by a
  platform ceiling.
- Streaming upload with a strict size limit (2 MB for timetables, tenant limit for documents)
- Filename sanitization & length truncation
- Magic byte & MIME sniffing (rejects .xls, .xlsm, .docm, executables, scripts)
- Zip bomb & archive safety checks (absolute caps, ratio thresholds, encrypted entry rejection)
- XML entity-expansion defense using defusedxml
- Plain text cell sanitization (control characters stripped, no quotes prefixed on import)
- Formula neutralization for export and template generation
"""

import asyncio
import io
import logging
import re
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import quote
from xml.sax.handler import ContentHandler, feature_namespaces

import defusedxml.common as defused_common
import defusedxml.sax as defused_sax
from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger(__name__)

# Called with the (namespace, local name) and the attributes of every element of a streamed XML part
ElementCallback = Callable[[tuple[str | None, str], Any], None]

MAX_UPLOAD_SIZE = 2 * 1024 * 1024  # 2 MB hard cap for timetable imports
# Platform ceiling for document uploads. A tenant's own max_file_bytes (default 10 MB, step 5b) can
# be raised by a superadmin but never above this. It is a constant on purpose: not an env setting.
DOC_UPLOAD_CEILING_BYTES = 50 * 1024 * 1024
MAX_FILENAME_LENGTH = 100

# Archive safety limits (timetable profile)
MAX_ZIP_MEMBERS = 200
MAX_ZIP_TOTAL_UNCOMPRESSED = 15 * 1024 * 1024  # 15 MB
MAX_ZIP_MEMBER_UNCOMPRESSED = 5 * 1024 * 1024  # 5 MB
MAX_ZIP_COMPRESSION_RATIO = 50.0  # Threshold for zip bomb detection

# Archive safety limits (document profile)
DOC_MAX_ZIP_MEMBERS = 1000
DOC_MAX_ZIP_TOTAL_UNCOMPRESSED = 60 * 1024 * 1024  # 60 MB
DOC_MAX_ZIP_MEMBER_UNCOMPRESSED = 25 * 1024 * 1024  # 25 MB
DOC_MAX_ZIP_COMPRESSION_RATIO = 100.0

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
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
# Deepest element nesting accepted in any package part. Real Office parts nest a few dozen levels;
# a part that nests deeper is a memory or recursion bomb for whatever reads it later.
MAX_XML_DEPTH = 256

# PDF best-effort limits. This byte scan is only a first filter: it cannot see inside compressed
# object streams. The real protection arrives with the sandboxed document parser in step 5b.
MAX_PDF_OBJECTS = 20000
MAX_PDF_PAGES = 100  # timetable profile
DOC_MAX_PDF_PAGES = 200  # document profile default; the tenant limit is passed in (step 5b)
DOC_MAX_PPTX_SLIDES = 100
# Names that make a PDF active or able to carry other files. /OpenAction and /AA are NOT here:
# they only name actions, and the sandboxed parser never renders or executes anything.
PDF_ACTIVE_NAMES = ("JavaScript", "JS", "Launch", "RichMedia", "XFA")
PDF_EMBEDDED_NAME = "EmbeddedFile"  # singular only: /EmbeddedFiles is the catalog's name tree key
PDF_FORBIDDEN_NAMES = (*PDF_ACTIVE_NAMES, PDF_EMBEDDED_NAME)
# A PDF name ends at whitespace (NUL, tab, LF, FF, CR, space) or one of ()<>[]{}/%, so /JSON must
# not match /JS, and /JavaScript followed by a NUL byte must still match. The \s of a bytes pattern
# has no NUL, so it is added by hand. (/EmbeddedFiles, /EmbeddedFileX: the name goes on, no match.)
_PDF_NAME_END = rb"(?![^\s\x00()<>\[\]{}/%])"
# Every regex here runs over attacker-controlled bytes (up to 50 MB), so each one uses BOUNDED
# repetition only. An unbounded pattern such as \d+\s+\d+ backtracks quadratically on a long run
# of digits: 2 MB of digits kept the scan busy for hours. With {m,n} bounds the work per start
# position is a small constant, so the scan stays linear in the file size. Keep it that way.
PDF_FORBIDDEN_RE = re.compile(rb"/(?:" + b"|".join(n.encode() for n in PDF_FORBIDDEN_NAMES) + rb")" + _PDF_NAME_END)
# The same without the attachment name: once an attachment is seen the scan only looks for these,
# so a file made of millions of /EmbeddedFile tokens costs one match, not millions.
PDF_ACTIVE_RE = re.compile(rb"/(?:" + b"|".join(n.encode() for n in PDF_ACTIVE_NAMES) + rb")" + _PDF_NAME_END)
PDF_HEX_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
PDF_ACTIVE_CONTENT_MESSAGE = "PDFs with scripts, launch actions or rich media are not allowed."
# Fixed text, nothing taken from the file. Teacher-facing: say what is wrong and how to fix it.
PDF_EMBEDDED_FILE_MESSAGE = (
    "This PDF has another file attached inside it (some tools add one when you export or sign a PDF). "
    "Export or print it again without attachments, or upload a DOCX version instead."
)
# Object number (up to 10 digits), generation (up to 5 digits, max 65535) and a little whitespace.
# Real writers put one space or newline between the tokens; an object written with more is
# simply not counted, and the sandboxed parser still enforces its own limits.
PDF_OBJ_RE = re.compile(rb"[0-9]{1,10}\s{1,10}[0-9]{1,5}\s{1,10}obj\b")
PDF_PAGE_RE = re.compile(rb"/Type[\s\x00]{0,32}/Page" + _PDF_NAME_END)

# Control characters to strip from plain text (preserve \t, \n, \r)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

# Dangerous formula starters for CSV/Excel export
FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


class UploadProfile(Enum):
    TIMETABLE = "timetable"
    DOCUMENT = "document"


@dataclass(frozen=True)
class ProfileRules:
    formats: frozenset[str]  # detected formats the profile accepts
    zip_members: int
    zip_total: int
    zip_member: int
    zip_ratio: float
    pdf_pages: int  # default page cap for the byte scan
    pdf_pages_code: str  # error code when the page cap is exceeded
    pptx_slides: int | None  # default slide cap (None: not checked)
    scan_all_pptx_parts: bool  # a presentation: scan every part (the timetable import never loads one)
    default_max_bytes: int


PROFILE_RULES: dict[UploadProfile, ProfileRules] = {
    # Unchanged behaviour of the Phase 4 import. PPTX still passes validation here and is refused
    # later by the parser with unsupported_format (backlog B4).
    UploadProfile.TIMETABLE: ProfileRules(
        formats=frozenset({"csv", "xlsx", "docx", "pdf", "pptx"}),
        zip_members=MAX_ZIP_MEMBERS,
        zip_total=MAX_ZIP_TOTAL_UNCOMPRESSED,
        zip_member=MAX_ZIP_MEMBER_UNCOMPRESSED,
        zip_ratio=MAX_ZIP_COMPRESSION_RATIO,
        pdf_pages=MAX_PDF_PAGES,
        pdf_pages_code="pdf_too_complex",
        pptx_slides=None,
        scan_all_pptx_parts=False,
        default_max_bytes=MAX_UPLOAD_SIZE,
    ),
    UploadProfile.DOCUMENT: ProfileRules(
        formats=frozenset({"pdf", "docx", "pptx", "txt", "md"}),
        zip_members=DOC_MAX_ZIP_MEMBERS,
        zip_total=DOC_MAX_ZIP_TOTAL_UNCOMPRESSED,
        zip_member=DOC_MAX_ZIP_MEMBER_UNCOMPRESSED,
        zip_ratio=DOC_MAX_ZIP_COMPRESSION_RATIO,
        pdf_pages=DOC_MAX_PDF_PAGES,
        pdf_pages_code="too_many_pages",
        pptx_slides=DOC_MAX_PPTX_SLIDES,
        scan_all_pptx_parts=True,
        default_max_bytes=DOC_UPLOAD_CEILING_BYTES,
    ),
}


def accepted_extensions(profile: UploadProfile) -> list[str]:
    """The file name extensions a profile accepts (lower case, no dot, sorted): its detected formats."""
    return sorted(PROFILE_RULES[profile].formats)


def resolve_max_file_bytes(profile: UploadProfile, requested: int | None = None) -> int:
    """
    The per-file size limit to enforce: the caller's value (the tenant's max_file_bytes for
    documents) capped by the platform ceiling. Timetable imports are always 2 MB.
    """
    if profile is UploadProfile.TIMETABLE:
        return MAX_UPLOAD_SIZE
    if requested is None or requested <= 0:
        return DOC_UPLOAD_CEILING_BYTES
    return min(requested, DOC_UPLOAD_CEILING_BYTES)


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


def _too_large_message(max_bytes: int) -> str:
    return f"File exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB."


async def stream_upload_with_limit(
    upload_file: UploadFile,
    max_bytes: int = MAX_UPLOAD_SIZE,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """
    Stream upload bytes from FastAPI UploadFile enforcing a hard size limit.
    Stops reading immediately and raises HTTP 413 if max_bytes is exceeded.
    max_bytes can never exceed the platform ceiling, whatever the caller passes.
    """
    max_bytes = min(max_bytes, DOC_UPLOAD_CEILING_BYTES)
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
                message=_too_large_message(max_bytes),
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        buffer.write(chunk)

    return buffer.getvalue()


@dataclass(frozen=True)
class UploadInspection:
    """What validation found: the format, and the pages (PDF) or slides (PPTX) it counted."""

    kind: str
    # PDF: uncompressed /Type /Page objects seen by the byte scan (a compressed page tree is
    # invisible to it, backlog B33). PPTX: p:sldId entries. None for the other formats.
    page_count: int | None = None


def validate_upload(
    data: bytes,
    filename: str,
    profile: UploadProfile = UploadProfile.TIMETABLE,
    *,
    max_file_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    max_pptx_slides: int | None = None,
) -> str:
    """
    Validate file content (magic bytes) against the profile's allow-list and return the format.
    The one validation: inspect_upload does the work, this returns only the format.
    """
    return inspect_upload(
        data,
        filename,
        profile,
        max_file_bytes=max_file_bytes,
        max_pdf_pages=max_pdf_pages,
        max_pptx_slides=max_pptx_slides,
    ).kind


def inspect_upload(
    data: bytes,
    filename: str,
    profile: UploadProfile = UploadProfile.TIMETABLE,
    *,
    max_file_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    max_pptx_slides: int | None = None,
) -> UploadInspection:
    """
    Validate file content (magic bytes) against the profile's allow-list; return the format and
    the page or slide count the checks saw. The document worker runs this inside its sandbox
    (app/services/document_parse_child.py), the routes run it through validate_upload_async.

    TIMETABLE: 'csv', 'xlsx', 'docx', 'pdf' (and 'pptx', which the importer refuses later).
    DOCUMENT:  'pdf', 'docx', 'pptx', 'txt', 'md'. CSV and XLSX give unsupported_format; text must
               be UTF-8 (BOM allowed); .md and .txt are told apart by extension.
    Always rejects OLE2 (.xls, .doc), macro containers, executables and shell scripts.

    max_file_bytes is the per-file limit (the tenant's own limit for documents) and is capped by
    DOC_UPLOAD_CEILING_BYTES. max_pdf_pages and max_pptx_slides override the profile defaults.
    """
    rules = PROFILE_RULES[profile]
    if not data:
        raise UploadSecurityError(code="empty_file", message="Uploaded file is empty.")
    limit = resolve_max_file_bytes(profile, max_file_bytes)
    if len(data) > limit:
        raise UploadSecurityError(
            code="file_too_large", message=_too_large_message(limit), status_code=status.HTTP_413_CONTENT_TOO_LARGE
        )

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

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    # 3. Check PDF magic
    if data.startswith(b"%PDF-"):
        if profile is UploadProfile.DOCUMENT and ext != "pdf":
            raise _extension_mismatch()
        pages = validate_pdf_bytes(
            data,
            max_pages=max_pdf_pages if max_pdf_pages is not None else rules.pdf_pages,
            pages_code=rules.pdf_pages_code,
        )
        return UploadInspection("pdf", pages)

    # 4. Check Zip-based formats (XLSX, DOCX, PPTX)
    if data.startswith(b"PK\x03\x04"):
        kind, slides = _sniff_openxml_format(
            data,
            filename,
            profile,
            max_slides=max_pptx_slides if max_pptx_slides is not None else rules.pptx_slides,
        )
        return UploadInspection(kind, slides)

    # 5. Plain text: CSV for timetables, TXT / MD for documents
    # Reject files containing NUL bytes (binary indicators)
    if b"\x00" in data:
        raise UploadSecurityError(
            code="disallowed_file_type",
            message="File contains binary characters and is not a valid CSV or text file.",
        )

    if profile is UploadProfile.DOCUMENT:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadSecurityError(
                code="invalid_encoding", message="Text files must be UTF-8 encoded."
            ) from None
        # The content is known to be text, so the extension decides between .md and .txt
        if ext == "csv":
            raise _unsupported_format()
        if ext not in ("txt", "md"):
            raise _extension_mismatch()
        return UploadInspection(ext)

    # Timetable CSV: verify text decodability
    try:
        data.decode("utf-8-sig")
        return UploadInspection("csv")
    except UnicodeDecodeError:
        try:
            data.decode("cp1252")
            return UploadInspection("csv")
        except UnicodeDecodeError as err:
            raise UploadSecurityError(
                code="disallowed_file_type",
                message="File encoding is not supported. Please upload UTF-8 or CP1252 encoded CSV.",
            ) from err


async def validate_upload_async(
    data: bytes,
    filename: str,
    profile: UploadProfile = UploadProfile.TIMETABLE,
    *,
    max_file_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    max_pptx_slides: int | None = None,
) -> str:
    """
    validate_upload in a worker thread, for every profile. Routes must call this one, never the
    synchronous function, so that scanning a large file does not stop the event loop from
    serving other requests.

    Honest limits of this: a Python thread cannot be killed or interrupted. If a validation ever
    ran for a long time, the thread would keep its CPU core and its slot in the default thread
    pool busy until it finished, and a regex holds the GIL while it runs. The thread only keeps
    the loop responsive; the protection against hostile input is that every algorithm in this
    module is linear in the input size (bounded regexes, capped archive work).
    """
    return await asyncio.to_thread(
        validate_upload,
        data,
        filename,
        profile,
        max_file_bytes=max_file_bytes,
        max_pdf_pages=max_pdf_pages,
        max_pptx_slides=max_pptx_slides,
    )


def sniff_and_validate_file_type(data: bytes, filename: str) -> str:
    """Timetable profile of validate_upload (the name the Phase 4 code and tests use)."""
    return validate_upload(data, filename, UploadProfile.TIMETABLE)


def _unsupported_format() -> UploadSecurityError:
    return UploadSecurityError(code="unsupported_format", message="This file type is not supported here.")


def _extension_mismatch() -> UploadSecurityError:
    return UploadSecurityError(code="file_type_mismatch", message="The file extension does not match the file content.")


class _PartScanner(ContentHandler):
    """
    Streams one XML part through expat. No tree is built, so memory stays small however large or
    deeply nested the part is. It enforces the nesting cap and hands every element to `on_start`.
    """

    def __init__(self, on_start: ElementCallback | None) -> None:
        super().__init__()
        self._on_start = on_start
        self._depth = 0

    def startElementNS(self, name: tuple[str | None, str], qname: str | None, attrs: Any) -> None:
        self._depth += 1
        if self._depth > MAX_XML_DEPTH:
            raise UploadSecurityError(
                code="xml_too_complex", message="The document contains XML that is nested too deeply."
            )
        if self._on_start is not None:
            self._on_start(name, attrs)

    def endElementNS(self, name: tuple[str | None, str], qname: str | None) -> None:
        self._depth -= 1


def _scan_xml(zf: zipfile.ZipFile, info: zipfile.ZipInfo, on_start: ElementCallback | None = None) -> None:
    """
    Parse one package part with defusedxml, as a stream. Any failure rejects the file (fail closed).
    A DOCTYPE, an entity or an external reference is refused by defusedxml; malformed XML, a bad CRC
    and nesting beyond MAX_XML_DEPTH are refused too.
    """
    parser = defused_sax.make_parser()
    parser.setFeature(feature_namespaces, True)
    parser.setContentHandler(_PartScanner(on_start))
    try:
        with zf.open(info) as member:
            parser.parse(member)
    except UploadSecurityError:
        raise
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


def _invalid_entries() -> UploadSecurityError:
    return UploadSecurityError(code="invalid_openxml_format", message="The archive contains invalid entries.")


def _unsafe_member_name(name: str) -> bool:
    """
    True for a zip entry name no OpenXML package needs: absolute (/ or \\), any backslash, a '..'
    segment, a colon (drive letters, alternate data streams), NUL or another control character.
    The name is the RAW one (ZipInfo.orig_filename): zipfile itself cuts at NUL and, on Windows,
    turns backslashes into slashes in ZipInfo.filename, which would hide both from this check.
    """
    if name.startswith(("/", "\\")) or "\\" in name or ":" in name:
        return True
    if ".." in name.split("/"):
        return True
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in name)


def _name_key(name: str) -> str:
    """Name as the collision check sees it: NFC, then casefold, then NFC again (casefold can denormalise)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def _reject_collision(members: dict[str, zipfile.ZipInfo], key: str) -> None:
    """Two entries whose names are the same after NFC + casefold: readers may pick either, so refuse both."""
    if key in members:
        raise _invalid_entries()


def _check_members(zf: zipfile.ZipFile, rules: ProfileRules) -> dict[str, zipfile.ZipInfo]:
    """
    Check every entry of the archive (all of infolist(), never a dict keyed by a lower-cased name,
    which would leave the second of two case-variant entries unchecked). Rejects unsafe names,
    names that collide after NFC + casefold, macro parts, embeddings, ActiveX, nested archives and
    OLE blobs. Returns the entries by their normalised name.
    """
    members: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if _unsafe_member_name(info.orig_filename):
            raise _invalid_entries()
        key = _name_key(info.orig_filename)
        _reject_collision(members, key)
        members[key] = info
        if info.is_dir():
            continue
        if key.rsplit("/", 1)[-1] == "vbaproject.bin":
            raise _reject_macro()
        padded = "/" + key
        if "/embeddings/" in padded or "/activex/" in padded:
            raise _reject_embedded()
        with zf.open(info) as member:
            head = member.read(8)
        if head.startswith(b"PK\x03\x04") or head.startswith(OLE2_MAGIC):
            raise _reject_embedded()
    return members


def _check_relationship(_name: tuple[str | None, str], attrs: Any) -> None:
    """One element of a .rels part: only plain hyperlinks may be external, and altChunk is refused."""
    rel_type = (attrs.get((None, "Type")) or "").rstrip("/").lower()
    if rel_type.endswith("/relationships/afchunk"):
        # altChunk pulls another (HTML, RTF, MHT, DOCX) file into the document
        raise _reject_embedded()
    is_hyperlink = rel_type.endswith("/relationships/hyperlink")
    # The target of a hyperlink is never fetched
    if (attrs.get((None, "TargetMode")) or "").lower() == "external" and not is_hyperlink:
        raise UploadSecurityError(
            code="external_relationship",
            message="Documents with links to external resources are not allowed.",
        )


def _check_content_type(_name: tuple[str | None, str], attrs: Any) -> None:
    content_type = (attrs.get((None, "ContentType")) or "").lower()
    if "macroenabled" in content_type or "vbaproject" in content_type:
        raise _reject_macro()


# ── Which package parts are scanned as XML ───────────────────────────────────

# Parts that are binary media or fonts by their extension. They are NOT parsed as XML, unless the
# package declares an XML content type for the very part (see _part_needs_xml_scan). Macro and OLE
# blobs (vbaProject.bin, embeddings, nested archives) are refused in _check_members whatever the
# extension, so ".bin" here only covers harmless binaries such as printer settings.
BINARY_MEDIA_EXTENSIONS = frozenset(
    {
        "png", "jpg", "jpeg", "jpe", "jfif", "gif", "bmp", "tif", "tiff", "webp", "ico", "wdp", "jxr", "heic",
        "wmf", "emf", "bin",
        "odttf", "ttf", "otf", "eot", "fntdata",
        "mp3", "m4a", "wav", "wma", "mp4", "m4v", "mov", "wmv", "avi", "mpg", "mpeg",
    }
)


def _extension(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _is_xml_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    base = content_type.split(";", 1)[0].strip().lower()
    return base.endswith("/xml") or base.endswith("+xml")


def _part_needs_xml_scan(key: str, declared: str | None) -> bool:
    """
    True when the part must be streamed through defusedxml. The libraries parse a part when the package
    DECLARES an XML content type for it (an Override by part name, else a Default by extension),
    whatever its file name says, so that is checked first; a ".xml" name is always scanned; and every
    other part is scanned too, unless its extension is a known binary media or font type. A part that
    is not XML at all then fails closed as invalid_xml, which a genuine package does not have.
    """
    if _is_xml_content_type(declared) or _extension(key) == "xml":
        return True
    return _extension(key) not in BINARY_MEDIA_EXTENSIONS


class _ContentTypes:
    """The Default (by extension) and Override (by part name) entries of [Content_Types].xml."""

    def __init__(self) -> None:
        self.defaults: dict[str, str] = {}
        self.overrides: dict[str, str] = {}

    def on_element(self, name: tuple[str | None, str], attrs: Any) -> None:
        _check_content_type(name, attrs)
        content_type = (attrs.get((None, "ContentType")) or "").lower()
        if name[1] == "Default":
            extension = (attrs.get((None, "Extension")) or "").lower().lstrip(".")
            if extension:
                self.defaults.setdefault(extension, content_type)
        elif name[1] == "Override":
            part = _name_key((attrs.get((None, "PartName")) or "").lstrip("/"))
            if part:
                self.overrides.setdefault(part, content_type)


def _sniff_openxml_format(
    data: bytes, filename: str, profile: UploadProfile, max_slides: int | None
) -> tuple[str, int | None]:
    """
    Validate a ZIP-based OpenXML container (XLSX, DOCX, PPTX). Returns the format and, for a
    presentation, the number of slides (p:sldId entries); None for the other formats.
    The type comes from the CONTENT; the extension must agree with it.
    Every XML check fails closed, and every XML part is streamed (see _scan_xml).
    """
    rules = PROFILE_RULES[profile]
    validate_zip_archive(data, profile)

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            members = _check_members(zf, rules)

            if "[content_types].xml" not in members:
                raise UploadSecurityError(code="invalid_openxml_format", message="File is not a valid Office document.")
            content_types = _ContentTypes()
            _scan_xml(zf, members["[content_types].xml"], content_types.on_element)

            # Relationship parts must parse and must not point outside the package
            for key, info in members.items():
                if key.endswith(".rels"):
                    _scan_xml(zf, info, _check_relationship)

            detected = [kind for kind, part in OPENXML_MAIN_PARTS.items() if part in members]
            if len(detected) != 1:
                raise UploadSecurityError(
                    code="invalid_openxml_format",
                    message="File is not a valid Excel (.xlsx), Word (.docx) or PowerPoint (.pptx) document.",
                )
            kind = detected[0]
            if kind not in rules.formats:
                raise _unsupported_format()

            # The main part must parse too (this also blocks entity expansion in it). For a
            # presentation the slides are the p:sldId entries of its slide list, whatever the
            # slide parts are called.
            slide_ids = 0

            def count_slide_ids(name: tuple[str | None, str], _attrs: Any) -> None:
                nonlocal slide_ids
                if name == (PRESENTATION_NS, "sldId"):
                    slide_ids += 1
                    if max_slides is not None and slide_ids > max_slides:
                        raise UploadSecurityError(
                            code="too_many_slides", message="The presentation has too many slides to process."
                        )

            main_part = OPENXML_MAIN_PARTS[kind]
            _scan_xml(zf, members[main_part], count_slide_ids if kind == "pptx" else None)

            # EVERY other part that the libraries could load as XML must parse (fail closed), wherever it
            # lives and whatever its name is (review of 5b-3, finding 2): python-docx, python-pptx and
            # openpyxl pick a part by the relationship that points at it and by the content type that
            # [Content_Types].xml declares for it, not by a ".xml" file name. See _part_needs_xml_scan.
            # The relationship parts and the content types were scanned above. Only a PRESENTATION in the
            # timetable profile keeps the old, narrower scan: the timetable importer refuses it later
            # (unsupported_format, backlog B4) and never loads its parts. DOCX and XLSX are scanned in full
            # by both profiles, because both load them with the same libraries.
            if kind == "pptx" and not rules.scan_all_pptx_parts:
                members_to_scan: dict[str, zipfile.ZipInfo] = {
                    k: i for k, i in members.items() if k.endswith(".xml") and k.startswith(("word/", "xl/"))
                }
            else:
                members_to_scan = members
            for key, info in members_to_scan.items():
                if info.is_dir() or key.endswith(".rels") or key in (main_part, "[content_types].xml"):
                    continue
                declared = content_types.overrides.get(key) or content_types.defaults.get(_extension(key))
                if _part_needs_xml_scan(key, declared):
                    _scan_xml(zf, info)

            ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
            if ext != kind:
                raise _extension_mismatch()
            return kind, (slide_ids if kind == "pptx" else None)

    except zipfile.BadZipFile as err:
        raise UploadSecurityError(
            code="corrupt_archive", message="Uploaded archive is corrupted or not a valid zip container."
        ) from err


# The PDF scan works on 1 MB slices so that decoding #xx escapes never needs more than a slice of
# memory: a 50 MB file made only of escapes would otherwise need over 1 GB. OVERLAP is how far a
# slice looks into the next one, so a name that crosses the cut is still seen whole.
_PDF_SCAN_SLICE = 1024 * 1024
_PDF_SCAN_OVERLAP = 256  # more than any forbidden name written entirely with #xx escapes
_HEX_ESCAPE_BYTES = {
    (a + b).encode(): bytes([int(a + b, 16)]) for a in "0123456789abcdefABCDEF" for b in "0123456789abcdefABCDEF"
}


def _decode_name_escapes(raw: bytes) -> bytes:
    if b"#" not in raw:
        return raw
    return PDF_HEX_ESCAPE_RE.sub(lambda m: _HEX_ESCAPE_BYTES[m.group(1)], raw)


def validate_pdf_bytes(
    data: bytes, *, max_pages: int = MAX_PDF_PAGES, pages_code: str = "pdf_too_complex"
) -> int:
    """
    The one PDF rule, shared by timetable imports and documents. Best-effort scan of the raw bytes:
    - rejects /JavaScript, /JS, /Launch, /RichMedia and /XFA (pdf_active_content) and the singular
      /EmbeddedFile (pdf_embedded_file: a real attached file), also written with #xx escapes
      (/J#61vaScript). The plural /EmbeddedFiles is the catalog's name tree key, which many PDFs
      carry without any attachment, and it is allowed. If both kinds are present the more serious
      code, pdf_active_content, wins;
    - allows /OpenAction and /AA (they only name actions and nothing here executes them);
    - caps the object and page counts, and returns the page count it saw.
    It only sees UNCOMPRESSED names and counts: a name or a /Type /Page inside a compressed object
    stream is invisible to it, so a PDF can hide both active content and its real page count from
    this scan. It is a first filter, not a protection. Since step 5b-2 the document worker runs it
    again inside its sandbox (killable; memory limit on Linux only); the real page count comes from
    the text extraction of 5b-3, which must re-check it (backlog B33).
    The scan is linear in the file size and its memory use does not grow with the file.
    """
    pages = 0
    objects = 0
    embedded = False
    pos = 0
    size = len(data)
    while pos < size:
        end = min(pos + _PDF_SCAN_SLICE, size)
        if end < size:
            # Never cut inside a #xx escape: that would leave half of it undecoded
            hash_at = data.rfind(b"#", end - 2, end)
            if hash_at != -1:
                end = hash_at
        # Decode the slice and its look-ahead apart, so the match offsets of the slice stay known
        main = _decode_name_escapes(data[pos:end])
        plain = main + _decode_name_escapes(data[end : end + _PDF_SCAN_OVERLAP])
        found = PDF_ACTIVE_RE.search(plain) if embedded else PDF_FORBIDDEN_RE.search(plain)
        if found is not None and found.group(0)[1:] == PDF_EMBEDDED_NAME.encode():
            # Spelled with #xx escapes the match is still the plain name here: the slice is decoded.
            # The leftmost match was the attachment, so nothing forbidden comes before it
            embedded = True
            found = PDF_ACTIVE_RE.search(plain, found.end())
        if found is not None:
            raise UploadSecurityError(code="pdf_active_content", message=PDF_ACTIVE_CONTENT_MESSAGE)
        # A match belongs to the slice it starts in, so the look-ahead is never counted twice
        for match in PDF_PAGE_RE.finditer(plain):
            if match.start() >= len(main):
                break
            pages += 1
        # Objects are counted slice by slice as well: one regex pass over 50 MB would hold the GIL
        # (and so freeze the event loop) for over a second
        raw = data[pos : end + _PDF_SCAN_OVERLAP]
        for match in PDF_OBJ_RE.finditer(raw):
            if match.start() >= end - pos:
                break
            objects += 1
        pos = end
    if embedded:
        raise UploadSecurityError(code="pdf_embedded_file", message=PDF_EMBEDDED_FILE_MESSAGE)
    if objects > MAX_PDF_OBJECTS:
        raise UploadSecurityError(code="pdf_too_complex", message="The PDF is too large or complex to process.")
    if pages > max_pages:
        if pages_code == "too_many_pages":
            raise UploadSecurityError(code="too_many_pages", message="The PDF has too many pages to process.")
        raise UploadSecurityError(code="pdf_too_complex", message="The PDF is too large or complex to process.")
    return pages


def validate_zip_archive(data: bytes, profile: UploadProfile = UploadProfile.TIMETABLE) -> None:
    """
    Perform zip safety and anti-zip-bomb checks:
    - Verifies zip integrity
    - Rejects password-protected / encrypted entries
    - Checks absolute member count cap
    - Checks total uncompressed size cap
    - Checks per-member uncompressed size cap
    - Checks compression ratio threshold
    The caps come from the profile (timetable: 15 MB / 5 MB / ratio 50; document: 60 MB / 25 MB / ratio 100).
    """
    rules = PROFILE_RULES[profile]
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile as err:
        raise UploadSecurityError(code="corrupt_archive", message="File is not a valid zip archive.") from err

    infolist = zf.infolist()
    if len(infolist) > rules.zip_members:
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
        if info.file_size > rules.zip_member:
            raise UploadSecurityError(
                code="zip_member_too_large",
                message=f"An archive member exceeds the maximum size of {rules.zip_member // (1024*1024)} MB.",
            )

        total_uncompressed += info.file_size
        if total_uncompressed > rules.zip_total:
            raise UploadSecurityError(
                code="zip_bomb_detected",
                message=f"Archive total uncompressed size exceeds {rules.zip_total // (1024*1024)} MB.",
            )

        # Check compression ratio for entries larger than 10KB
        if info.compress_size > 0 and info.file_size > 10 * 1024:
            ratio = info.file_size / info.compress_size
            if ratio > rules.zip_ratio:
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
