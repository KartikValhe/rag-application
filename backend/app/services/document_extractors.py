"""
app/services/document_extractors.py
───────────────────────────────────
Real text extraction for the sandboxed document child (step 5b-3, design A3 steps 3-4). Runs ONLY
inside app.services.document_parse_child, after the upload validation has been re-run on the bytes.

  PDF   pypdfium2: text per page; the real page count is re-checked right after opening (a page tree
        in a compressed object stream hides it from the upload scan, backlog B33); page size bound;
        a per-page character count before the text is read; every handle closed; open errors are
        corrupt_pdf, a password is password_protected_pdf.
  DOCX  python-docx (package loading) + lxml walk of the body: paragraphs, headings by outline level
        (paragraph, then style chain) or the built-in style ids Heading1-9 / Title, never by the
        (localized) style name; tables as table_row (only the cell's own paragraphs and nested tables:
        text boxes and mc:Fallback copies are skipped); hidden runs are NOT extracted but their
        characters are counted in stats.hidden_text_chars: w:vanish (on the run, its character style, its
        paragraph style or the document defaults), w:specVanish, tracked deletions, runs of 1 pt or less
        and white runs (FFFFFF) outside a shaded paragraph, cell or table style; images, text boxes,
        headers, footers, footnotes and comments are not read.
  PPTX  python-pptx: slide = page; the title placeholder is slide_title; text frames in reading order
        (top to bottom in 0.25 inch bands, then left to right); groups walked; tables as table_row;
        speaker notes as notes; hidden slides are extracted AND their characters counted.
  TXT   paragraphs split on blank lines. MD: the same, plus ATX headings (# to ######).

Every text goes through document_text.clean_text and is split into blocks of at most MAX_BLOCK_CHARS
(at a sentence end, else a space, else a hard cut that never splits a conjunct). A single text longer
than the whole text cap is refused on its RAW length, before it is cleaned or split. The quality gates
(document_gates) run at the end. Numeric attributes of DOCX are read strictly (_strict_small_int): an
attribute that is not 1-2 ASCII digits makes the file a permanent parse_failed.
Failures raise GateError with a stable code; nothing of the content is ever put in an error.
This module must never import app.core.config.
"""

import io
import re
import time
import unicodedata
from collections.abc import Iterator
from typing import Any

from app.services.document_gates import GateError, check_scanned_pdf, check_unreadable_text
from app.services.document_text import clean_text, count_chars, guess_script
from app.services.parsed_document import (
    MAX_BLOCK_CHARS,
    MAX_BLOCKS_PER_PAGE,
    MAX_BLOCKS_TOTAL,
    MAX_HEADING_LEVEL,
)

MAX_PAGE_POINTS = 14_400.0  # 200 inches, the largest page size PDF viewers support
MAX_PAGE_RAW_CHARS = 500_000  # characters on one PDF page, checked before the text is read
MAX_SHAPE_DEPTH = 32  # nested PPTX groups
READING_BAND_EMU = 228_600  # 0.25 inch: shapes whose tops are this close count as one line
LANG_HINTS = frozenset({"hi", "mr"})
_LANG_SCAN_LIMIT = 500  # language attributes looked at per file

_SENTENCE_ENDS = (". ", "? ", "! ", "। ", "॥ ")
_PARAGRAPH_BREAK_RE = re.compile(r"(?:\r?\n[ \t]*){2,}")
_PDF_LANG_RE = re.compile(rb"/Lang\s{0,8}\(\s{0,4}([A-Za-z]{2,3})(?:[-_][A-Za-z0-9]{1,8}){0,3}\s{0,4}\)")
_MD_MAX_HEADING_LINE = 1024  # a longer line is never a heading (and is never run through a pattern)
_MD_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)")


_JOINERS = "\u200c\u200d"  # ZWNJ, ZWJ
_VIRAMA = "\u094d"


