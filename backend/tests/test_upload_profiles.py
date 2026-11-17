"""
Step 5a-3: upload profiles (TIMETABLE, DOCUMENT), the shared PDF rule and the per-file size limit.

Pure unit tests: no database, no Redis.
"""

import io
import os
import zipfile
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.core.upload_security import (
    DOC_MAX_ZIP_COMPRESSION_RATIO,
    DOC_UPLOAD_CEILING_BYTES,
    MAX_UPLOAD_SIZE,
    MAX_ZIP_COMPRESSION_RATIO,
    UploadProfile,
    UploadSecurityError,
    resolve_max_file_bytes,
    stream_upload_with_limit,
    validate_upload,
)
from tests.security.upload_assertions import (
    CT,
    CT_XML,
    EMPTY_XML,
    MB,
    assert_hostile_pdfs_rejected,
    code_of,
    good_docx,
    good_pptx,
    make_pdf,
    make_zip,
)

DOC = UploadProfile.DOCUMENT
TT = UploadProfile.TIMETABLE
XXE = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><root>&a;</root>'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def reject(data: bytes, name: str, profile: UploadProfile = DOC, **kw: int) -> str:
    with pytest.raises(UploadSecurityError) as exc:
        validate_upload(data, name, profile, **kw)
    return code_of(exc.value)


def ext_rel(rel_type: str, target: str = "http://example.org/x") -> bytes:
    return (
        f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" Type="{OD}/{rel_type}" '
        f'Target="{target}" TargetMode="External"/></Relationships>'
    ).encode()


# ─── Formats ─────────────────────────────────────────────────────────────────

def test_document_profile_accepts_its_five_formats() -> None:
    assert validate_upload(make_pdf(), "a.pdf", DOC) == "pdf"
    assert validate_upload(good_docx(), "a.docx", DOC) == "docx"
    assert validate_upload(good_pptx(), "a.pptx", DOC) == "pptx"
    assert validate_upload(b"plain notes", "a.txt", DOC) == "txt"
    assert validate_upload(b"# Heading\ntext", "a.md", DOC) == "md"


def test_csv_and_xlsx_are_unsupported_for_documents() -> None:
    xlsx = make_zip({CT: CT_XML, "xl/workbook.xml": EMPTY_XML})
    assert reject(xlsx, "a.xlsx") == "unsupported_format"
    assert reject(b"a,b\n1,2\n", "a.csv") == "unsupported_format"
    # They still work for timetables
    assert validate_upload(xlsx, "a.xlsx", TT) == "xlsx"
    assert validate_upload(b"a,b\n1,2\n", "a.csv", TT) == "csv"


def test_document_default_profile_is_timetable() -> None:
    assert validate_upload(b"a,b\n1,2\n", "whatever.bin") == "csv"


def test_pdf_extension_must_match_for_documents() -> None:
    assert reject(make_pdf(), "a.txt") == "file_type_mismatch"
    assert validate_upload(make_pdf(), "renamed.bin", TT) == "pdf"  # timetable behaviour unchanged


