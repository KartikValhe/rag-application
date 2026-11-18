"""
The PDF embedded-file rule (real-corpus finding, 2026-11-18).

Two real syllabus PDFs were refused as `pdf_active_content`. The scan was right: each carries a real
attached file stream (`/Type /EmbeddedFile`, about 24 KB, declared as a C2PA manifest). The rule keeps
refusing a real attachment, now with its own code `pdf_embedded_file` and a message that says how to
fix the file. Everything that only looks similar stays allowed: the plural `/EmbeddedFiles` name tree
entry, a file specification without a stream, longer names (`/EmbeddedFileX`) and text that is merely
split across lines (a PDF name never spans a line break).

Every byte string here is built by this file. No real document content is used.
"""

import pytest

from app.core import upload_security
from app.core.upload_security import (
    UploadProfile,
    UploadSecurityError,
    validate_pdf_bytes,
    validate_upload,
)
from app.workers.errors import PERMANENT_ERROR_CODES
from tests.security.upload_assertions import code_of, make_pdf

EMBEDDED = "pdf_embedded_file"
ACTIVE = "pdf_active_content"
PROFILES = list(UploadProfile)


def verdict(data: bytes, profile: UploadProfile = UploadProfile.DOCUMENT) -> str:
    try:
        validate_upload(data, "a.pdf", profile)
    except UploadSecurityError as exc:
        return code_of(exc)
    return "accepted"


