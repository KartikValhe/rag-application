"""
tests/fixtures/chunk_docs.py
────────────────────────────
Builders of validated `ParsedDocument` objects for the chunker, embedding and index tests (step 5c-2).

A block is a tuple `(type, text)` or `(type, text, level)`; a page is `(n, [blocks])`. Texts must already
be clean (single spaces, NFC, no control characters): the real model validates them.
"""

import random
from collections.abc import Sequence

from app.services.document_text import count_chars, guess_script
from app.services.parsed_document import ParsedDocument

Block = tuple[str, str] | tuple[str, str, int]
Page = tuple[int, Sequence[Block]]


def build_doc(kind: str, pages: Sequence[Page], *, page_count: int | None = None) -> ParsedDocument:
    """A validated ParsedDocument (the same strict model the worker uses on the child's answer)."""
    raw_pages = []
    chars = 0
    blocks = 0
    all_text: list[str] = []
    for n, page_blocks in pages:
        raw_blocks = []
        for block in page_blocks:
            kind_, text = block[0], block[1]
            level = block[2] if len(block) == 3 else None  # type: ignore[misc]
            raw_blocks.append({"t": kind_, "level": level, "text": text})
            chars += len(text)
            blocks += 1
            all_text.append(text)
        raw_pages.append({"n": n, "blocks": raw_blocks})
    last = pages[-1][0] if pages else 1
    payload = {
        "file_kind": kind,
        "page_count": page_count if page_count is not None else (last if kind in ("pdf", "pptx") else 1),
        "pages": raw_pages,
        "stats": {
            "chars": chars,
            "blocks": blocks,
            "hidden_text_chars": 0,
            "script_guess": guess_script(count_chars(" ".join(all_text))),
        },
    }
    return ParsedDocument.model_validate(payload)


def flow_doc(blocks: Sequence[Block], kind: str = "txt") -> ParsedDocument:
    """A DOCX, TXT or MD document: one page, n = 1."""
    return build_doc(kind, [(1, blocks)])


def sentence_text(words: int, *, start: int = 0, per_sentence: int = 12) -> str:
    """`words` English words in sentences of `per_sentence` words (deterministic, clean)."""
    out: list[str] = []
    for i in range(words):
        word = f"w{start + i}"
        if (i + 1) % per_sentence == 0 or i == words - 1:
            word += "."
        out.append(word)
    return " ".join(out)


# ── Random text for the property-style test ──────────────────────────────────

_LATIN = ["alpha", "beta", "gamma", "Delta", "x2", "motion", "force", "e.g", "3.14", "Newton's", "co-op"]
_DEVA = [
    "भारत", "विज्ञान", "क्षत्रिय", "द्रव्यमान", "पानी", "क्‍ष", "कर्म", "ज्ञान", "शिक्षक", "विद्यार्थी", "महाराष्ट्राची",
]
_ENDS = [".", "?", "!", "।", "॥", ""]


def random_text(rng: random.Random, max_words: int) -> str:
    """A clean random text of Latin, Devanagari and mixed words with sentence ends (danda included)."""
    words: list[str] = []
    for _ in range(rng.randint(1, max_words)):
        word = rng.choice(_LATIN if rng.random() < 0.5 else _DEVA)
        if rng.random() < 0.15:
            word += rng.choice(_ENDS)
        words.append(word)
    return " ".join(words)