@pytest.mark.parametrize("magic", [b"MZ\x90\x00", b"\x7fELF\x02", b"#!/bin/sh\n", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x"])
def test_executables_scripts_and_ole_rejected_by_both_profiles(magic: bytes) -> None:
    for profile in UploadProfile:
        assert reject(magic + b"payload", "a.pdf", profile) == "disallowed_file_type"


def test_empty_file_rejected() -> None:
    assert reject(b"", "a.txt") == "empty_file"
    assert reject(b"", "a.csv", TT) == "empty_file"


# ─── Text ────────────────────────────────────────────────────────────────────

def test_text_utf8_with_and_without_bom() -> None:
    assert validate_upload("नमस्ते दुनिया".encode(), "a.txt", DOC) == "txt"
    assert validate_upload(b"\xef\xbb\xbfhello", "a.md", DOC) == "md"


def test_text_not_utf8_is_invalid_encoding() -> None:
    assert reject("café".encode("cp1252"), "a.txt") == "invalid_encoding"
    assert reject("café".encode("utf-16"), "a.txt") in ("disallowed_file_type", "invalid_encoding")


def test_md_and_txt_decided_by_extension_once_text() -> None:
    same = b"# Title\n\nbody"
    assert validate_upload(same, "n.md", DOC) == "md"
    assert validate_upload(same, "N.TXT", DOC) == "txt"
    assert reject(same, "n.html") == "file_type_mismatch"
    assert reject(same, "noextension") == "file_type_mismatch"


def test_text_with_nul_bytes_rejected() -> None:
    assert reject(b"abc\x00def", "a.txt") == "disallowed_file_type"


def test_timetable_csv_still_accepts_cp1252_and_ignores_extension() -> None:
    assert validate_upload("café,x\n".encode("cp1252"), "a.csv", TT) == "csv"
    assert validate_upload(b"a,b", "a.txt", TT) == "csv"


# ─── PDF rule (documents AND timetables) ─────────────────────────────────────

def test_hostile_pdfs_rejected_in_both_profiles() -> None:
    assert_hostile_pdfs_rejected()


@pytest.mark.parametrize("profile", list(UploadProfile))
@pytest.mark.parametrize("token", [b"/OpenAction 3 0 R", b"/AA << /O 4 0 R >>", b"/O#70enAction 3 0 R", b"/A#41 <<>>"])
def test_open_action_and_aa_allowed_in_both_profiles(profile: UploadProfile, token: bytes) -> None:
    assert validate_upload(make_pdf(token), "a.pdf", profile) == "pdf"


@pytest.mark.parametrize("profile", list(UploadProfile))
def test_pdf_names_that_only_look_similar_pass(profile: UploadProfile) -> None:
    assert validate_upload(make_pdf(b"/JSON 1 /AAA 2 /XFAX 3 /RichMediaX 4 /Launcher 5"), "a.pdf", profile) == "pdf"


def test_pdf_page_cap_is_a_parameter() -> None:
    assert validate_upload(make_pdf(pages=150), "a.pdf", DOC) == "pdf"  # document default is 200
    assert reject(make_pdf(pages=201), "a.pdf") == "too_many_pages"
    assert reject(make_pdf(pages=5), "a.pdf", max_pdf_pages=4) == "too_many_pages"
    assert validate_upload(make_pdf(pages=5), "a.pdf", DOC, max_pdf_pages=5) == "pdf"
    # Timetable keeps 100 pages and its old code
    assert reject(make_pdf(pages=150), "a.pdf", TT) == "pdf_too_complex"
    assert validate_upload(make_pdf(pages=100), "a.pdf", TT) == "pdf"


def test_pdf_object_cap_applies_to_documents() -> None:
    many = b"%PDF-1.4\n" + b"".join(b"%d 0 obj\n<< >>\nendobj\n" % i for i in range(1, 25000))
    assert reject(many, "a.pdf") == "pdf_too_complex"


# ─── Per-file size is a parameter, capped by the platform ceiling ────────────

def test_resolve_max_file_bytes() -> None:
    assert resolve_max_file_bytes(TT) == MAX_UPLOAD_SIZE
    assert resolve_max_file_bytes(TT, 10 * MB) == MAX_UPLOAD_SIZE
    assert resolve_max_file_bytes(DOC) == DOC_UPLOAD_CEILING_BYTES
    assert resolve_max_file_bytes(DOC, 10 * MB) == 10 * MB
    assert resolve_max_file_bytes(DOC, 10**12) == DOC_UPLOAD_CEILING_BYTES
    assert resolve_max_file_bytes(DOC, 0) == DOC_UPLOAD_CEILING_BYTES
    assert DOC_UPLOAD_CEILING_BYTES == 50 * MB


def test_tenant_limit_is_enforced_by_validate_upload() -> None:
    assert reject(b"x" * 1001, "a.txt", max_file_bytes=1000) == "file_too_large"
    assert validate_upload(b"x" * 1000, "a.txt", DOC, max_file_bytes=1000) == "txt"


def test_ceiling_cannot_be_raised_by_the_parameter() -> None:
    assert reject(b"x" * (DOC_UPLOAD_CEILING_BYTES + 1), "a.txt", max_file_bytes=10**12) == "file_too_large"


def test_timetable_size_limit_unchanged() -> None:
    assert reject(b"a" * (MAX_UPLOAD_SIZE + 1), "a.csv", TT) == "file_too_large"
    assert validate_upload(b"a" * MAX_UPLOAD_SIZE, "a.csv", TT) == "csv"


async def test_stream_upload_never_exceeds_the_ceiling() -> None:
    big = UploadFile(file=io.BytesIO(b"x" * (DOC_UPLOAD_CEILING_BYTES + 1)), filename="a.txt")
    with pytest.raises(UploadSecurityError) as exc:
        await stream_upload_with_limit(big, max_bytes=10**12)
    assert exc.value.status_code == 413
    assert code_of(exc.value) == "file_too_large"


async def test_stream_upload_uses_the_tenant_limit() -> None:
    f = UploadFile(file=io.BytesIO(b"x" * (10 * MB + 1)), filename="a.txt")
    with pytest.raises(UploadSecurityError) as exc:
        await stream_upload_with_limit(f, max_bytes=10 * MB)
    assert code_of(exc.value) == "file_too_large"
    ok = UploadFile(file=io.BytesIO(b"x" * (10 * MB)), filename="a.txt")
    assert len(await stream_upload_with_limit(ok, max_bytes=10 * MB)) == 10 * MB


# ─── Zip caps ────────────────────────────────────────────────────────────────

def stored_zip(members: dict[str, int]) -> bytes:
    """Zip of incompressible members (ratio 1), name -> size, so only the size caps can trigger."""
    return make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, **{n: os.urandom(s) for n, s in members.items()}},
                    compression=zipfile.ZIP_STORED)