def attachment(subtype: bytes = b"/application#2Fc2pa", body: bytes = b"0123456789") -> bytes:
    """A catalog with a name tree entry, a file specification and a real embedded file stream."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /AF [4 0 R] "
        b"/Names << /EmbeddedFiles << /Names [(a) 4 0 R] >> >> >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Type /Filespec /F (a) /EF << /F 5 0 R >> >>\nendobj\n"
        b"5 0 obj\n<< /Type /EmbeddedFile /Subtype " + subtype + b" /Length "
        + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


# ─── A real attached stream is refused, with its own code ────────────────────

@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.value)
def test_a_real_embedded_file_stream_is_rejected_with_its_own_code(profile: UploadProfile) -> None:
    assert verdict(attachment(), profile) == EMBEDDED


@pytest.mark.parametrize("subtype", [b"/application#2Fc2pa", b"/application#2Fpdf", b"/application#2Foctet-stream"])
def test_the_declared_subtype_does_not_buy_an_exemption(subtype: bytes) -> None:
    """The subtype is written by whoever made the file, so it proves nothing and is never trusted."""
    assert verdict(attachment(subtype)) == EMBEDDED


def test_an_attachment_without_a_subtype_is_rejected_too() -> None:
    data = make_pdf(b"/Names << /X << /Type /EmbeddedFile /Length 0 >> >>")
    assert verdict(data) == EMBEDDED


def test_the_message_is_fixed_and_tells_the_teacher_what_to_do() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        validate_pdf_bytes(attachment(b"/application#2Fc2pa", b"SECRET-BODY"))
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == EMBEDDED
    message = str(detail["message"])
    assert "attached" in message and "DOCX" in message and "export" in message.lower()
    assert "c2pa" not in message.lower() and "SECRET-BODY" not in message  # nothing taken from the file
    assert message == upload_security.PDF_EMBEDDED_FILE_MESSAGE


def test_the_new_code_ends_a_job_for_good_and_is_documented() -> None:
    assert EMBEDDED in PERMANENT_ERROR_CODES
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "api" / "phase5.md").read_text(encoding="utf-8")
    assert f"`{EMBEDDED}`" in doc


# ─── What only looks similar stays allowed ───────────────────────────────────

@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.value)
def test_the_plural_name_tree_entry_without_a_stream_is_allowed(profile: UploadProfile) -> None:
    data = make_pdf(b"/Names << /EmbeddedFiles << /Names [] >> >>")
    assert verdict(data, profile) == "accepted"


def test_a_file_specification_without_ef_is_allowed() -> None:
    data = make_pdf(b"/AF [4 0 R]") + b"4 0 obj\n<< /Type /Filespec /F (a) /UF (a) >>\nendobj\n"
    assert verdict(data) == "accepted"


@pytest.mark.parametrize(
    "extra",
    [b"/EmbeddedFileX", b"/EmbeddedFile1", b"/EmbeddedFile_", b"/EmbeddedFile-", b"/EmbeddedFile.", b"/EmbeddedFiles",
     b"/EmbeddedFile#41", b"/EmbeddedFile#73", b"/xEmbeddedFile", b"/EmbeddedFil", b"/embeddedfile"],
)
def test_longer_shorter_and_different_names_are_allowed(extra: bytes) -> None:
    assert verdict(make_pdf(extra)) == "accepted"


@pytest.mark.parametrize(
    "extra",
    [b"/Embedded\nFile", b"/Embedded\r\nFile", b"/Embedded File", b"/Embedded\tFile", b"/Embedded\x00File",
     b"/Embedded%x\nFile", b"/Embedded(\n)File", b"/Embedded\\\nFile", b"/Embedded#0AFile", b"/Embedded#20File"],
)
def test_a_name_split_across_lines_is_not_a_name(extra: bytes) -> None:
    assert verdict(make_pdf(extra)) == "accepted"


# ─── The name token ends at whitespace or one of ()<>[]{}/% ──────────────────

DELIMITERS = [b" ", b"\t", b"\n", b"\r", b"\x0c", b"\x00", b"(", b")", b"<", b">", b"[", b"]", b"{", b"}", b"/", b"%"]


@pytest.mark.parametrize("delimiter", DELIMITERS, ids=lambda d: repr(d))
def test_the_name_followed_by_each_delimiter_is_rejected(delimiter: bytes) -> None:
    assert verdict(make_pdf(b"/EmbeddedFile" + delimiter + b"x")) == EMBEDDED


def test_the_name_at_the_very_end_of_the_file_is_rejected() -> None:
    assert verdict(b"%PDF-1.4\n1 0 obj\n<< /Type /EmbeddedFile") == EMBEDDED


@pytest.mark.parametrize("token", [b"/Embedded#46ile", b"/E#6dbeddedFile", b"/#45mbeddedFile", b"/EmbeddedFil#65", b"/#45#6d#62#65#64#64#65#64#46#69#6c#65"])
def test_hex_escaped_spellings_are_rejected(token: bytes) -> None:
    assert verdict(make_pdf(token + b" ")) == EMBEDDED


# ─── It never hides the more serious code, and the scan stays exact ──────────

@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.value)
@pytest.mark.parametrize("body", [
    b"/EmbeddedFile /JavaScript (x)",
    b"/JavaScript (x) /EmbeddedFile",
    b"/EmbeddedFile /EmbeddedFile /Launch",
    b"/EmbeddedFile /RichMedia",
    b"/EmbeddedFile /XFA",
    b"/EmbeddedFile /J#53 (x)",
])
def test_active_content_wins_over_an_embedded_file(profile: UploadProfile, body: bytes) -> None:
    assert verdict(make_pdf(body), profile) == ACTIVE


def test_active_content_in_a_later_slice_still_wins() -> None:
    size = upload_security._PDF_SCAN_SLICE
    first = b"%PDF-1.4\n/EmbeddedFile\n" + b" " * size
    assert verdict(first + b"/JavaScript (x)\n") == ACTIVE
    assert verdict(first + b"/JS (x)\n") == ACTIVE
    assert verdict(first + b"/Embedded#46ile\n") == EMBEDDED


def test_the_name_is_seen_on_a_slice_cut() -> None:
    size = upload_security._PDF_SCAN_SLICE
    for shift in range(-30, 30, 3):
        head = b"%PDF-1.4\n" + b" " * (size + shift - len(b"%PDF-1.4\n"))
        assert verdict(head + b"/EmbeddedFile\n") == EMBEDDED, shift
        assert verdict(head + b"/Embedded#46ile\n") == EMBEDDED, shift
        assert verdict(head + b"/EmbeddedFiles\n") == "accepted", shift


def test_many_repeats_are_handled_in_linear_time() -> None:
    """The scan must not loop over millions of matches once the first one is found."""
    import time

    data = b"%PDF-1.4\n" + b"/EmbeddedFile " * 1_000_000
    start = time.perf_counter()
    assert verdict(data) == EMBEDDED
    assert time.perf_counter() - start < 3.0


def test_the_embedded_file_is_reported_before_the_page_cap() -> None:
    data = make_pdf(b"/EmbeddedFile", pages=5)
    with pytest.raises(UploadSecurityError) as exc:
        validate_pdf_bytes(data, max_pages=2, pages_code="too_many_pages")
    assert code_of(exc.value) == EMBEDDED  # the embedded file is the first thing the teacher must fix


# ─── A NUL byte is PDF whitespace: it ends a name too ────────────────────────

@pytest.mark.parametrize("name", [b"/JavaScript", b"/JS", b"/Launch", b"/RichMedia", b"/XFA"])
def test_a_nul_after_a_forbidden_name_does_not_hide_it(name: bytes) -> None:
    assert verdict(make_pdf(name + b"\x00(x)")) == ACTIVE


def test_a_nul_between_type_and_page_still_counts_the_page() -> None:
    data = b"%PDF-1.4\n" + b"1 0 obj\n<< /Type\x00/Page >>\nendobj\n" * 3
    assert validate_pdf_bytes(data, max_pages=10) == 3
