"""
app/services/parsed_document.py
───────────────────────────────
The contract between the sandboxed document child and the ingestion worker (step 5b-3, design A3
step 3). The child answers with this JSON; the PARENT validates it before it uses a single value:
the child is untrusted too, because a parser exploit makes it send anything it likes.

    ParsedDocument{file_kind, page_count, pages: [Page{n, blocks: [Block{t, level, text}]}],
                   stats{chars, blocks, hidden_text_chars, script_guess}}

Pages that hold no text are left out (`page_count` still counts them). DOCX, TXT and MD have no real
pages: they carry exactly one page with n = 1 and page_count = 1, and the chunker (5c) leaves
`page_start` empty for those kinds. For PPTX a slide is a page.

Strictness: every model forbids extra keys and refuses type coercion (a bool is not an int, "1" is
not 1). Hard caps (pages, blocks per page, blocks in total, characters per block, total characters)
are checked on the raw payload BEFORE any model is built, so an over-cap payload costs nothing.
Block text must already be clean (document_text.is_clean_text): no control, zero-width or bidi
character, NFC, single spaces.

Errors carry a stable code and a fixed message. Nothing of the payload is ever echoed, and the
underlying pydantic error (whose text quotes the input) is never chained.
"""

from typing import Annotated, Any, Literal, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from app.services.document_text import is_clean_text

FILE_KINDS = ("pdf", "docx", "pptx", "txt", "md")
PAGED_KINDS = ("pdf", "pptx")  # kinds with real page numbers; the others have the single page n = 1
BLOCK_TYPES = ("heading", "para", "table_row", "slide_title", "notes")

# Hard caps. The tenant's own limits (max_pdf_pages, max_pptx_slides) are tighter; these are the
# ceilings no caller can raise.
MAX_PAGES = 2000  # the platform ceiling of max_pdf_pages
MAX_BLOCKS_PER_PAGE = 50_000  # a DOCX or TXT is one page, so this is also its paragraph cap
MAX_BLOCKS_TOTAL = 100_000
MAX_BLOCK_CHARS = 10_000
DEFAULT_MAX_TEXT_CHARS = 2_000_000  # DOC_MAX_TEXT_CHARS
MAX_HEADING_LEVEL = 9
MAX_HIDDEN_TEXT_CHARS = 2**40  # a plausible upper bound for a counter, not a content limit

ERROR_MESSAGES: dict[str, str] = {
    "parse_output_invalid": "The document parser returned a result that could not be accepted.",
    "parse_output_too_large": "The document parser returned more content than is allowed.",
    "text_too_large": "The document contains more text than can be processed.",
}


class ParsedDocumentError(Exception):
    """A rejected child result. `code` is stable, `message` is fixed; the payload is never echoed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = ERROR_MESSAGES[code]


def _fail(code: str) -> NoReturn:
    raise ParsedDocumentError(code) from None


_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


class Block(BaseModel):
    model_config = _STRICT

    t: Literal["heading", "para", "table_row", "slide_title", "notes"]
    level: StrictInt | None = None
    text: StrictStr

    @model_validator(mode="after")
    def _check(self) -> "Block":
        if not is_clean_text(self.text):
            raise ValueError("text")
        if self.t == "heading":
            if self.level is None or not 1 <= self.level <= MAX_HEADING_LEVEL:
                raise ValueError("level")
        elif self.level is not None:
            raise ValueError("level")
        return self


class Page(BaseModel):
    model_config = _STRICT

    n: Annotated[StrictInt, Field(ge=1, le=MAX_PAGES)]
    blocks: Annotated[list[Block], Field(min_length=1)]


class Stats(BaseModel):
    model_config = _STRICT

    chars: Annotated[StrictInt, Field(ge=0)]
    blocks: Annotated[StrictInt, Field(ge=0)]
    hidden_text_chars: Annotated[StrictInt, Field(ge=0, le=MAX_HIDDEN_TEXT_CHARS)]
    script_guess: Literal["latn", "deva", "mixed"]


class ParsedDocument(BaseModel):
    model_config = _STRICT

    file_kind: Literal["pdf", "docx", "pptx", "txt", "md"]
    page_count: Annotated[StrictInt, Field(ge=1, le=MAX_PAGES)]
    pages: Annotated[list[Page], Field(min_length=1)]
    stats: Stats

    @model_validator(mode="after")
    def _check(self) -> "ParsedDocument":
        previous = 0
        for page in self.pages:
            if page.n <= previous or page.n > self.page_count:  # strictly increasing, inside the document
                raise ValueError("pages")
            previous = page.n
        if self.file_kind not in PAGED_KINDS and (self.page_count != 1 or self.pages[0].n != 1):
            raise ValueError("flow format")
        chars = sum(len(b.text) for p in self.pages for b in p.blocks)
        blocks = sum(len(p.blocks) for p in self.pages)
        if self.stats.chars != chars or self.stats.blocks != blocks:
            raise ValueError("stats")
        return self


def _check_caps(payload: dict[str, Any], max_pages: int, max_text_chars: int) -> None:
    """The caps, on the raw payload and before any model exists. Malformed parts are skipped here."""
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return
    if len(pages) > min(max_pages, MAX_PAGES):
        _fail("parse_output_too_large")
    total_blocks = 0
    total_chars = 0
    for page in pages:
        blocks = page.get("blocks") if isinstance(page, dict) else None
        if not isinstance(blocks, list):
            continue
        if len(blocks) > MAX_BLOCKS_PER_PAGE:
            _fail("parse_output_too_large")
        total_blocks += len(blocks)
        if total_blocks > MAX_BLOCKS_TOTAL:
            _fail("parse_output_too_large")
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str):
                if len(text) > MAX_BLOCK_CHARS:
                    _fail("parse_output_too_large")
                total_chars += len(text)
                if total_chars > max_text_chars:
                    _fail("text_too_large")


def validate_parsed_document(
    payload: object,
    *,
    max_pages: int = MAX_PAGES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    expected_kind: str | None = None,
) -> ParsedDocument:
    """
    The child's `document` object as a ParsedDocument, or a ParsedDocumentError:

      parse_output_too_large  more pages, blocks or characters per block than the caps allow
      text_too_large          more than `max_text_chars` characters in total
      parse_output_invalid    anything else that is not exactly the contract

    `max_pages` is the tenant's page (or slide) cap for the kind; the hard caps above always apply.
    `expected_kind` is the file kind the worker stored for the version.
    """
    if not isinstance(payload, dict):
        _fail("parse_output_invalid")
    _check_caps(payload, max_pages, max(0, min(max_text_chars, DEFAULT_MAX_TEXT_CHARS)))
    try:
        document = ParsedDocument.model_validate(payload)
    except ValidationError:
        _fail("parse_output_invalid")  # the pydantic error quotes the input: it is dropped here
    if expected_kind is not None and document.file_kind != expected_kind:
        _fail("parse_output_invalid")
    return document
