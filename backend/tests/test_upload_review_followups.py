"""
Review of step 5a-3, findings 5, 6, 7 and 8: zip entry names, the slide count, large hostile
files, ActiveX / altChunk and full XML parsing of word/ and xl/ parts.

Pure unit tests: no database, no Redis.
"""

import asyncio
import time
import tracemalloc
import zipfile
from typing import Any

import pytest

from app.core import upload_security
from app.core.upload_security import (
    UploadProfile,
    UploadSecurityError,
    validate_upload,
    validate_upload_async,
)
from tests.security.upload_assertions import (
    CT,
    CT_XML,
    EMPTY_XML,
    MB,
    assert_slide_cap_counts_slide_list_entries,
    assert_zip_member_names_rejected,
    code_of,
    make_zip,
    zip_entries,
)

DOC = UploadProfile.DOCUMENT
TT = UploadProfile.TIMETABLE
XXE = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><root>&a;</root>'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def reject(data: bytes, name: str, profile: UploadProfile = DOC, **kw: int) -> str:
    with pytest.raises(UploadSecurityError) as exc:
        validate_upload(data, name, profile, **kw)
    return code_of(exc.value)


def package(kind: str, extra: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """(zip bytes, file name) of a minimal docx, pptx or xlsx plus the extra parts."""
    main = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}[kind]
    return zip_entries([(CT, CT_XML), (main, EMPTY_XML), *extra]), f"a.{kind}"


def profile_for(kind: str) -> UploadProfile:
    return TT if kind == "xlsx" else DOC  # xlsx is only a timetable format


def rels(rel_type: str, target: str, mode: str = "") -> bytes:
    return (
        f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" Type="{OD}/{rel_type}" Target="{target}"{mode}/>'
        "</Relationships>"
    ).encode()


ROOT = {"docx": "word", "pptx": "ppt", "xlsx": "xl"}


# ─── Finding 5: zip entry names ──────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
def test_case_variant_duplicate_names_are_rejected(kind: str) -> None:
    root = ROOT[kind]
    for first, second in ((f"{root}/a/x.xml", f"{root.upper()}/a/x.xml"), (f"{root.upper()}/a/x.xml", f"{root}/a/x.xml")):
        data, name = package(kind, [(first, EMPTY_XML), (second, XXE)])
        assert reject(data, name, profile_for(kind)) == "invalid_openxml_format", (first, second)


def test_the_unchecked_variant_cannot_hide_an_xxe_part() -> None:
    """The review case: ppt/slides/slide1.xml and PPT/slides/slide1.xml (the second one was never checked)."""
    data, name = package("pptx", [("ppt/slides/slide1.xml", EMPTY_XML), ("PPT/slides/slide1.xml", XXE)])
    assert reject(data, name) == "invalid_openxml_format"


def test_exact_duplicate_names_are_rejected() -> None:
    data, name = package("docx", [("word/a.xml", EMPTY_XML), ("word/a.xml", EMPTY_XML)])
    assert reject(data, name) == "invalid_openxml_format"


def test_names_that_collide_only_after_unicode_normalisation_and_casefold_are_rejected() -> None:
    nfc, nfd = "word/\u00e9.xml", "word/e\u0301.xml"
    assert nfc != nfd
    data, name = package("docx", [(nfc, EMPTY_XML), (nfd, EMPTY_XML)])
    assert reject(data, name) == "invalid_openxml_format"
    data, name = package("docx", [("word/stra\u00dfe.xml", EMPTY_XML), ("word/STRASSE.xml", EMPTY_XML)])
    assert reject(data, name) == "invalid_openxml_format"


def test_names_cut_at_a_nul_byte_by_zipfile_collide_and_are_rejected() -> None:
    raw = zip_entries([("word/aXb.xml", EMPTY_XML), ("word/aYb.xml", EMPTY_XML), (CT, CT_XML), ("word/document.xml", EMPTY_XML)])
    raw = raw.replace(b"word/aXb.xml", b"word/a\x00b.xml").replace(b"word/aYb.xml", b"word/a\x00c.xml")
    assert reject(raw, "a.docx") == "invalid_openxml_format"


@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
@pytest.mark.parametrize(
    "bad",
    [
        "/word/x.xml",  # absolute
        "\\word\\x.xml",  # starts with a backslash
        "word\\x.xml",  # contains a backslash
        "word/..\\x.xml",
        "word/../x.xml",
        "../x.xml",
        "word/./../x.xml",
        "word/x:y.xml",  # colon (drive letters, alternate data streams)
        "C:/x.xml",
        "word/x\x01y.xml",  # control characters
        "word/x\ny.xml",
        "word/x\x7fy.xml",
    ],
)
def test_unsafe_member_names_are_rejected(kind: str, bad: str) -> None:
    data, name = package(kind, [(bad, EMPTY_XML)])
    assert reject(data, name, profile_for(kind)) == "invalid_openxml_format", repr(bad)


