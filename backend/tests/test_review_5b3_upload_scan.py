"""
Review of step 5b-3, finding 2: the XML scan of OpenXML packages must cover EVERY part python-docx,
python-pptx and openpyxl can load, not only parts whose name ends in `.xml`. The libraries pick a part by
the relationship that points at it and by the content type [Content_Types].xml declares for it (an Override
by part name, else a Default by extension), whatever the file name says. Both profiles (DOCUMENT and
TIMETABLE) load docx and xlsx through the same libraries, so both get the same scan.
"""

import io
import re
import zipfile

import pytest

from app.core.upload_security import UploadProfile, UploadSecurityError, inspect_upload
from tests.fixtures.parse_files import docx_bytes, pptx_bytes

ENTITY = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><r>&a;</r>'
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
XLSX_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active["A1"] = "Monday"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx() -> bytes:
    return docx_bytes(lambda d: d.add_paragraph("Unit 1"))


def _rewrite(data: bytes, edits: dict[str, bytes | None], add: dict[str, bytes] | None = None) -> bytes:
    """Copy of a zip with members replaced (bytes), removed (None) and added."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in edits:
                if edits[info.filename] is not None:
                    dst.writestr(info.filename, edits[info.filename])  # type: ignore[arg-type]
                continue
            dst.writestr(info.filename, src.read(info.filename))
        for name, content in (add or {}).items():
            dst.writestr(name, content)
    return out.getvalue()


def _read(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name).decode()


def _with_main_part_renamed(data: bytes, old: str, new: str, content_type: str, payload: bytes = ENTITY) -> bytes:
    """The package's real main part stays (so the format is still recognised); the root relationship
    and an Override now point at `new`, a part whose NAME is not .xml, carrying `payload`."""
    rels = _read(data, "_rels/.rels")
    assert f'Target="{old}"' in rels or f'Target="/{old}"' in rels
    rels = re.sub(r'Target="/?' + re.escape(old) + '"', f'Target="{new}"', rels)
    ct = _read(data, "[Content_Types].xml")
    ct = ct.replace("</Types>", f'<Override PartName="/{new}" ContentType="{content_type}"/></Types>')
    return _rewrite(data, {"_rels/.rels": rels.encode(), "[Content_Types].xml": ct.encode()}, {new: payload})


def _code(data: bytes, filename: str, profile: UploadProfile) -> str:
    with pytest.raises(UploadSecurityError) as exc:
        inspect_upload(data, filename, profile)
    assert isinstance(exc.value.detail, dict)
    return str(exc.value.detail["code"])


CASES = [
    pytest.param(UploadProfile.DOCUMENT, "docx", id="document-docx"),
    pytest.param(UploadProfile.TIMETABLE, "docx", id="timetable-docx"),
    pytest.param(UploadProfile.TIMETABLE, "xlsx", id="timetable-xlsx"),
]


def _base(kind: str) -> tuple[bytes, str, str, str]:
    """(package, main part name, content type, file name)."""
    if kind == "docx":
        return _docx(), "word/document.xml", DOCX_MAIN, "syllabus.docx"
    return _xlsx(), "xl/workbook.xml", XLSX_MAIN, "timetable.xlsx"


@pytest.mark.parametrize(("profile", "kind"), CASES)
def test_a_main_part_with_a_non_xml_name_is_scanned(profile: UploadProfile, kind: str) -> None:
    """Root relationship and Override point at word/x.dat (or xl/x.dat): the libraries load it as the main
    part, so an entity declaration in it must be refused like one in word/document.xml."""
    data, old, content_type, filename = _base(kind)
    folder = old.split("/")[0]
    hostile = _with_main_part_renamed(data, old, f"{folder}/x.dat", content_type)
    assert _code(hostile, filename, profile) == "xml_entity_expansion_forbidden"


@pytest.mark.parametrize(("profile", "kind"), CASES)
@pytest.mark.parametrize("name", ["customXml/item1.dat", "word/notes.txt", "docProps/extra", "xl/data.blob", "word/media/image1.dat"])
def test_a_doctype_in_a_part_with_a_non_xml_name_is_rejected(profile: UploadProfile, kind: str, name: str) -> None:
    data, _old, _ct, filename = _base(kind)
    hostile = _rewrite(data, {}, {name: ENTITY})
    assert _code(hostile, filename, profile) == "xml_entity_expansion_forbidden"


@pytest.mark.parametrize(("profile", "kind"), CASES)
def test_a_part_declared_as_xml_is_scanned_even_with_a_binary_looking_name(profile: UploadProfile, kind: str) -> None:
    data, _old, _ct, filename = _base(kind)
    ct = _read(data, "[Content_Types].xml").replace(
        "</Types>", '<Override PartName="/word/looks-like.png" ContentType="application/vnd.example+xml"/></Types>'
    )
    hostile = _rewrite(data, {"[Content_Types].xml": ct.encode()}, {"word/looks-like.png": ENTITY})
    assert _code(hostile, filename, profile) == "xml_entity_expansion_forbidden"


@pytest.mark.parametrize(("profile", "kind"), CASES)
def test_a_default_content_type_of_xml_covers_every_part_with_that_extension(profile: UploadProfile, kind: str) -> None:
    data, _old, _ct, filename = _base(kind)
    ct = _read(data, "[Content_Types].xml").replace(
        "</Types>", '<Default Extension="gif" ContentType="application/xml"/></Types>'
    )
    hostile = _rewrite(data, {"[Content_Types].xml": ct.encode()}, {"word/media/anim.gif": ENTITY})
    assert _code(hostile, filename, profile) == "xml_entity_expansion_forbidden"


@pytest.mark.parametrize("name", ["word/data.txt", "docProps/blob.dat"])
def test_malformed_xml_in_a_part_without_an_xml_name_is_rejected_too(name: str) -> None:
    hostile = _rewrite(_docx(), {}, {name: b"<not-closed"})
    assert _code(hostile, "syllabus.docx", UploadProfile.DOCUMENT) == "invalid_xml"


def test_ordinary_packages_with_binary_parts_are_still_accepted() -> None:
    """Pictures, fonts, printer settings and thumbnails are binary by extension and are not parsed as XML."""
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    binary = {
        "word/media/image1.png": png,
        "word/media/image2.jpeg": b"\xff\xd8\xff" + bytes(range(256)),
        "word/media/image3.emf": b"\x01\x00\x00\x00" + bytes(range(256)),
        "word/fonts/font1.odttf": bytes(range(256)) * 2,
        "word/printerSettings/printerSettings1.bin": bytes(range(256)) * 2,
        "docProps/thumbnail.wmf": b"\xd7\xcd\xc6\x9a" + bytes(range(64)),
    }
    for profile, data, filename in (
        (UploadProfile.DOCUMENT, _rewrite(_docx(), {}, binary), "a.docx"),
        (UploadProfile.TIMETABLE, _rewrite(_docx(), {}, binary), "a.docx"),
        (UploadProfile.TIMETABLE, _rewrite(_xlsx(), {}, {"xl/printerSettings/printerSettings1.bin": bytes(range(256))}), "a.xlsx"),
    ):
        assert inspect_upload(data, filename, profile).kind in ("docx", "xlsx")


def test_the_libraries_own_default_packages_are_accepted_by_both_profiles() -> None:
    assert inspect_upload(_docx(), "a.docx", UploadProfile.DOCUMENT).kind == "docx"
    assert inspect_upload(_docx(), "a.docx", UploadProfile.TIMETABLE).kind == "docx"
    assert inspect_upload(_xlsx(), "a.xlsx", UploadProfile.TIMETABLE).kind == "xlsx"
    assert inspect_upload(pptx_bytes(lambda p: p.slides.add_slide(p.slide_layouts[6])), "a.pptx", UploadProfile.DOCUMENT).kind == "pptx"
