"""
app/services/document_gates.py
──────────────────────────────
Quality gates of the document parser (step 5b-3, design A3 step 4). They run in the sandboxed child
after extraction; each failure is a stable, permanent error code with a fixed message (docs/api/phase5.md
section 9). There is no OCR in v1.

scanned_pdf
    A page is "sparse" when it has fewer than SCANNED_MIN_CHARS_PER_PAGE (20) non-space characters
    after cleaning. The PDF fails when sparse pages are SCANNED_PAGE_SHARE (80 %) or more of ALL its
    pages (empty pages included).

unreadable_text (garbled legacy-font Devanagari, Kruti Dev / DevLys style)
    1. Private Use Area: more than 30 % of the letters (alphabetic characters plus U+E000-U+F8FF) are
       private-use code points. Fonts that draw Devanagari from private-use slots produce these.
    2. Latin-1 mojibake: more than 30 % of the non-space characters are "suspect" Latin-1 Supplement
       characters (U+00A1-U+00FF without the ones normal in English and maths: § © ® ° ± ² ³ µ ¶ · ¹
       « » ¿ ¡ × ÷), AND the text is Devanagari-targeted: the file's metadata names Hindi or Marathi
       (PDF /Lang, DOCX w:lang, PPTX a:rPr lang) or the text holds at least one Devanagari character.
       English text with accents stays around 5 % (tested).
    Known limit: legacy fonts whose codes are plain ASCII ("Hkkjr dk lafo/kku") look like Latin text
    and are NOT detected; that needs a word-list or n-gram check (backlog B50).

no_text_found, text_too_large, too_many_pages, too_many_slides are applied by the extractors.
"""

from collections.abc import Sequence

from app.services.document_text import CharCounts

SCANNED_MIN_CHARS_PER_PAGE = 20
SCANNED_PAGE_SHARE_PERCENT = 80
UNREADABLE_SHARE_PERCENT = 30

GATE_CODES = (
    "scanned_pdf",
    "unreadable_text",
    "no_text_found",
    "text_too_large",
    "too_many_pages",
    "too_many_slides",
)


class GateError(Exception):
    """A document that fails a quality gate. `code` is one of GATE_CODES (or another stable code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_scanned_pdf(non_space_chars_per_page: Sequence[int]) -> None:
    """One entry per page of the PDF (0 for a page without text)."""
    pages = len(non_space_chars_per_page)
    if pages == 0:
        return
    sparse = sum(1 for n in non_space_chars_per_page if n < SCANNED_MIN_CHARS_PER_PAGE)
    if sparse * 100 >= SCANNED_PAGE_SHARE_PERCENT * pages:
        raise GateError("scanned_pdf")


def check_unreadable_text(counts: CharCounts, *, lang_hint: bool) -> None:
    if counts.letters and counts.private_use * 100 > UNREADABLE_SHARE_PERCENT * counts.letters:
        raise GateError("unreadable_text")
    targeted = lang_hint or counts.deva > 0
    if targeted and counts.non_space and counts.latin1_suspect * 100 > UNREADABLE_SHARE_PERCENT * counts.non_space:
        raise GateError("unreadable_text")
