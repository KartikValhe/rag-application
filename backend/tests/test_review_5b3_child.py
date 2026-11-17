"""
Regression tests for the read-only review of step 5b-3, child side (findings 1, 3, 4, 6, 8, 9, 10, 11 and
the PPTX group depth). Written BEFORE the fixes: each one failed on the 5b-3 code (see the 5b-3 review
record in docs/phase5-design.md). They call the child in-process (`document_parse_child.handle`), the
same code the sandbox runs; the sandbox and worker side is in tests/test_review_5b3_parent.py.

Timing tests are marked `machine_dependent` (bounds are generous and relative; see pyproject.toml).
"""

import time
import unicodedata
from typing import Any

import pytest
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn

from app.services import document_extractors as ex
from app.services.document_text import is_clean_text
from app.services.parsed_document import MAX_BLOCK_CHARS
from tests.fixtures.parse_files import docx_bytes, make_text_pdf, pptx_bytes, rewrite_zip_member
from tests.test_document_extract import blocks, fails, ok, run

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# ── 1. An unexpected error in the child is PERMANENT (parse_failed), never a retry loop ─────────────


def _outline_docx(value: str) -> bytes:
    def build(d: Any) -> None:
        p = d.add_paragraph("A paragraph with a hostile outline level")
        el = OxmlElement("w:outlineLvl")
        el.set(qn("w:val"), value)
        p._p.get_or_add_pPr().append(el)

    return docx_bytes(build)


@pytest.mark.parametrize(
    "value", ["²", "1" * 5000, "٣", "１", "009", "-1", "1.5"], ids=["superscript", "5000-digits", "arabic-indic", "fullwidth", "3-digits", "negative", "float"]
)
def test_a_malformed_outline_level_is_a_permanent_parse_failed(value: str) -> None:
    assert fails(_outline_docx(value), "docx") == "parse_failed"


@pytest.mark.parametrize("value", ["²", "1" * 5000])
def test_a_malformed_outline_level_in_a_style_is_a_permanent_parse_failed(value: str) -> None:
    def build(d: Any) -> None:
        d.styles.element.append(
            parse_xml(
                f'<w:style {W} w:type="paragraph" w:styleId="Odd"><w:name w:val="Odd"/>'
                f'<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="{value}"/></w:pPr></w:style>'
            )
        )
        d.add_paragraph("Styled").style = d.styles.get_by_id("Odd", 1)

    assert fails(docx_bytes(build), "docx") == "parse_failed"


@pytest.mark.parametrize("value", ["0", "2", "8", "9", "10", "99"])
def test_a_well_formed_outline_level_still_works(value: str) -> None:
    assert ok(_outline_docx(value), "docx").stats.blocks == 1


def test_a_pptx_slide_list_pointing_at_a_missing_part_is_a_permanent_parse_failed() -> None:
    """python-pptx raises KeyError while iterating the slides, after the package opened fine."""
    def build(p: Any) -> None:
        p.slides.add_slide(p.slide_layouts[5]).shapes.title.text_frame.text = "T"

    base = pptx_bytes(build)
    import re

    def point_elsewhere(old: bytes) -> bytes:
        xml = old.decode()
        rid = re.search(r'<p:sldId [^>]*r:id="(rId\d+)"', xml)
        assert rid is not None
        return xml.replace(f'r:id="{rid.group(1)}"', 'r:id="rId99"').encode()

    result = run(rewrite_zip_member(base, "ppt/presentation.xml", point_elsewhere), "pptx")
    assert result == {"success": False, "error_code": "parse_failed", "error_type": "KeyError"}  # the class name only