def test_nul_in_a_name_is_unsafe() -> None:
    """zipfile itself cuts names at NUL, so the rule is tested on the name check directly."""
    assert upload_security._unsafe_member_name("word/a\x00b.xml") is True
    assert upload_security._unsafe_member_name("word/a.xml") is False


def test_dotdot_inside_a_segment_name_is_not_a_traversal() -> None:
    data, name = package("docx", [("word/a..b.xml", EMPTY_XML), ("word/media/image..1.png", b"x")])
    assert validate_upload(data, name, DOC) == "docx"


def test_directory_entries_and_normal_names_still_pass() -> None:
    data, name = package("docx", [("word/", b""), ("word/media/", b""), ("word/media/a.png", b"x"), ("_rels/.rels", rels("officeDocument", "word/document.xml"))])
    assert validate_upload(data, name, DOC) == "docx"


# ─── Finding 6: the slide cap counts p:sldId entries ─────────────────────────

def presentation(n: int, prefix: str = "p") -> bytes:
    ids = "".join(f'<{prefix}:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(n))
    return (
        f'<{prefix}:presentation xmlns:{prefix}="{P_NS}" xmlns:r="{OD}"><{prefix}:sldIdLst>{ids}</{prefix}:sldIdLst>'
        f"</{prefix}:presentation>"
    ).encode()


def deck(n_entries: int, part_names: list[str] | None = None, prefix: str = "p") -> bytes:
    parts = [(p, EMPTY_XML) for p in (part_names or [])]
    return zip_entries([(CT, CT_XML), ("ppt/presentation.xml", presentation(n_entries, prefix)), *parts])


def test_slide_cap_counts_sld_id_entries_even_when_slide_parts_have_other_names() -> None:
    renamed = [f"ppt/slides/renamed_{i}.xml" for i in range(101)]
    assert reject(deck(101, renamed), "a.pptx") == "too_many_slides"
    assert reject(deck(101, []), "a.pptx") == "too_many_slides"  # no slide parts at all
    assert reject(deck(101, [f"ppt/s/{i}.xml" for i in range(101)]), "a.pptx") == "too_many_slides"


def test_slide_cap_boundary_and_parameter() -> None:
    assert validate_upload(deck(100), "a.pptx", DOC) == "pptx"
    assert reject(deck(101), "a.pptx") == "too_many_slides"
    assert reject(deck(4), "a.pptx", max_pptx_slides=3) == "too_many_slides"
    assert validate_upload(deck(4), "a.pptx", DOC, max_pptx_slides=4) == "pptx"


def test_slide_cap_does_not_depend_on_the_namespace_prefix() -> None:
    assert reject(deck(101, prefix="zz"), "a.pptx") == "too_many_slides"


def test_slide_parts_without_sld_id_entries_are_not_slides() -> None:
    parts = [f"ppt/slides/slide{i}.xml" for i in range(1, 151)]
    assert validate_upload(deck(5, parts), "a.pptx", DOC) == "pptx"


def test_timetable_profile_still_does_not_count_slides() -> None:
    assert validate_upload(deck(101), "a.pptx", TT) == "pptx"


# ─── Finding 8: ActiveX and altChunk for every format ────────────────────────

@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
@pytest.mark.parametrize("part", ["{r}/activeX/activeX1.xml", "{r}/ActiveX/ax1.bin", "{r}/sub/activex/a.xml", "{r}/activeX/_rels/activeX1.xml.rels"])
def test_activex_is_rejected_for_every_format_and_profile(kind: str, part: str) -> None:
    data, name = package(kind, [(part.format(r=ROOT[kind]), EMPTY_XML)])
    profiles = [DOC, TT] if kind != "xlsx" else [TT]
    for profile in profiles:
        assert reject(data, name, profile) == "embedded_content", (kind, part, profile)


@pytest.mark.parametrize("kind, rel_path", [("docx", "word/_rels/document.xml.rels"), ("pptx", "ppt/_rels/presentation.xml.rels"), ("xlsx", "xl/_rels/workbook.xml.rels")])
@pytest.mark.parametrize("rel_type", ["aFChunk", "afchunk", "AFCHUNK"])
def test_alt_chunk_relationship_is_rejected_for_every_format(kind: str, rel_path: str, rel_type: str) -> None:
    data, name = package(kind, [(rel_path, rels(rel_type, "afchunk.mht")), (f"{ROOT[kind]}/afchunk.mht", b"MIME-Version: 1.0")])
    profiles = [DOC, TT] if kind != "xlsx" else [TT]
    for profile in profiles:
        assert reject(data, name, profile) == "embedded_content", (kind, rel_type, profile)


def test_alt_chunk_lookalike_types_and_ordinary_relationships_still_pass() -> None:
    ok = package("docx", [("word/_rels/document.xml.rels", rels("styles", "styles.xml")), ("word/styles.xml", EMPTY_XML)])
    assert validate_upload(*ok, DOC) == "docx"
    lookalike = package("docx", [("word/_rels/document.xml.rels", rels("aFChunkX", "x.xml")), ("word/x.xml", EMPTY_XML)])
    assert validate_upload(*lookalike, DOC) == "docx"


@pytest.mark.parametrize("profile", [DOC, TT])
def test_docx_parts_are_all_parsed_with_defusedxml(profile: UploadProfile) -> None:
    for part in ("word/styles.xml", "word/header1.xml", "word/footnotes.xml", "word/theme/theme1.xml"):
        data, name = package("docx", [(part, XXE)])
        assert reject(data, name, profile) == "xml_entity_expansion_forbidden", (part, profile)
        data, name = package("docx", [(part, b"<not xml")])
        assert reject(data, name, profile) == "invalid_xml", (part, profile)


def test_xlsx_parts_are_all_parsed_with_defusedxml() -> None:
    for part in ("xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/styles.xml", "xl/charts/chart1.xml"):
        data, name = package("xlsx", [(part, XXE)])
        assert reject(data, name, TT) == "xml_entity_expansion_forbidden", part
        data, name = package("xlsx", [(part, b"<not xml")])
        assert reject(data, name, TT) == "invalid_xml", part


def test_xl_parts_inside_a_docx_are_parsed_too() -> None:
    data, name = package("docx", [("xl/worksheets/sheet1.xml", XXE)])
    assert reject(data, name) == "xml_entity_expansion_forbidden"


def test_well_formed_parts_and_non_xml_parts_still_pass() -> None:
    data, name = package("docx", [("word/styles.xml", EMPTY_XML), ("word/media/a.png", b"\x89PNG not xml <<<")])
    assert validate_upload(data, name, DOC) == "docx"
    data, name = package("xlsx", [("xl/worksheets/sheet1.xml", EMPTY_XML)])
    assert validate_upload(data, name, TT) == "xlsx"


def test_unparsed_ppt_parts_keep_the_timetable_behaviour() -> None:
    data, name = package("pptx", [("ppt/slides/slide1.xml", XXE)])
    assert validate_upload(data, name, TT) == "pptx"
    assert reject(data, name, DOC) == "xml_entity_expansion_forbidden"


# ─── Finding 7: a 50 MB hostile file stays bounded and does not block the loop ──

HOSTILE_BUDGET_SECONDS = 25.0  # measured at a few seconds on a development machine; generous for CI
LOOP_STALL_SECONDS = 1.0  # the longest the event loop may go without running while a validation is in flight
MEMORY_BUDGET_BYTES = 60 * MB  # Python-allocated peak while validating a hostile sample


def _hostile_hex_pdf(escapes: int) -> bytes:
    """A PDF whose body is millions of #xx escapes: the hex decoder's worst case."""
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /N " + b"#41" * escapes + b" >>\nendobj\n"


def _flat_xml_docx(elements: int) -> bytes:
    """A docx whose word/document.xml has many small sibling elements (the attribute varies, so it compresses little)."""
    body = b"<r>" + b"".join(b'<a i="%d"/>' % i for i in range(elements)) + b"</r>"
    return make_zip({CT: CT_XML, "word/document.xml": body}, compression=zipfile.ZIP_STORED)


async def _validate_while_watching_the_loop(data: bytes, name: str) -> tuple[str | UploadSecurityError, float, float]:
    """validate_upload_async while a heartbeat task runs. Returns (result or error, seconds, longest loop gap)."""
    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.02)

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    outcome: str | UploadSecurityError
    try:
        outcome = await validate_upload_async(data, name, DOC)
    except UploadSecurityError as err:
        outcome = err
    ticks.append(time.perf_counter())
    elapsed = time.perf_counter() - started
    stop.set()
    await task
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    return outcome, elapsed, max(gaps, default=0.0)