def _md_heading(line: str) -> tuple[int, str] | None:
    """(level, text) of an ATX heading line ("# Title", up to 3 spaces of indent, 1-6 hashes, an optional
    closing run of hashes), else None. Hand-written and linear: the regular expression it replaces was
    quadratic on '# a' + a million spaces + 'b', inside C code that the soft deadline cannot interrupt."""
    if len(line) > _MD_MAX_HEADING_LINE:
        return None
    n = len(line)
    i = 0
    while i < n and i < 3 and line[i] == " ":
        i += 1
    j = i
    while j < n and line[j] == "#":
        j += 1
    level = j - i
    if not 1 <= level <= 6:
        return None
    if j < n and line[j] not in " \t":
        return None
    text = line[j:].strip(" \t")
    k = len(text)
    while k > 0 and text[k - 1] == "#":
        k -= 1
    if 0 < k < len(text) and text[k - 1] in " \t":
        text = text[:k].rstrip(" \t")  # the closing sequence ("# Title ##")
    return level, text


def _unsafe_cut(text: str, cut: int) -> bool:
    """True when cutting `text` into text[:cut] and text[cut:] would split a conjunct: the second piece
    would start with a joiner or a combining mark, or the first would end with a joiner or a virama.
    (A piece that starts with a joiner is not clean text: the parent would refuse the whole document.)"""
    after = text[cut]
    before = text[cut - 1]
    return (
        after in _JOINERS
        or unicodedata.category(after).startswith("M")
        or before in _JOINERS
        or before == _VIRAMA
    )


def split_text(text: str, limit: int = MAX_BLOCK_CHARS) -> list[str]:
    """
    Pieces of at most `limit` characters: cut after a sentence end, else at a space, else hard. A hard cut
    is moved backwards until it does not split a conjunct (_unsafe_cut); only when there is no safe cut at
    all (a text made of nothing but marks and joiners) it cuts at the limit and drops the joiners that
    would start the next piece. Linear: the text is walked with an index and never re-sliced.
    """
    pieces: list[str] = []
    n = len(text)
    start = 0
    while n - start > limit:
        low = start + limit // 2
        high = start + limit + 1
        cut = max(text.rfind(end, low, high) for end in _SENTENCE_ENDS)
        if cut != -1:
            pieces.append(text[start : cut + 1])
            start = cut + 2
            continue
        space = text.rfind(" ", low, high)
        if space != -1:
            pieces.append(text[start:space])
            start = space + 1
            continue
        cut = start + limit
        while cut > start + 1 and _unsafe_cut(text, cut):
            cut -= 1
        if _unsafe_cut(text, cut):
            cut = start + limit  # no safe cut anywhere in this stretch
            pieces.append(text[start:cut])
            start = cut
            while start < n and text[start] in _JOINERS:
                start += 1
            continue
        pieces.append(text[start:cut])
        start = cut
    pieces.append(text[start:])
    return [p.strip(" ") for p in pieces if p.strip(" ")]


def _primary_subtag(value: str | None) -> str:
    return (value or "").replace("_", "-").split("-", 1)[0].strip().lower()


