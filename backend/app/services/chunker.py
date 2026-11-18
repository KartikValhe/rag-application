"""
app/services/chunker.py
───────────────────────
The chunker (step 5c-2, design A3 step 5): pure functions from a ParsedDocument to chunks. No database, no
model, no I/O. Tokens are counted by the caller's `count_tokens` (the embedder's own tokenizer, or a stand-in).

How a document is cut
  1. Heading first. A heading block (or, for PPTX, a new slide) ends the section before it, so a chunk never
     spans two headings. The heading path of a chunk is the stack of headings above it joined with " > ",
     capped at HEADING_PATH_MAX_CHARS characters and HEADING_PATH_MAX_TOKENS tokens. A slide title is the
     path of its slide; a slide with no title has an empty path.
  2. Then packing by tokens. A section is turned into units (a sentence at a time; the sentence ends are
     . ? ! and the Devanagari danda and double danda) and the units are packed up to CHUNK_TARGET_TOKENS. A
     sentence longer than the target is packed by words; a single word longer than that is cut, and a cut
     NEVER lands inside a Devanagari combining sequence or next to a ZWJ / ZWNJ (the same rule the parser
     uses, document_text.is_unsafe_cut). Blocks (paragraphs, table rows, notes) are joined with a newline,
     sentences of one block with a space, so a table row stays one line.
  3. Overlap. The next chunk of a section starts with the last whole sentences of the previous one, as many
     as fit in CHUNK_OVERLAP_TOKENS (whole words when even the last sentence is longer). No overlap crosses
     a heading.

The 512-token margin (the e5-small limit counts the 'passage: ' prefix, two special tokens, the heading path
and the newline as well as the content):

    content tokens <= min(CHUNK_MAX_TOKENS, model_max_tokens - PASSAGE_RESERVED_TOKENS - 1 - heading tokens)

and the target shrinks with it. Packing uses per-unit counts, but every finished chunk is MEASURED once on its
real embedded text (a sub-word tokenizer does not add up exactly); a chunk that does not fit gives units back.
So the bound holds for the text that is embedded, not for an estimate of it. Linear time: every unit is
counted once, every chunk is measured once or twice, nothing is re-sliced.

Each chunk: ordinal (dense, from 0), content, heading_path ("" for none), page_start / page_end (None for
formats without pages: DOCX, TXT, MD), content_sha256 (of the content, UTF-8), token_count (of the content)
and script (latn / deva / mixed). The text that is embedded is `embedding_text`: heading path, a newline,
content (just the content when there is no heading).
"""

import hashlib
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from app.services.document_text import count_chars, guess_script, is_unsafe_cut
from app.services.parsed_document import PAGED_KINDS, ParsedDocument

CHUNK_TARGET_TOKENS = 380
CHUNK_MAX_TOKENS = 460
CHUNK_OVERLAP_TOKENS = 60
MODEL_MAX_TOKENS = 512  # multilingual-e5-small; counts the prefix and the two special tokens

# The 'passage: ' prefix is 2 tokens and the two special tokens are 2 more; 8 leaves a margin of 4.
PASSAGE_RESERVED_TOKENS = 8
HEADING_SEPARATOR = " > "
HEADING_PATH_MAX_CHARS = 300
HEADING_PATH_MAX_TOKENS = 100
_ELLIPSIS = "…"

# A space that follows a sentence end. The text is clean (single spaces), so this is all a boundary needs.
_SENTENCE_BREAK = re.compile(r"(?<=[.?!।॥]) ")

CountTokens = Callable[[str], int]


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int = CHUNK_TARGET_TOKENS
    max_tokens: int = CHUNK_MAX_TOKENS
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS
    model_max_tokens: int = MODEL_MAX_TOKENS

    def __post_init__(self) -> None:
        if self.target_tokens < 1 or self.overlap_tokens < 0:
            raise ValueError("target_tokens must be positive and overlap_tokens not negative")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        if self.max_tokens + PASSAGE_RESERVED_TOKENS + 1 > self.model_max_tokens:
            raise ValueError("max_tokens leaves no room for the prefix inside the model's token limit")
        if self.model_max_tokens - PASSAGE_RESERVED_TOKENS - 1 - HEADING_PATH_MAX_TOKENS < 2 * self.overlap_tokens + 1:
            raise ValueError("the model's token limit leaves too little room for content next to a heading")


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    content: str
    heading_path: str
    page_start: int | None
    page_end: int | None
    content_sha256: str
    token_count: int
    script: str

    @property
    def embedding_text(self) -> str:
        """What is embedded: the heading path, a newline, the content."""
        return f"{self.heading_path}\n{self.content}" if self.heading_path else self.content


@dataclass(slots=True)
class _Unit:
    text: str
    tokens: int
    page: int | None
    sep: str  # put before this unit when it is not the first of a chunk: "\n" new block, " " same block, "" cut word