async def test_validating_a_50_mb_hex_escape_pdf_is_bounded_and_keeps_the_loop_responsive() -> None:
    data = _hostile_hex_pdf(16 * MB)
    assert len(data) > 48 * MB
    outcome, elapsed, longest_gap = await _validate_while_watching_the_loop(data, "a.pdf")
    assert outcome == "pdf"
    assert elapsed < HOSTILE_BUDGET_SECONDS, f"hex decoding of 50 MB took {elapsed:.1f} s"
    assert longest_gap < LOOP_STALL_SECONDS, f"the event loop stalled for {longest_gap:.2f} s"


async def test_validating_a_50_mb_pdf_of_object_headers_keeps_the_loop_responsive() -> None:
    """The object-count regex must not run over the whole file in one call (it holds the GIL)."""
    data = b"%PDF-1.4\n" + b"1 0 obj\n" * (6 * MB)
    outcome, elapsed, longest_gap = await _validate_while_watching_the_loop(data, "a.pdf")
    assert isinstance(outcome, UploadSecurityError) and code_of(outcome) == "pdf_too_complex"
    assert elapsed < HOSTILE_BUDGET_SECONDS
    assert longest_gap < LOOP_STALL_SECONDS, f"the event loop stalled for {longest_gap:.2f} s"