def mixed_zip(members: dict[str, int], random_each: int) -> bytes:
    """Members of the given sizes that are partly random, so they stay under the ratio cap and
    the whole zip fits the 2 MB timetable file limit while the uncompressed sizes are large."""
    files = {n: os.urandom(random_each) + b"\x00" * (s - random_each) for n, s in members.items()}
    return make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, **files})


def test_member_cap_5mb_for_timetable_25mb_for_documents() -> None:
    data = mixed_zip({"word/media/a.bin": 6 * MB}, 600 * 1024)  # ratio ~10, zip ~0.6 MB
    assert reject(data, "a.docx", TT) == "zip_member_too_large"
    assert validate_upload(data, "a.docx", DOC) == "docx"
    assert reject(stored_zip({"word/media/a.bin": 26 * MB}), "a.docx") == "zip_member_too_large"


def test_total_cap_15mb_for_timetable_60mb_for_documents() -> None:
    four_mb = mixed_zip({f"word/media/{i}.bin": 4 * MB for i in range(4)}, 400 * 1024)  # 16 MB, ratio ~10
    assert reject(four_mb, "a.docx", TT) == "zip_bomb_detected"
    assert validate_upload(four_mb, "a.docx", DOC) == "docx"
    big = mixed_zip({f"word/media/{i}.bin": 21 * MB for i in range(3)}, 5 * MB)  # 63 MB, zip ~15 MB
    assert reject(big, "a.docx") == "zip_bomb_detected"


def _ratio_member(random_bytes: int, zero_bytes: int) -> bytes:
    return os.urandom(random_bytes) + b"\x00" * zero_bytes


def _zip_ratio(data: bytes, name: str) -> float:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(name)
        return info.file_size / info.compress_size


def test_ratio_cap_50_for_timetable_100_for_documents() -> None:
    mid = make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, "word/media/m.bin": _ratio_member(1000, 75000)})
    ratio = _zip_ratio(mid, "word/media/m.bin")
    assert MAX_ZIP_COMPRESSION_RATIO < ratio < DOC_MAX_ZIP_COMPRESSION_RATIO, ratio
    assert reject(mid, "a.docx", TT) == "zip_bomb_detected"
    assert validate_upload(mid, "a.docx", DOC) == "docx"
    bomb = make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, "word/media/m.bin": b"\x00" * (3 * MB)})
    assert reject(bomb, "a.docx") == "zip_bomb_detected"


def test_member_count_cap_for_documents() -> None:
    many = make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, **{f"word/media/{i}.png": b"a" for i in range(1000)}})
    assert reject(many, "a.docx") == "zip_bomb_detected"
    ok = make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, **{f"word/media/{i}.png": b"a" for i in range(500)}})
    assert validate_upload(ok, "a.docx", DOC) == "docx"
    assert reject(ok, "a.docx", TT) == "zip_bomb_detected"  # timetable cap is 200


def test_encrypted_member_rejected() -> None:
    data = bytearray(good_docx())
    # Set the "encrypted" bit in the local header and the central directory flag fields
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):
        pos = 0
        while (pos := bytes(data).find(marker, pos)) != -1:
            off = pos + (6 if marker == b"PK\x03\x04" else 8)
            data[off] |= 0x1
            pos += 4
    assert reject(bytes(data), "a.docx") == "encrypted_file"


# ─── OpenXML rules, including pptx parts ─────────────────────────────────────

@pytest.mark.parametrize("builder, name", [(good_docx, "a.docx"), (good_pptx, "a.pptx")])
def test_macros_embeddings_nested_zip_ole_rejected(builder, name: str) -> None:  # type: ignore[no-untyped-def]
    root = "word" if name.endswith("docx") else "ppt"
    assert reject(builder({f"{root}/vbaProject.bin": b"x"}), name) == "macro_enabled_file"
    assert reject(builder({f"{root}/embeddings/oleObject1.bin": b"x"}), name) == "embedded_content"
    assert reject(builder({f"{root}/media/n.bin": b"PK\x03\x04rest"}), name) == "embedded_content"
    assert reject(builder({f"{root}/media/o.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"}), name) == "embedded_content"