@pytest.mark.parametrize(
    "exc", [ValueError("x"), KeyError("k"), AttributeError("a"), RecursionError("r"), MemoryError("m"), OSError("o")]
)
def test_any_unexpected_exception_in_extraction_is_answered_as_parse_failed(
    exc: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise type(exc)(f"CANARY-EXCEPTION-TEXT /secret/path {exc}")

    monkeypatch.setattr(ex, "extract_document", boom)
    result = run(b"Unit 1", "txt")
    # Only the exception CLASS NAME is reported, for the server log; never its text
    assert result == {"success": False, "error_code": "parse_failed", "error_type": type(exc).__name__}
    assert "CANARY" not in repr(result)


def test_an_unexpected_exception_during_the_upload_recheck_is_parse_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import upload_security

    def boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("CANARY a library bug while validating")

    monkeypatch.setattr(upload_security, "inspect_upload", boom)
    result = run(b"Unit 1", "txt")
    assert result["success"] is False and result["error_code"] == "parse_failed"
    assert "CANARY" not in repr(result)


def test_a_real_gate_error_is_still_answered_with_its_own_code() -> None:
    assert fails(b"   \n\n  ", "txt") == "no_text_found"


# ── 3. The Markdown heading check is linear and the soft deadline can fire ───────────────────────────


MD_HEADING_CASES = [
    ("# Title", (1, "Title")),
    ("###### Six", (6, "Six")),
    ("####### Seven", None),
    ("#NoSpace", None),
    ("   ## Indented three", (2, "Indented three")),
    ("    ## Indented four", None),
    ("# Closed ##", (1, "Closed")),
    ("# Closed with spaces ##   ", (1, "Closed with spaces")),
    ("## a ## ##", (2, "a ##")),
    ("# ##", (1, "##")),
    ("#", (1, "")),
    ("# \t ", (1, "")),
    ("#\ttab", (1, "tab")),
    ("# foo#", (1, "foo#")),
]


@pytest.mark.parametrize(("line", "expected"), MD_HEADING_CASES)
def test_markdown_heading_lines_keep_their_meaning(line: str, expected: tuple[int, str] | None) -> None:
    """Behaviour of the old regex, pinned through the real extractor (heading blocks of one line)."""
    doc_bytes = f"{line}\n\nbody text here\n".encode()
    result = run(doc_bytes, "md")
    if expected is None:
        assert result["success"] is True
        assert all(b["t"] == "para" for b in result["document"]["pages"][0]["blocks"])
        return
    level, text = expected
    if text == "":
        # an empty heading adds no block; the body paragraph remains
        assert [b["t"] for b in result["document"]["pages"][0]["blocks"]] == ["para"]
        return
    first = result["document"]["pages"][0]["blocks"][0]
    assert (first["t"], first["level"], first["text"]) == ("heading", level, text)


@pytest.mark.machine_dependent
def test_a_hostile_nearly_two_megabyte_heading_line_is_linear() -> None:
    """'# a' + spaces + 'b' made the old regex quadratic, inside C code the soft deadline cannot interrupt."""
    import asyncio

    from app.services.document_limits import default_limits
    from app.workers import stages

    settings = stages.ParseSettings(timeout_seconds=20.0, max_output_bytes=14 * 1024 * 1024, memory_limit_mb=1024, cpu_seconds=60)
    data = ("# a" + " " * 1_900_000 + "b\n\nbody\n").encode()  # under the text cap: the pattern must not hang

    async def go() -> float:
        started = time.monotonic()
        await stages.parse_in_sandbox(data, file_kind="md", limits=default_limits(), parse=settings)
        return time.monotonic() - started

    elapsed = asyncio.run(go())
    assert elapsed < 10.0, f"took {elapsed:.1f} s"  # the hostile line itself costs milliseconds


def test_a_long_line_is_never_a_heading() -> None:
    line = "# " + "word " * 300  # over 1024 characters
    doc = ok(f"{line}\n\nbody\n".encode(), "md")
    assert [b[1] for b in blocks(doc)] == ["para", "para"]


def test_a_line_at_the_limit_is_still_a_heading() -> None:
    line = "# " + "x" * 1000
    assert blocks(ok(f"{line}\n".encode(), "md"))[0][1:3] == ("heading", 1)


def test_the_hand_written_heading_check_agrees_with_the_regular_expression_it_replaced() -> None:
    """Differential test on short random lines: the 5b-3 regular expression, kept here as the oracle."""
    import random
    import re

    oracle = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
    rng = random.Random(8)
    for _ in range(20_000):
        line = "".join(rng.choice("## \tab") for _ in range(rng.randint(0, 14)))
        match = oracle.match(line)
        expected = (len(match.group(1)), match.group(2) or "") if match else None
        assert ex._md_heading(line) == expected, repr(line)


# ── 4. The text cap is checked on the RAW length; splitting is linear ────────────────────────────────


def test_a_ten_megabyte_single_paragraph_fails_before_cleaning_or_splitting(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the raw length must be checked before clean_text and split_text")

    monkeypatch.setattr(ex, "clean_text", forbidden)
    monkeypatch.setattr(ex, "split_text", forbidden)
    data = (b"word " * 2_000_000)[:10_000_000]
    assert fails(data, "txt", max_file_bytes=20 * 1024 * 1024) == "text_too_large"


def test_many_lines_of_one_paragraph_are_refused_without_building_the_whole_join(monkeypatch: pytest.MonkeyPatch) -> None:
    joins: list[int] = []
    real_add = ex.Collector.add

    def spy(self: Any, kind: str, raw: str, level: int | None = None) -> int:
        joins.append(len(raw))
        return real_add(self, kind, raw, level)

    monkeypatch.setattr(ex.Collector, "add", spy)
    data = ("word word word\n" * 700_000).encode()  # 10.5 MB, one paragraph (no blank line)
    assert fails(data, "txt", max_file_bytes=20 * 1024 * 1024) == "text_too_large"
    assert max(joins, default=0) <= 2_000_001


@pytest.mark.machine_dependent
def test_split_text_time_grows_linearly_with_the_text() -> None:
    def best(n: int) -> float:
        text = "wordy " * (n // 6)
        times = []
        for _ in range(3):
            started = time.perf_counter()
            pieces = ex.split_text(text)
            times.append(time.perf_counter() - started)
            assert "".join(pieces).replace(" ", "") == text.replace(" ", "")
        return min(times)

    small, large = best(4_000_000), best(16_000_000)
    assert large < small * 9, f"4x the text took {large / small:.1f}x the time (linear is about 4x, quadratic 16x)"


@pytest.mark.machine_dependent
def test_a_legitimate_1_9_million_character_paragraph_is_split_and_kept() -> None:
    import random

    rng = random.Random(3)
    words = ["".join(rng.choice("abcdefghij") for _ in range(rng.randint(2, 9))) for _ in range(2000)]
    text = " ".join(rng.choice(words) for _ in range(400_000))[:1_900_000].strip()
    started = time.monotonic()
    doc = ok(text.encode(), "txt", max_text_chars=2_000_000)
    assert time.monotonic() - started < 20.0
    parts = [b.text for b in doc.pages[0].blocks]
    assert " ".join(parts) == text
    assert all(len(p) <= MAX_BLOCK_CHARS for p in parts)


# ── 6. DOCX hidden text, the cheap part: docDefaults, specVanish, tiny and white runs ────────────────


def _add_xml(d: Any, xml: str) -> None:
    body = d.element.body
    el = parse_xml(xml)
    sect = body.find(qn("w:sectPr"))
    if sect is not None:
        sect.addprevious(el)
    else:
        body.append(el)


def _runs(*runs: tuple[str, str]) -> str:
    """A w:p with runs given as (rPr xml, text)."""
    body = "".join(f"<w:r><w:rPr>{rpr}</w:rPr><w:t>{text}</w:t></w:r>" for rpr, text in runs)
    return f"<w:p {W}>{body}</w:p>"


def _hidden_case(build: Any) -> tuple[list[str], int]:
    doc = ok(docx_bytes(build), "docx")
    return [b.text for p in doc.pages for b in p.blocks], doc.stats.hidden_text_chars


def test_text_hidden_by_the_document_defaults_is_counted_not_extracted() -> None:
    def build(d: Any) -> None:
        defaults = d.styles.element.find(qn("w:docDefaults"))
        if defaults is None:
            defaults = parse_xml(f"<w:docDefaults {W}><w:rPrDefault><w:rPr/></w:rPrDefault></w:docDefaults>")
            d.styles.element.insert(0, defaults)
        defaults.find(qn("w:rPrDefault")).find(qn("w:rPr")).append(OxmlElement("w:vanish"))
        _add_xml(d, _runs(('<w:vanish w:val="0"/>', "Shown on purpose")))
        _add_xml(d, _runs(("", "Hidden by the defaults")))

    texts, hidden = _hidden_case(build)
    assert texts == ["Shown on purpose"]
    assert hidden == len("Hidden by the defaults")


def test_spec_vanish_runs_are_counted_not_extracted() -> None:
    def build(d: Any) -> None:
        _add_xml(d, _runs(("", "Visible. "), ("<w:specVanish/>", "special hidden")))

    texts, hidden = _hidden_case(build)
    assert texts == ["Visible."]
    assert hidden == len("special hidden")


@pytest.mark.parametrize(
    ("rpr", "is_hidden"),
    [
        ('<w:sz w:val="2"/>', True),  # 1 pt
        ('<w:sz w:val="1"/>', True),  # 0.5 pt
        ('<w:sz w:val="3"/>', False),  # 1.5 pt
        ('<w:sz w:val="24"/>', False),  # 12 pt
        ('<w:color w:val="FFFFFF"/>', True),
        ('<w:color w:val="ffffff"/>', True),
        ('<w:color w:val="FFFFFE"/>', False),
        ('<w:color w:val="auto"/>', False),
        ('<w:color w:val="FFFFFF"/><w:shd w:val="clear" w:fill="1F3864"/>', False),  # white on a dark fill
        ('<w:color w:val="FFFFFF"/><w:highlight w:val="darkBlue"/>', False),
    ],
)
def test_tiny_and_white_runs_are_counted_not_extracted(rpr: str, is_hidden: bool) -> None:
    def build(d: Any) -> None:
        _add_xml(d, _runs(("", "Plain. "), (rpr, "suspect text")))

    texts, hidden = _hidden_case(build)
    if is_hidden:
        assert texts == ["Plain."] and hidden == len("suspect text")
    else:
        assert texts == ["Plain. suspect text"] and hidden == 0


def test_white_text_in_a_shaded_table_cell_is_header_text_not_hidden() -> None:
    def build(d: Any) -> None:
        _add_xml(
            d,
            f'<w:tbl {W}><w:tr><w:tc><w:tcPr><w:shd w:val="clear" w:fill="1F3864"/></w:tcPr>'
            '<w:p><w:r><w:rPr><w:color w:val="FFFFFF"/></w:rPr><w:t>Unit</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:rPr><w:color w:val="FFFFFF"/></w:rPr><w:t>Secret</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
        )

    texts, hidden = _hidden_case(build)
    assert texts == ["Unit"]  # the unshaded cell's white text IS hidden; the shaded cell's is a header
    assert hidden == len("Secret")


def test_tiny_text_through_a_character_style_is_counted() -> None:
    def build(d: Any) -> None:
        d.styles.element.append(
            parse_xml(f'<w:style {W} w:type="character" w:styleId="Speck"><w:name w:val="Speck"/><w:rPr><w:sz w:val="2"/></w:rPr></w:style>')
        )
        p = d.add_paragraph("Visible. ")
        p.add_run("speck text").style = d.styles.get_by_id("Speck", 2)

    texts, hidden = _hidden_case(build)
    assert texts == ["Visible."] and hidden == len("speck text")


# ── 8. A hard cut never splits a conjunct (joiners, combining marks, virama) ─────────────────────────

VIRAMA = "्"
ZWJ = "\u200d"
ZWNJ = "\u200c"


def _boundary_problems(pieces: list[str]) -> list[str]:
    problems = []
    for a, b in zip(pieces, pieces[1:], strict=False):
        if b[0] in (ZWJ, ZWNJ):
            problems.append("a piece starts with a joiner")
        if unicodedata.category(b[0]).startswith("M"):
            problems.append("a piece starts with a combining mark")
        if a[-1] in (ZWJ, ZWNJ, VIRAMA):
            problems.append("a piece ends with a joiner or virama")
    return problems


def _text_with(at_limit: str, before: str = "क") -> str:
    """No spaces or sentence ends, so the cut is a hard one at the limit; `at_limit` starts at index `limit`."""
    return before * (MAX_BLOCK_CHARS - 1) + VIRAMA + at_limit + "ष" * 40


def _text_with_nukta_at_the_limit() -> str:
    return "क" * MAX_BLOCK_CHARS + "़" + "ष" * 40  # base letters, then the nukta exactly at the limit


@pytest.mark.parametrize(
    "text",
    [
        _text_with(ZWJ + "ष"),
        _text_with(ZWNJ + "ष"),
        _text_with("ि"),
        _text_with("ं"),
        _text_with_nukta_at_the_limit(),
    ],
    ids=["on-zwj", "on-zwnj", "on-matra", "on-anusvara", "on-nukta"],
)
def test_a_hard_cut_never_lands_on_a_joiner_or_combining_mark(text: str) -> None:
    assert unicodedata.normalize("NFC", text) == text  # the fixture is clean text
    assert text[MAX_BLOCK_CHARS] in (ZWJ, ZWNJ, "ि", "ं", "़")
    pieces = ex.split_text(text)
    assert len(pieces) >= 2 and all(len(p) <= MAX_BLOCK_CHARS for p in pieces)
    assert _boundary_problems(pieces) == []
    assert all(is_clean_text(p) for p in pieces), "the parent would reject the whole document"
    assert "".join(pieces) == text  # nothing lost


def test_a_hard_cut_never_lands_right_after_a_joiner() -> None:
    text = "क" * (MAX_BLOCK_CHARS - 2) + VIRAMA + ZWJ + "ष" * 40  # the joiner is the last character of a piece
    pieces = ex.split_text(text)
    assert _boundary_problems(pieces) == []
    assert "".join(pieces) == text


def test_a_long_devanagari_paragraph_whose_natural_cut_falls_on_a_joiner_is_accepted_end_to_end() -> None:
    text = _text_with(ZWJ + "ष")
    doc = ok(text.encode(), "txt")  # ok() runs the PARENT's validation of every block
    assert "".join(b.text for b in doc.pages[0].blocks) == text


def test_text_with_no_safe_cut_anywhere_still_gives_clean_pieces() -> None:
    text = "क" + (VIRAMA + ZWJ) * (3 * MAX_BLOCK_CHARS)
    pieces = ex.split_text(text)
    assert all(len(p) <= MAX_BLOCK_CHARS for p in pieces)
    assert all(is_clean_text(p) for p in pieces)
    marks = "क" + "ं" * (3 * MAX_BLOCK_CHARS)
    assert all(is_clean_text(p) and len(p) <= MAX_BLOCK_CHARS for p in ex.split_text(marks))


# ── 9. DOCX table cells: no text boxes, no mc:Fallback copies, nested tables kept ────────────────────

TEXTBOX_RUN = (
    '<w:r><mc:AlternateContent>'
    '<mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData><wps:wsp><wps:txbx>'
    "<w:txbxContent><w:p><w:r><w:t>BOXED</w:t></w:r></w:p></w:txbxContent>"
    "</wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
    '<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r><w:t>BOXED</w:t></w:r></w:p></w:txbxContent>'
    "</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
)
NS_ALL = (
    f'{W} xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml"'
)


def test_a_text_box_inside_a_table_cell_is_not_read_twice() -> None:
    def build(d: Any) -> None:
        _add_xml(
            d,
            f"<w:tbl {NS_ALL}><w:tr><w:tc><w:p><w:r><w:t>Cell text</w:t></w:r></w:p>"
            f"<w:p>{TEXTBOX_RUN}</w:p></w:tc><w:tc><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        )

    rows = [b for b in blocks(ok(docx_bytes(build), "docx")) if b[1] == "table_row"]
    assert [r[3] for r in rows] == ["Cell text | Second"]


def test_nested_tables_and_content_controls_in_a_cell_are_still_read_once() -> None:
    def build(d: Any) -> None:
        _add_xml(
            d,
            f"<w:tbl {NS_ALL}><w:tr><w:tc><w:p><w:r><w:t>Outer</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Inner</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "<w:sdt><w:sdtContent><w:p><w:r><w:t>Control</w:t></w:r></w:p></w:sdtContent></w:sdt>"
            "</w:tc></w:tr></w:tbl>",
        )

    rows = [b[3] for b in blocks(ok(docx_bytes(build), "docx")) if b[1] == "table_row"]
    assert rows == ["Outer Inner Control"]


# ── 10. The child's answer: UTF-8 (not \uXXXX escapes) and non-BMP text survive ──────────────────────


def test_non_bmp_text_in_a_pdf_survives_get_text_range() -> None:
    """The review feared that get_text_range (UCS-2) drops characters outside the BMP. On the pinned
    pypdfium2 it decodes UTF-16, so pairs survive; this test pins that behaviour against a version bump."""
    text = "Math: 𝐀𝐁𝐂 and 𝒳 equals x 😀 done and some more words to pass the scanned gate"
    doc = ok(make_text_pdf([text], unicode_text=True), "pdf")
    assert doc.pages[0].blocks[0].text == text


def test_non_bmp_text_is_cleaned_and_split_without_loss() -> None:
    word = "𝐀𝐁𝐂𝐃"
    text = " ".join([word] * 6000)
    parts = [b.text for b in ok(text.encode(), "txt").pages[0].blocks]
    assert " ".join(parts) == text


# ── 11. Symbol-font private-use code points are not garbled text ─────────────────────────────────────


def test_bullet_and_equation_heavy_text_is_not_unreadable() -> None:
    bullets = " A\n\n" * 60  # Word bullets: one Symbol-font private-use code point, one letter
    equations = " x\n\n" * 60  # Symbol-font alpha beta gamma
    doc = ok((bullets + equations + "A normal paragraph about linear equations.\n").encode(), "txt")
    assert doc.stats.blocks > 100


@pytest.mark.parametrize("base", [0xE000, 0xE100, 0xF100, 0xF200, 0xF700])
def test_real_private_use_garbage_is_still_unreadable(base: int) -> None:
    garbled = "".join(chr(base + (i % 60)) for i in range(120))
    assert fails((garbled + " Unit 1\n").encode(), "txt") == "unreadable_text"


def test_symbol_font_range_counts_as_neither_letters_nor_garbage() -> None:
    from app.services.document_text import count_chars

    counts = count_chars(" abc")
    assert (counts.private_use, counts.letters) == (0, 3)
    assert count_chars("").private_use == 2
    assert count_chars("").private_use == 1


# ── PPTX: groups nested deeper than the walker goes are an error, not silent loss ────────────────────


def _nested_groups_pptx(depth: int) -> bytes:
    from pptx.util import Inches

    def build(p: Any) -> None:
        slide = p.slides.add_slide(p.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(4), Inches(1)).text_frame.text = "Top level text"
        shapes = slide.shapes
        for _ in range(depth):
            shapes = shapes.add_group_shape().shapes
        shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1)).text_frame.text = "Deeply nested text"

    return pptx_bytes(build)


def test_groups_nested_32_deep_are_read() -> None:
    texts = [b[3] for b in blocks(ok(_nested_groups_pptx(ex.MAX_SHAPE_DEPTH), "pptx"))]
    assert "Deeply nested text" in texts


def test_groups_nested_33_deep_fail_instead_of_dropping_their_text() -> None:
    assert fails(_nested_groups_pptx(ex.MAX_SHAPE_DEPTH + 1), "pptx") == "xml_too_complex"


# ── Documented gap: invisible PDF text is extracted (backlog B56) ────────────────────────────────────

HIDDEN_PDF_CASES = {
    "render-mode-3": b"BT /F1 12 Tf 72 400 Td 3 Tr (HIDDEN INJECTION TEXT) Tj ET\n",
    "white-fill": b"BT /F1 12 Tf 72 400 Td 1 1 1 rg (HIDDEN INJECTION TEXT) Tj ET\n",
    "off-page": b"BT /F1 12 Tf 5000 5000 Td (HIDDEN INJECTION TEXT) Tj ET\n",
}


@pytest.mark.xfail(strict=True, reason="B56: invisible PDF text (render mode 3, white, off-page) is not detected yet")
@pytest.mark.parametrize("name", list(HIDDEN_PDF_CASES))
def test_invisible_pdf_text_is_not_extracted_known_gap(name: str) -> None:
    pdf = make_text_pdf(["Visible words on this page, enough of them for the scanned gate."], extra_content=HIDDEN_PDF_CASES[name])
    doc = ok(pdf, "pdf")
    assert "HIDDEN INJECTION" not in " ".join(b.text for p in doc.pages for b in p.blocks)
    assert doc.stats.hidden_text_chars >= len("HIDDEN INJECTION TEXT")