def chunk_document(
    document: ParsedDocument, count_tokens: CountTokens, config: ChunkingConfig | None = None
) -> list[TextChunk]:
    """The chunks of a parsed document (see the module docstring). Pure and deterministic."""
    return _Chunker(count_tokens, config or ChunkingConfig()).run(document)


def cap_heading_path(parts: list[str], count_tokens: CountTokens) -> str:
    """The heading stack as one path: whole leading levels while they fit, then ' > …'; one huge level is cut
    on a word boundary and ends with '…'. Never longer than 300 characters or 100 tokens."""

    def ok(candidate: str) -> bool:
        return len(candidate) <= HEADING_PATH_MAX_CHARS and count_tokens(candidate) <= HEADING_PATH_MAX_TOKENS

    full = HEADING_SEPARATOR.join(parts)
    if ok(full):
        return full
    for n in range(len(parts) - 1, 0, -1):
        candidate = HEADING_SEPARATOR.join(parts[:n]) + HEADING_SEPARATOR + _ELLIPSIS
        if ok(candidate):
            return candidate
    text = parts[0]
    while text and not ok(text + _ELLIPSIS):
        k = max(len(text) * 3 // 4, 0)
        space = text.rfind(" ", 0, k)
        k = space if space > 0 else k
        while 0 < k < len(text) and is_unsafe_cut(text, k):
            k -= 1
        text = text[:k].rstrip(" ")
    return text + _ELLIPSIS


class _Chunker:
    def __init__(self, count_tokens: CountTokens, config: ChunkingConfig) -> None:
        self.count = count_tokens
        self.cfg = config
        self.chunks: list[TextChunk] = []
        self._stack: list[tuple[int, str]] = []  # (level, text) of the headings above the current section
        self._units: list[_Unit] = []

    # ── Walking the document ────────────────────────────────────────────────

    def run(self, document: ParsedDocument) -> list[TextChunk]:
        paged = document.file_kind in PAGED_KINDS
        for page in document.pages:
            if document.file_kind == "pptx":
                self._end_section()
                self._stack = []  # a slide never inherits the previous slide's title
            number = page.n if paged else None
            for block in page.blocks:
                if block.t == "heading":
                    self._end_section()
                    level = block.level or 1
                    while self._stack and self._stack[-1][0] >= level:
                        self._stack.pop()
                    self._stack.append((level, block.text))
                elif block.t == "slide_title":
                    self._end_section()
                    self._stack = [(1, block.text)]
                else:
                    self._add_block(block.text, number)
        self._end_section()
        return self.chunks

    def _add_block(self, text: str, page: int | None) -> None:
        for index, sentence in enumerate(_SENTENCE_BREAK.split(text)):
            if sentence:
                self._units.append(_Unit(sentence, self.count(sentence), page, "\n" if index == 0 else " "))

    def _end_section(self) -> None:
        if self._units:
            heading = cap_heading_path([text for _, text in self._stack], self.count) if self._stack else ""
            self._pack(self._units, heading)
            self._units = []

    # ── Packing one section ─────────────────────────────────────────────────

    def _pack(self, units: list[_Unit], heading: str) -> None:
        cfg = self.cfg
        head_tokens = (self.count(heading) + 1) if heading else 0  # +1: the newline
        limit = min(cfg.max_tokens, cfg.model_max_tokens - PASSAGE_RESERVED_TOKENS - head_tokens)
        target = min(cfg.target_tokens, limit)
        overlap = min(cfg.overlap_tokens, target // 2)
        queue = deque(units)
        carry: list[_Unit] = []
        while queue:
            cur = list(carry)
            n_carry = len(cur)
            total = sum(u.tokens for u in cur)
            new = 0
            while queue:
                front = queue[0]
                if front.tokens > target:
                    queue.popleft()
                    queue.extendleft(reversed(self._explode(front, target)))
                    continue
                if new == 0 and total + front.tokens > target:
                    cur, n_carry, total = [], 0, 0  # no room for the overlap next to this unit: no overlap
                if new > 0 and total + front.tokens > target:
                    break
                cur.append(queue.popleft())
                total += front.tokens
                new += 1
            measured = self._settle(cur, n_carry, queue, heading, target)
            if measured is None:
                carry = []  # a unit was split and went back to the queue: pack again
                continue
            content, tokens = measured
            self._emit(content, tokens, cur, heading)
            carry = self._carry(cur, overlap) if queue else []

    def _settle(
        self, cur: list[_Unit], n_carry: int, queue: "deque[_Unit]", heading: str, target: int
    ) -> tuple[str, int] | None:
        """Measure the real text of `cur`; give units back to the queue until it fits. None: `cur` was emptied
        because its one unit had to be split (the pieces are back in the queue)."""
        budget = self.cfg.model_max_tokens - PASSAGE_RESERVED_TOKENS
        while True:
            content = _join(cur)
            tokens = self.count(content)
            embedded = self.count(f"{heading}\n{content}") if heading else tokens
            excess = max(tokens - self.cfg.max_tokens, embedded - budget)
            if excess <= 0:
                return content, tokens
            if len(cur) - n_carry > 1:
                dropped = 0
                while len(cur) - n_carry > 1 and dropped < excess:
                    unit = cur.pop()
                    queue.appendleft(unit)
                    dropped += max(unit.tokens, 1)
            elif n_carry:
                del cur[:n_carry]
                n_carry = 0
            else:
                unit = cur.pop()
                cur.clear()
                queue.extendleft(reversed(self._split_for(unit, target)))
                return None

    def _emit(self, content: str, tokens: int, cur: list[_Unit], heading: str) -> None:
        pages = [u.page for u in cur if u.page is not None]
        self.chunks.append(
            TextChunk(
                ordinal=len(self.chunks),
                content=content,
                heading_path=heading,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                token_count=tokens,
                script=guess_script(count_chars(content)),
            )
        )

    def _carry(self, cur: list[_Unit], overlap: int) -> list[_Unit]:
        """The overlap for the next chunk: the last whole units that fit in `overlap` tokens, else the last
        whole words of the last unit. Never the whole chunk, never starting inside a cut word."""
        if overlap <= 0 or len(cur) < 1:
            return []
        out: list[_Unit] = []
        acc = 0
        for unit in reversed(cur):
            if acc + unit.tokens > overlap:
                break
            out.append(unit)
            acc += unit.tokens
        out.reverse()
        if len(out) >= len(cur):
            out = out[1:]
        while out and out[0].sep == "":
            out.pop(0)  # would start in the middle of a cut word
        if out:
            return out
        last = cur[-1]
        words = last.text.split(" ")
        if len(words) < 2:
            return []
        tail: list[str] = []
        acc = 0
        for word in reversed(words):
            tokens = self.count(word)
            if acc + tokens > overlap:
                break
            tail.append(word)
            acc += tokens
        if not tail or len(tail) == len(words):
            return []
        tail.reverse()
        return [_Unit(" ".join(tail), acc, last.page, " ")]

    # ── Splitting what does not fit ─────────────────────────────────────────

    def _explode(self, unit: _Unit, target: int) -> list[_Unit]:
        """A unit over the target as word units (a word over the target is cut into pieces)."""
        out: list[_Unit] = []
        for index, word in enumerate(w for w in unit.text.split(" ") if w):
            sep = unit.sep if index == 0 else " "
            tokens = self.count(word)
            if tokens <= target:
                out.append(_Unit(word, tokens, unit.page, sep))
            else:
                out.extend(self._cut_word(word, target, unit.page, sep))
        return out

    def _split_for(self, unit: _Unit, target: int) -> list[_Unit]:
        """A unit that did not fit even alone (a heading or an uneven tokenizer took its room): its words, or
        for one word, halves of it."""
        if " " in unit.text:
            return self._explode(unit, max(1, min(target, unit.tokens) - 1))
        return self._cut_word(unit.text, max(1, min(target, unit.tokens) // 2), unit.page, unit.sep)

    def _cut_word(self, word: str, budget: int, page: int | None, sep: str) -> list[_Unit]:
        """Pieces of one word of at most `budget` tokens each, cut where the parser's rule allows."""
        n = len(word)
        per_char = max(self.count(word) / n, 1e-9)
        pieces: list[_Unit] = []
        start = 0
        while start < n:
            end = min(n, start + max(1, int(budget * 0.9 / per_char)))
            end = _safe_end(word, start, end)
            while end - start > 1 and self.count(word[start:end]) > budget:
                end = _safe_end(word, start, start + max(1, (end - start) * 3 // 4))
            piece = word[start:end]
            pieces.append(_Unit(piece, self.count(piece), page, sep if start == 0 else ""))
            start = end
        return pieces


def _safe_end(word: str, start: int, end: int) -> int:
    """`end` (or the nearest cut before it) where cutting `word` does not split a conjunct; the whole rest
    when the stretch has no safe cut at all."""
    n = len(word)
    if end >= n:
        return n
    cut = end
    while cut > start + 1 and is_unsafe_cut(word, cut):
        cut -= 1
    if not is_unsafe_cut(word, cut):
        return cut
    cut = end
    while cut < n and is_unsafe_cut(word, cut):
        cut += 1
    return cut


def _join(units: list[_Unit]) -> str:
    parts = [units[0].text]
    for unit in units[1:]:
        parts.append(unit.sep)
        parts.append(unit.text)
    return "".join(parts)
