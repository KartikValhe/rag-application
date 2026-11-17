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
        (localized) style name; tables as table_row; hidden runs (w:vanish, also through a style) and
        tracked deletions are NOT extracted but their characters are counted; images, text boxes,
        headers, footers, footnotes and comments are not read.
  PPTX  python-pptx: slide = page; the title placeholder is slide_title; text frames in reading order
        (top to bottom in 0.25 inch bands, then left to right); groups walked; tables as table_row;
        speaker notes as notes; hidden slides are extracted AND their characters counted.
  TXT   paragraphs split on blank lines. MD: the same, plus ATX headings (# to ######).

Every text goes through document_text.clean_text and is split into blocks of at most MAX_BLOCK_CHARS
(at a sentence end, else a space). The quality gates (document_gates) run at the end.
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
_MD_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_MD_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)")


def split_text(text: str, limit: int = MAX_BLOCK_CHARS) -> list[str]:
    """Pieces of at most `limit` characters: cut after a sentence end, else at a space, else hard."""
    pieces: list[str] = []
    while len(text) > limit:
        low = limit // 2
        cut = max(text.rfind(end, low, limit + 1) for end in _SENTENCE_ENDS)
        if cut != -1:
            pieces.append(text[: cut + 1])
            text = text[cut + 2 :]
            continue
        space = text.rfind(" ", low, limit + 1)
        if space != -1:
            pieces.append(text[:space])
            text = text[space + 1 :]
            continue
        cut = limit
        while cut > low and unicodedata.category(text[cut]).startswith("M"):
            cut -= 1  # never start a piece with a combining mark
        pieces.append(text[:cut])
        text = text[cut:]
    pieces.append(text)
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


class _Styles:
    def __init__(self, styles_el: Any) -> None:
        self.based: dict[str, str] = {}
        self.outline: dict[str, int] = {}
        self.vanish: dict[str, bool] = {}
        self.default_paragraph: str | None = None
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
            if outline is not None and (outline.get(W + "val") or "").isdigit():
                self.outline[sid] = int(outline.get(W + "val"))
            vanish = _on(st.find(f"{W}rPr/{W}vanish"))
            if vanish is not None:
                self.vanish[sid] = vanish

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
    level: int | None = None
    if direct is not None and (direct.get(W + "val") or "").isdigit():
        level = int(direct.get(W + "val"))
    else:
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


def run_is_hidden(rpr: Any, styles: _Styles, para_hidden: bool) -> bool:
    """w:vanish on the run, else on its character style chain, else on the paragraph's style chain."""
    hidden = _on(rpr.find(W + "vanish")) if rpr is not None else None
    if hidden is None and rpr is not None:
        rstyle = rpr.find(W + "rStyle")
        if rstyle is not None:
            hidden = styles.resolve(rstyle.get(W + "val"), styles.vanish)
    return para_hidden if hidden is None else bool(hidden)


def _paragraph_text(p: Any, styles: _Styles) -> tuple[str, int]:
    """Visible text of one paragraph and the number of hidden characters in it."""
    ppr = p.find(W + "pPr")
    pstyle = ppr.find(W + "pStyle") if ppr is not None else None
    para_style = (pstyle.get(W + "val") if pstyle is not None else None) or styles.default_paragraph
    para_hidden = bool(styles.resolve(para_style, styles.vanish))
    visible: list[str] = []
    hidden = 0
    stack = list(reversed(list(p)))
    while stack:
        el = stack.pop()
        tag = el.tag
        if tag == W + "r":
            run_hidden = run_is_hidden(el.find(W + "rPr"), styles, para_hidden)
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
            for tr in el.iterchildren(W + "tr"):
                cells: list[str] = []
                for tc in tr.iterchildren(W + "tc"):
                    parts: list[str] = []
                    for cp in tc.iter(W + "p"):
                        text, hidden = _paragraph_text(cp, styles)
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
            if depth < MAX_SHAPE_DEPTH:
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

    def flush() -> None:
        if paragraph:
            collector.add("para", " ".join(paragraph))
            paragraph.clear()

    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if kind == "md":
            if _MD_FENCE_RE.match(line):
                flush()
                in_fence = not in_fence
                continue
            heading = None if in_fence else _MD_HEADING_RE.match(line)
            if heading:
                flush()
                collector.add("heading", heading.group(2) or "", len(heading.group(1)))
                continue
        if not stripped:
            flush()
            continue
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