async def test_validating_a_deeply_nested_25_mb_xml_part_fails_fast_and_closed() -> None:
    depth = 25 * MB // 3
    data = make_zip({CT: CT_XML, "word/document.xml": b"<a>" * depth}, compression=zipfile.ZIP_STORED)
    outcome, elapsed, longest_gap = await _validate_while_watching_the_loop(data, "a.docx")
    assert isinstance(outcome, UploadSecurityError) and code_of(outcome) == "xml_too_complex"
    assert elapsed < HOSTILE_BUDGET_SECONDS, f"parsing took {elapsed:.1f} s"
    assert longest_gap < LOOP_STALL_SECONDS


async def test_validating_a_24_mb_xml_part_with_millions_of_elements_is_bounded() -> None:
    data = _flat_xml_docx(1_500_000)
    assert len(data) > 20 * MB
    outcome, elapsed, longest_gap = await _validate_while_watching_the_loop(data, "a.docx")
    assert outcome == "docx"
    assert elapsed < HOSTILE_BUDGET_SECONDS, f"parsing took {elapsed:.1f} s"
    assert longest_gap < LOOP_STALL_SECONDS, f"the event loop stalled for {longest_gap:.2f} s"


def _peak_python_memory(call: Any) -> int:
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_hex_escape_decoding_memory_does_not_grow_with_the_file() -> None:
    """2M escapes: decoding them all at once needs hundreds of MB; slice by slice needs a few."""
    data = _hostile_hex_pdf(2_000_000)
    peak = _peak_python_memory(lambda: validate_upload(data, "a.pdf", DOC))
    assert peak < MEMORY_BUDGET_BYTES, f"peak {peak / MB:.0f} MB"


def test_xml_validation_memory_does_not_grow_with_the_part() -> None:
    """A tree of 500 000 elements (or 2M nested ones) needs hundreds of MB; a stream needs almost nothing."""
    data = _flat_xml_docx(500_000)
    peak = _peak_python_memory(lambda: validate_upload(data, "a.docx", DOC))
    assert peak < MEMORY_BUDGET_BYTES, f"peak {peak / MB:.0f} MB"
    nested = make_zip({CT: CT_XML, "word/document.xml": b"<a>" * 2_000_000}, compression=zipfile.ZIP_STORED)
    peak = _peak_python_memory(lambda: reject(nested, "a.docx"))
    assert peak < MEMORY_BUDGET_BYTES, f"peak {peak / MB:.0f} MB"


def test_pdf_scan_is_exact_across_slice_boundaries() -> None:
    """Names, pages and objects that sit on a slice cut are still seen once and only once."""
    slice_size = upload_security._PDF_SCAN_SLICE
    for shift in range(-70, 70, 7):
        head = b"%PDF-1.4\n" + b" " * (slice_size + shift - len(b"%PDF-1.4\n"))
        assert reject(head + b"/J#61vaScript\n", "a.pdf") == "pdf_active_content", shift
        assert reject(head + b"/Type /Page\n" * 4, "a.pdf", max_pdf_pages=3) == "too_many_pages", shift
        assert validate_upload(head + b"/Type /Page\n" * 3, "a.pdf", DOC, max_pdf_pages=3) == "pdf", shift
        many = head + b"1 0 obj\n" * (upload_security.MAX_PDF_OBJECTS + 1)
        assert reject(many, "a.pdf") == "pdf_too_complex", shift
        exact = head + b"1 0 obj\n" * upload_security.MAX_PDF_OBJECTS
        assert validate_upload(exact, "a.pdf", DOC) == "pdf", shift


# ─── The shared helpers (also switched off in security/test_prove_it_fails_review2.py) ──

def test_zip_member_name_rules_hold_for_every_format() -> None:
    assert_zip_member_names_rejected()


def test_slide_cap_counts_the_slide_list_helper() -> None:
    assert_slide_cap_counts_slide_list_entries()