def test_pptx_macro_content_type_rejected() -> None:
    macro_ct = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/ppt/presentation.xml" '
        b'ContentType="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"/></Types>'
    )
    data = make_zip({CT: macro_ct, "ppt/presentation.xml": EMPTY_XML})
    assert reject(data, "a.pptx") == "macro_enabled_file"


def test_pptx_activex_rejected_for_documents() -> None:
    assert reject(good_pptx({"ppt/activeX/activeX1.xml": EMPTY_XML}), "a.pptx") == "embedded_content"


def test_pptx_external_relationships_and_hyperlink_exception() -> None:
    path = "ppt/slides/_rels/slide1.xml.rels"
    for rel in ("oleObject", "image", "attachedTemplate", "slideMaster"):
        assert reject(good_pptx({path: ext_rel(rel, "file:///x")}), "a.pptx") == "external_relationship"
    assert validate_upload(good_pptx({path: ext_rel("hyperlink")}), "a.pptx", DOC) == "pptx"
    assert reject(good_pptx({path: ext_rel("hyperlinkX")}), "a.pptx") == "external_relationship"


def test_pptx_xxe_in_main_part_and_in_other_ppt_parts() -> None:
    assert reject(make_zip({CT: CT_XML, "ppt/presentation.xml": XXE}), "a.pptx") == "xml_entity_expansion_forbidden"
    # Slides are parsed too for documents (fail closed), which the timetable profile never did
    slide = {"ppt/slides/slide1.xml": XXE}
    assert reject(good_pptx(slide), "a.pptx") == "xml_entity_expansion_forbidden"
    assert validate_upload(good_pptx(slide), "a.pptx", TT) == "pptx"  # timetable behaviour unchanged
    for part in ("ppt/slides/slide1.xml", "ppt/slideLayouts/slideLayout1.xml", "ppt/notesSlides/notesSlide1.xml"):
        assert reject(good_pptx({part: b"<not xml"}), "a.pptx") == "invalid_xml"


def test_pptx_slide_cap_is_a_parameter() -> None:
    # Slides are the p:sldId entries of ppt/presentation.xml (review of 5a-3, finding 6)
    def deck(n: int) -> bytes:
        ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(n))
        presentation = (
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            f'xmlns:r="{OD}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>'
        ).encode()
        return make_zip({CT: CT_XML, "ppt/presentation.xml": presentation})

    assert validate_upload(deck(100), "a.pptx", DOC) == "pptx"
    assert reject(deck(101), "a.pptx") == "too_many_slides"
    assert reject(deck(4), "a.pptx", max_pptx_slides=3) == "too_many_slides"
    assert validate_upload(deck(4), "a.pptx", DOC, max_pptx_slides=4) == "pptx"
    assert validate_upload(deck(101), "a.pptx", TT) == "pptx"  # timetable never counted slides


def test_docx_xml_rules_for_documents() -> None:
    assert reject(make_zip({CT: CT_XML, "word/document.xml": XXE}), "a.docx") == "xml_entity_expansion_forbidden"
    assert reject(good_docx({"word/_rels/document.xml.rels": b"not xml"}), "a.docx") == "invalid_xml"
    assert reject(make_zip({"readme.txt": b"x"}), "a.docx") == "invalid_openxml_format"
    assert reject(good_docx(), "a.pptx") == "file_type_mismatch"
    assert validate_upload(good_docx({"word/_rels/document.xml.rels": ext_rel("hyperlink")}), "a.docx", DOC) == "docx"
    assert reject(good_docx({"word/_rels/document.xml.rels": ext_rel("oleObject")}), "a.docx") == "external_relationship"


def test_path_traversal_member_names_rejected() -> None:
    assert reject(good_docx({"../evil.xml": EMPTY_XML}), "a.docx") == "invalid_openxml_format"


# ─── Fixed messages ──────────────────────────────────────────────────────────

def test_errors_never_echo_file_or_member_names() -> None:
    secret = "secret_dir_xyz"
    cases = [
        (good_docx({f"word/{secret}/vbaProject.bin": b"x"}), f"{secret}.docx"),
        (good_pptx({f"ppt/slides/{secret}.xml": b"<bad"}), f"{secret}.pptx"),
        (good_docx({f"word/{secret}/_rels/x.rels": ext_rel("image")}), f"{secret}.docx"),
        (b"abc\xff\xfe", f"{secret}.txt"),
    ]
    for data, name in cases:
        with pytest.raises(UploadSecurityError) as exc:
            validate_upload(data, name, DOC)
        assert secret not in str(exc.value.detail)


def test_new_codes_are_documented() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "api" / "phase5.md").read_text(encoding="utf-8")
    for code in ("unsupported_format", "invalid_encoding", "too_many_pages", "too_many_slides", "file_too_large",
                 "too_many_parts", "pdf_active_content", "pdf_too_complex"):
        assert f"`{code}`" in doc, code