class Collector:
    """Builds the pages and blocks, enforcing the text, block and time budgets as it goes."""

    def __init__(self, *, max_text_chars: int, deadline: float | None) -> None:
        self.max_text_chars = max_text_chars
        self.deadline = deadline
        self.pages: list[dict[str, Any]] = []
        self._blocks: list[dict[str, Any]] | None = None
        self._n = 0
        self.chars = 0
        self.blocks = 0
        self.hidden_text_chars = 0
        self.texts: list[str] = []

    def check_time(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise GateError("parse_timeout")

    def begin_page(self, n: int) -> None:
        self.end_page()
        self._n = n
        self._blocks = []

    def end_page(self) -> None:
        if self._blocks:
            self.pages.append({"n": self._n, "blocks": self._blocks})
        self._blocks = None

    def add(self, kind: str, raw: str, level: int | None = None) -> int:
        """Clean `raw`, split it and add the blocks. Returns the non-space characters added."""
        self.check_time()
        assert self._blocks is not None, "begin_page first"
        if len(raw) > self.max_text_chars:
            # Cleaning only shortens a text, so nothing this long can fit under the cap: refuse it on its
            # RAW length, before clean_text and split_text spend any time or memory on it
            raise GateError("text_too_large")
        text = clean_text(raw)
        if not text:
            return 0
        for i, piece in enumerate(split_text(text)):
            # A heading longer than one block continues as ordinary text
            block_kind = kind if (i == 0 or kind != "heading") else "para"
            self._blocks.append(
                {"t": block_kind, "level": level if block_kind == "heading" else None, "text": piece}
            )
            self.texts.append(piece)
            self.chars += len(piece)
            self.blocks += 1
            if (
                self.chars > self.max_text_chars
                or self.blocks > MAX_BLOCKS_TOTAL
                or len(self._blocks) > MAX_BLOCKS_PER_PAGE
            ):
                raise GateError("text_too_large")
        return len(text) - text.count(" ")


def check_page_count(count: int, cap: int, code: str) -> None:
    """The page (or slide) cap on the count the PARSER sees (backlog B33)."""
    if count > cap:
        raise GateError(code)


# ── PDF ──────────────────────────────────────────────────────────────────────


def _pdf_lang_hint(data: bytes) -> bool:
    for i, match in enumerate(_PDF_LANG_RE.finditer(data)):
        if _primary_subtag(match.group(1).decode("ascii")) in LANG_HINTS:
            return True
        if i >= 20:
            break
    return False


def extract_pdf(data: bytes, *, max_pages: int, collector: Collector) -> tuple[int, list[int], bool]:
    """Returns (page count, non-space characters per page, Hindi/Marathi metadata hint)."""
    import pypdfium2 as pdfium

    password_error = getattr(getattr(pdfium, "raw", None), "FPDF_ERR_PASSWORD", 4)
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise GateError("password_protected_pdf" if exc.err_code == password_error else "corrupt_pdf") from None
    per_page: list[int] = []
    try:
        count = len(pdf)
        # The real page count (B33): the upload scan only saw uncompressed page objects
        check_page_count(count, max_pages, "too_many_pages")
        for index in range(count):
            collector.check_time()
            try:
                width, height = pdf.get_page_size(index)
            except pdfium.PdfiumError:
                raise GateError("corrupt_pdf") from None
            if not (0 < width <= MAX_PAGE_POINTS and 0 < height <= MAX_PAGE_POINTS):
                raise GateError("pdf_too_complex")
            raw = _pdf_page_text(pdf, index, pdfium)
            collector.begin_page(index + 1)
            per_page.append(sum(collector.add("para", part) for part in _PARAGRAPH_BREAK_RE.split(raw)))
            collector.end_page()
    finally:
        pdf.close()
    return count, per_page, _pdf_lang_hint(data)


def _pdf_page_text(pdf: Any, index: int, pdfium: Any) -> str:
    try:
        page = pdf[index]
    except pdfium.PdfiumError:
        raise GateError("corrupt_pdf") from None
    try:
        try:
            textpage = page.get_textpage()
        except pdfium.PdfiumError:
            raise GateError("corrupt_pdf") from None
        try:
            chars = textpage.count_chars()
            # Bound the work on one page before its text is materialized
            if chars > MAX_PAGE_RAW_CHARS:
                raise GateError("text_too_large")
            # get_text_range decodes UTF-16, so characters outside the BMP (surrogate pairs) survive on the
            # pinned pypdfium2 (tests/test_review_5b3_child.py pins it); count_chars counts UTF-16 units
            return str(textpage.get_text_range()) if chars > 0 else ""
        finally:
            textpage.close()
    finally:
        page.close()


# ── DOCX ─────────────────────────────────────────────────────────────────────

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_DESCEND = frozenset(
    W + tag
    for tag in ("ins", "hyperlink", "smartTag", "fldSimple", "sdt", "sdtContent", "customXml", "moveTo", "dir", "bdo", "del", "moveFrom")
)
_HEADING_STYLE_RE = re.compile(r"[Hh]eading([1-9])")
_STYLE_CHAIN_LIMIT = 20


def _on(el: Any) -> bool | None:
    """The value of an OOXML on/off element: None when absent."""
    if el is None:
        return None
    return el.get(W + "val", "true").lower() not in ("0", "false", "off")


def _strict_small_int(value: str | None) -> int | None:
    """An outline level and the like: None when the attribute is absent or empty, the number when it is
    one or two ASCII digits, else GateError("parse_failed") (a permanent error). str.isdigit() alone is
    not enough: it accepts superscripts ("\u00b2", which int() then refuses with a ValueError), Arabic-Indic
    and full-width digits, and a 5000-digit value that int() refuses as well."""
    if not value:
        return None
    if value.isascii() and value.isdigit() and len(value) <= 2:
        return int(value)
    raise GateError("parse_failed")


def _half_points(value: str | None) -> int | None:
    """A w:sz value (half points): up to 4 ASCII digits, else ignored (a size nobody can hide behind)."""
    if value and value.isascii() and value.isdigit() and len(value) <= 4:
        return int(value)
    return None


TINY_HALF_POINTS = 2  # 1 pt: a run at this size or smaller is hidden text
WHITE = "ffffff"


def _is_filled(shd: Any) -> bool:
    """A w:shd (shading) that paints a background other than white or automatic."""
    if shd is None:
        return False
    return (shd.get(W + "fill") or "auto").lower() not in ("auto", WHITE, "")


def _rpr_facts(rpr: Any) -> tuple[bool | None, int | None, bool | None]:
    """(vanish, size in half points, white) of one w:rPr; None for what it does not say. w:specVanish
    counts as vanish."""
    if rpr is None:
        return None, None, None
    vanish = _on(rpr.find(W + "vanish"))
    spec = _on(rpr.find(W + "specVanish"))
    if spec:
        vanish = True
    size_el = rpr.find(W + "sz")
    size = _half_points(size_el.get(W + "val")) if size_el is not None else None
    color = rpr.find(W + "color")
    white = (color.get(W + "val") or "").lower() == WHITE if color is not None and color.get(W + "val") else None
    return vanish, size, white


class _Styles:
    def __init__(self, styles_el: Any) -> None:
        self.based: dict[str, str] = {}
        self.outline: dict[str, int] = {}
        self.vanish: dict[str, bool] = {}
        self.size: dict[str, int] = {}
        self.white: dict[str, bool] = {}
        self.default_paragraph: str | None = None
        # w:docDefaults: what a run is when no style and no run property says otherwise
        self.default_vanish, self.default_size, self.default_white = _rpr_facts(
            styles_el.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        )
        for st in styles_el.iterchildren(W + "style"):
            sid = st.get(W + "styleId")
            if not sid:
                continue
            if st.get(W + "type") == "paragraph" and st.get(W + "default") in ("1", "true"):
                self.default_paragraph = sid
            based = st.find(W + "basedOn")
            if based is not None and based.get(W + "val"):
                self.based[sid] = str(based.get(W + "val"))
            outline = st.find(f"{W}pPr/{W}outlineLvl")
            level = _strict_small_int(outline.get(W + "val")) if outline is not None else None
            if level is not None:
                self.outline[sid] = level
            vanish, size, white = _rpr_facts(st.find(f"{W}rPr"))
            if vanish is not None:
                self.vanish[sid] = vanish
            if size is not None:
                self.size[sid] = size
            if white is not None:
                self.white[sid] = white

    def resolve(self, sid: str | None, table: dict[str, Any]) -> Any:
        seen = 0
        while sid and seen < _STYLE_CHAIN_LIMIT:
            if sid in table:
                return table[sid]
            sid = self.based.get(sid)
            seen += 1
        return None


def _heading_level(p: Any, styles: _Styles) -> int | None:
    ppr = p.find(W + "pPr")
    style_id: str | None = None
    direct = None
    if ppr is not None:
        pstyle = ppr.find(W + "pStyle")
        style_id = pstyle.get(W + "val") if pstyle is not None else None
        direct = ppr.find(W + "outlineLvl")
    style_id = style_id or styles.default_paragraph
    level = _strict_small_int(direct.get(W + "val")) if direct is not None else None
    if level is None:
        level = styles.resolve(style_id, styles.outline)
    if level is not None:
        return level + 1 if 0 <= level < MAX_HEADING_LEVEL else None  # 9 means body text
    if style_id:
        match = _HEADING_STYLE_RE.fullmatch(style_id)
        if match:
            return int(match.group(1))
        if style_id == "Title":
            return 1
    return None


class _RunDefaults:
    """What a run of one paragraph is before its own properties: from the paragraph's style chain, else
    the document defaults. `shaded` is True where white text is a header, not a trick."""

    __slots__ = ("shaded", "size", "vanish", "white")

    def __init__(self, vanish: bool, size: int | None, white: bool, shaded: bool) -> None:
        self.vanish = vanish
        self.size = size
        self.white = white
        self.shaded = shaded


def _resolved(styles: _Styles, style_id: str | None, table: dict[str, Any], default: Any) -> Any:
    value = styles.resolve(style_id, table)
    return default if value is None else value


def run_is_hidden(rpr: Any, styles: _Styles, defaults: _RunDefaults) -> bool:
    """
    True when a run is not visible to a reader: w:vanish or w:specVanish (on the run, else on its
    character style chain, else the paragraph's style chain, else the document defaults); a size of 1 pt
    or less; or white text (FFFFFF) where nothing behind it is shaded (the run's own shading or
    highlight, the paragraph's, the cell's, or any table style: white on a dark fill is a header).
    """
    vanish, size, white = _rpr_facts(rpr)
    shaded = defaults.shaded
    if rpr is not None:
        rstyle = rpr.find(W + "rStyle")
        rstyle_id = rstyle.get(W + "val") if rstyle is not None else None
        if rstyle_id:
            if vanish is None:
                vanish = styles.resolve(rstyle_id, styles.vanish)
            if size is None:
                size = styles.resolve(rstyle_id, styles.size)
            if white is None:
                white = styles.resolve(rstyle_id, styles.white)
        highlight = rpr.find(W + "highlight")
        shaded = shaded or _is_filled(rpr.find(W + "shd")) or (
            highlight is not None and (highlight.get(W + "val") or "none").lower() != "none"
        )
    vanish = defaults.vanish if vanish is None else vanish
    size = defaults.size if size is None else size
    white = defaults.white if white is None else white
    return bool(vanish) or (size is not None and size <= TINY_HALF_POINTS) or (bool(white) and not shaded)


def _paragraph_text(p: Any, styles: _Styles, shaded: bool = False) -> tuple[str, int]:
    """Visible text of one paragraph and the number of hidden characters in it. `shaded`: the paragraph
    sits in a shaded cell or table."""
    ppr = p.find(W + "pPr")
    pstyle = ppr.find(W + "pStyle") if ppr is not None else None
    para_style = (pstyle.get(W + "val") if pstyle is not None else None) or styles.default_paragraph
    defaults = _RunDefaults(
        vanish=bool(_resolved(styles, para_style, styles.vanish, styles.default_vanish)),
        size=_resolved(styles, para_style, styles.size, styles.default_size),
        white=bool(_resolved(styles, para_style, styles.white, styles.default_white)),
        shaded=shaded or (ppr is not None and _is_filled(ppr.find(W + "shd"))),
    )
    visible: list[str] = []
    hidden = 0
    stack = list(reversed(list(p)))
    while stack:
        el = stack.pop()
        tag = el.tag
        if tag == W + "r":
            run_hidden = run_is_hidden(el.find(W + "rPr"), styles, defaults)
            for part in el:
                ptag = part.tag
                if ptag == W + "t":
                    value = part.text or ""
                    if run_hidden:
                        hidden += len(value)
                    else:
                        visible.append(value)
                elif ptag == W + "delText":
                    hidden += len(part.text or "")  # tracked deletion: not in the final document
                elif not run_hidden and ptag in (W + "tab", W + "br", W + "cr"):
                    visible.append(" ")
                elif not run_hidden and ptag == W + "noBreakHyphen":
                    visible.append("-")
        elif tag in _DOCX_DESCEND:
            stack.extend(reversed(list(el)))
    return "".join(visible), hidden


def _docx_lang_hint(*roots: Any) -> bool:
    seen = 0
    for root in roots:
        for lang in root.iter(W + "lang"):
            for attr in ("val", "eastAsia", "bidi"):
                if _primary_subtag(lang.get(W + attr)) in LANG_HINTS:
                    return True
            seen += 1
            if seen >= _LANG_SCAN_LIMIT:
                return False
    return False


def _open_package(opener: Any, data: bytes) -> Any:
    try:
        return opener(io.BytesIO(data))
    except (MemoryError, RecursionError):
        raise
    except Exception:
        raise GateError("parse_failed") from None  # validation passed, the library still refused


_BLOCK_WRAPPERS = frozenset(W + tag for tag in ("sdt", "sdtContent", "customXml"))


def _table_is_shaded(tbl: Any) -> bool:
    """A table whose shading is not on its runs: a table style (which may shade rows) or its own w:shd."""
    props = tbl.find(W + "tblPr")
    return props is not None and (props.find(W + "tblStyle") is not None or _is_filled(props.find(W + "shd")))


def _cell_paragraphs(tc: Any, shaded: bool) -> Iterator[tuple[Any, bool]]:
    """
    The paragraphs of one table cell in document order, each with its `shaded` flag: the cell's DIRECT w:p
    children, the paragraphs of nested tables and of content controls. NOT tc.iter(): that also descends
    into text boxes (w:txbxContent inside a drawing) and reads both the mc:Choice and the mc:Fallback
    copy of each, which doubled their text (review of 5b-3, finding 9).
    """
    stack: list[tuple[Any, bool]] = [(child, shaded) for child in reversed(list(tc))]
    while stack:
        el, is_shaded = stack.pop()
        tag = el.tag
        if tag == W + "p":
            yield el, is_shaded
        elif tag == W + "tbl":
            table_shaded = is_shaded or _table_is_shaded(el)
            flat: list[tuple[Any, bool]] = []
            for tr in el.iterchildren(W + "tr"):
                for inner in tr.iterchildren(W + "tc"):
                    cell_shaded = table_shaded or _is_filled(inner.find(f"{W}tcPr/{W}shd"))
                    flat.extend((child, cell_shaded) for child in inner)
            stack.extend(reversed(flat))
        elif tag in _BLOCK_WRAPPERS:
            stack.extend((child, is_shaded) for child in reversed(list(el)))


def extract_docx(data: bytes, *, collector: Collector) -> bool:
    """Returns the Hindi/Marathi metadata hint."""
    import docx

    document = _open_package(docx.Document, data)
    styles = _Styles(document.styles.element)
    body = document.element.body
    collector.begin_page(1)
    stack = list(reversed(list(body)))
    while stack:
        el = stack.pop()
        tag = el.tag
        if tag == W + "p":
            text, hidden = _paragraph_text(el, styles)
            collector.hidden_text_chars += hidden
            level = _heading_level(el, styles)
            collector.add("heading" if level else "para", text, level)
        elif tag == W + "tbl":
            table_shaded = _table_is_shaded(el)
            for tr in el.iterchildren(W + "tr"):
                cells: list[str] = []
                for tc in tr.iterchildren(W + "tc"):
                    parts: list[str] = []
                    cell_shaded = table_shaded or _is_filled(tc.find(f"{W}tcPr/{W}shd"))
                    for cp, cp_shaded in _cell_paragraphs(tc, cell_shaded):
                        text, hidden = _paragraph_text(cp, styles, cp_shaded)
                        collector.hidden_text_chars += hidden
                        parts.append(text)
                    cell = clean_text(" ".join(parts))
                    if cell:
                        cells.append(cell)
                collector.add("table_row", " | ".join(cells))
        elif tag in (W + "sdt", W + "sdtContent", W + "customXml"):
            stack.extend(reversed(list(el)))
    collector.end_page()
    return _docx_lang_hint(document.styles.element, document.element)


# ── PPTX ─────────────────────────────────────────────────────────────────────


def _walk_shapes(shapes: Any, depth: int = 0) -> Iterator[Any]:
    from pptx.shapes.group import GroupShape

    for shape in shapes:
        if isinstance(shape, GroupShape):
            if depth >= MAX_SHAPE_DEPTH:
                # Text in a group nested deeper than the walker goes would be dropped without a trace. Real
                # slides nest a handful of groups; a deeper chain is a refused file (documented limit,
                # review of 5b-3): groups nested up to MAX_SHAPE_DEPTH levels are read.
                raise GateError("xml_too_complex")
            yield from _walk_shapes(shape.shapes, depth + 1)
        else:
            yield shape


def _position(shape: Any) -> tuple[int, int]:
    try:
        top, left = shape.top, shape.left
    except Exception:  # an odd placeholder whose position cannot be inherited
        return 0, 0
    return int(top or 0), int(left or 0)


def _table_rows(table: Any) -> Iterator[str]:
    for row in table.rows:
        cells = [clean_text(cell.text) for cell in row.cells]
        yield " | ".join(c for c in cells if c)


def extract_pptx(data: bytes, *, max_slides: int, collector: Collector) -> tuple[int, bool]:
    """Returns (slide count, Hindi/Marathi metadata hint)."""
    import pptx
    from pptx.oxml.ns import qn

    presentation = _open_package(pptx.Presentation, data)
    slides = presentation.slides
    count = len(slides)
    check_page_count(count, max_slides, "too_many_slides")  # re-checked here, whatever the upload scan counted
    lang_hint = False
    langs_seen = 0
    for index, slide in enumerate(slides, start=1):
        collector.begin_page(index)
        chars_before = collector.chars
        title = slide.shapes.title
        if title is not None and title.has_text_frame:
            collector.add("slide_title", title.text_frame.text)
        items: list[tuple[int, int, int, Any]] = []
        for order, shape in enumerate(_walk_shapes(slide.shapes)):
            if title is not None and shape._element is title._element:
                continue
            top, left = _position(shape)
            items.append((top // READING_BAND_EMU, left, order, shape))
        items.sort(key=lambda item: item[:3])
        for _band, _left, _order, shape in items:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    collector.add("para", paragraph.text)
            elif getattr(shape, "has_table", False) and shape.has_table:
                for row in _table_rows(shape.table):
                    collector.add("table_row", row)
        if slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            if frame is not None:
                for paragraph in frame.paragraphs:
                    collector.add("notes", paragraph.text)
        collector.end_page()
        if slide._element.get("show") in ("0", "false"):
            collector.hidden_text_chars += collector.chars - chars_before
        if not lang_hint and langs_seen < _LANG_SCAN_LIMIT:
            for rpr in slide._element.iter(qn("a:rPr"), qn("a:endParaRPr"), qn("a:defRPr")):
                langs_seen += 1
                if _primary_subtag(rpr.get("lang")) in LANG_HINTS:
                    lang_hint = True
                    break
                if langs_seen >= _LANG_SCAN_LIMIT:
                    break
    return count, lang_hint


# ── TXT and MD ───────────────────────────────────────────────────────────────


def extract_plain(data: bytes, kind: str, *, collector: Collector) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GateError("invalid_encoding") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    collector.begin_page(1)
    paragraph: list[str] = []
    pending = 0  # characters waiting in `paragraph`

    def flush() -> None:
        nonlocal pending
        if paragraph:
            collector.add("para", " ".join(paragraph))
            paragraph.clear()
        pending = 0

    in_fence = False
    for number, line in enumerate(text.split("\n")):
        if number % 4096 == 0:
            collector.check_time()  # a file of millions of short lines must meet the soft deadline too
        stripped = line.strip()
        if kind == "md":
            if _MD_FENCE_RE.match(line):
                flush()
                in_fence = not in_fence
                continue
            heading = None if in_fence else _md_heading(line)
            if heading:
                flush()
                collector.add("heading", heading[1], heading[0])
                continue
        if not stripped:
            flush()
            continue
        pending += len(stripped) + (1 if paragraph else 0)  # the joining space
        if pending > collector.max_text_chars:
            raise GateError("text_too_large")  # one paragraph over the whole cap: never join it
        paragraph.append(stripped)
    flush()
    collector.end_page()


# ── Driver ───────────────────────────────────────────────────────────────────


def extract_document(
    data: bytes,
    kind: str,
    *,
    max_pdf_pages: int,
    max_pptx_slides: int,
    max_text_chars: int,
    deadline: float | None,
) -> dict[str, Any]:
    """The ParsedDocument payload for `data`, or GateError. Gates run in this order: page and slide
    caps and text_too_large while extracting, then scanned_pdf, no_text_found, unreadable_text."""
    collector = Collector(max_text_chars=max_text_chars, deadline=deadline)
    per_page: list[int] | None = None
    lang_hint = False
    page_count = 1
    if kind == "pdf":
        page_count, per_page, lang_hint = extract_pdf(data, max_pages=max_pdf_pages, collector=collector)
    elif kind == "docx":
        lang_hint = extract_docx(data, collector=collector)
    elif kind == "pptx":
        page_count, lang_hint = extract_pptx(data, max_slides=max_pptx_slides, collector=collector)
    elif kind in ("txt", "md"):
        extract_plain(data, kind, collector=collector)
    else:
        raise GateError("unsupported_format")
    if per_page is not None:
        check_scanned_pdf(per_page)
    if collector.blocks == 0:
        raise GateError("no_text_found")
    counts = count_chars(" ".join(collector.texts))
    check_unreadable_text(counts, lang_hint=lang_hint)
    return {
        "file_kind": kind,
        "page_count": max(page_count, 1),
        "pages": collector.pages,
        "stats": {
            "chars": collector.chars,
            "blocks": collector.blocks,
            "hidden_text_chars": collector.hidden_text_chars,
            "script_guess": guess_script(counts),
        },
    }
