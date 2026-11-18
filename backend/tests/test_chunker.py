"""
The chunker (step 5c-2, design A3 step 5): pure functions from a ParsedDocument to chunks.

No database, no model: token counts come from the FakeEmbedder (one token per word) or from small stand-in
counters that behave like a sub-word tokenizer (several tokens per word, so a word can be longer than a chunk).
"""

import hashlib
import math
import random
import re
import time
import unicodedata
from collections.abc import Callable

import pytest

from app.ml.embedder import FakeEmbedder
from app.services import chunker
from app.services.chunker import ChunkingConfig, TextChunk, chunk_document
from tests.fixtures.chunk_docs import build_doc, flow_doc, random_text, sentence_text
from tests.security.pipeline_assertions import assert_chunks_within_model_limit

COUNT = FakeEmbedder().count_tokens
CFG = ChunkingConfig()  # target 380, max 460, overlap 60, model limit 512
JOINERS = "‌‍"


def subword(text: str) -> int:
    """About three characters per token, never zero: a stand-in for a real sub-word tokenizer."""
    return max(1, math.ceil(len(text) / 3)) if text else 0


def embedded_tokens(chunk: TextChunk, count: Callable[[str], int]) -> int:
    return count(chunk.embedding_text)


def check_bounds(chunks: list[TextChunk], count: Callable[[str], int], config: ChunkingConfig = CFG) -> None:
    for c in chunks:
        assert c.content.strip(), "empty chunk"
        assert c.token_count == count(c.content)
    assert_chunks_within_model_limit(chunks, count, config)


# ── Bounds and the margin ────────────────────────────────────────────────────


def test_every_chunk_obeys_the_token_bounds_and_fills_up_to_the_target() -> None:
    doc = flow_doc([("heading", "Unit 1", 1), ("para", sentence_text(5000)), ("heading", "Unit 2", 1), ("para", sentence_text(700))])
    chunks = chunk_document(doc, COUNT, CFG)
    check_bounds(chunks, COUNT)
    unit1 = [c for c in chunks if c.heading_path == "Unit 1"]
    assert len(unit1) >= 12
    # every chunk but the last of a section is filled to within one sentence of the target
    for c in unit1[:-1]:
        assert CFG.target_tokens - 12 <= c.token_count <= CFG.target_tokens


def test_the_512_token_margin_holds_even_with_a_300_character_devanagari_heading() -> None:
    long_heading = " ".join(["विद्यार्थी"] * 40)  # well over 300 characters
    assert len(long_heading) > 300
    doc = flow_doc([("heading", long_heading, 1), ("para", sentence_text(3000))])
    for count in (COUNT, subword):
        chunks = chunk_document(doc, count, CFG)
        assert chunks and all(len(c.heading_path) <= chunker.HEADING_PATH_MAX_CHARS for c in chunks)
        check_bounds(chunks, count)
        # the heading takes room from the content, never the other way round
        assert max(count(c.heading_path) for c in chunks) <= chunker.HEADING_PATH_MAX_TOKENS


def test_the_bound_is_measured_on_the_embedded_text_with_a_tokenizer_that_is_not_additive() -> None:
    """Joining pieces can cost more than the sum of the pieces; the chunker measures the real text."""

    def lumpy(text: str) -> int:
        words = text.split()
        return len(words) + sum(1 for w in words if len(w) > 6) + math.ceil(len(text) / 40)

    doc = flow_doc([("heading", "Mechanics > Forces", 1), ("para", " ".join(["momentum", "is", "conserved."] * 900))])
    chunks = chunk_document(doc, lumpy, CFG)
    assert len(chunks) > 3
    check_bounds(chunks, lumpy)


def test_a_word_longer_than_a_chunk_is_cut_but_never_inside_a_conjunct() -> None:
    conjunct = "क्‍ष"  # ka + virama + ZWJ + ssa
    word = (conjunct + "ा") * 3000
    doc = flow_doc([("para", f"start {word} end")])
    chunks = chunk_document(doc, subword, CFG)
    assert len(chunks) > 5
    check_bounds(chunks, subword)
    for c in chunks:
        assert not unicodedata.category(c.content[0]).startswith("M") and c.content[0] not in JOINERS
        assert c.content[-1] not in JOINERS and c.content[-1] != "्"
    # nothing was lost or glued: the pieces put back together (without the spaces) are exactly the text again
    assert "".join(c.content for c in chunks).replace(" ", "") == "start" + word + "end"


def test_when_the_heading_leaves_less_room_than_the_target_the_content_shrinks_to_fit() -> None:
    config = ChunkingConfig(target_tokens=380, max_tokens=420, overlap_tokens=60, model_max_tokens=430)
    doc = flow_doc([("heading", " ".join(["विद्यार्थी"] * 40), 1), ("para", sentence_text(3000))])
    chunks = chunk_document(doc, subword, config)
    check_bounds(chunks, subword, config)
    for c in chunks:
        room = config.model_max_tokens - chunker.PASSAGE_RESERVED_TOKENS - 1 - subword(c.heading_path)
        assert c.token_count <= room < config.target_tokens


# ── Overlap ──────────────────────────────────────────────────────────────────


def _shared_prefix_words(a: str, b: str) -> list[str]:
    """The longest run of words that ends chunk `a` and starts chunk `b`."""
    aw, bw = a.split(), b.split()
    for n in range(min(len(aw), len(bw)), 0, -1):
        if aw[-n:] == bw[:n]:
            return bw[:n]
    return []


def test_consecutive_chunks_of_a_section_overlap_by_whole_sentences_up_to_60_tokens() -> None:
    doc = flow_doc([("para", sentence_text(4000))])
    chunks = chunk_document(doc, COUNT, CFG)
    assert len(chunks) > 5
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        shared = _shared_prefix_words(prev.content, nxt.content)
        tokens = len(shared)
        assert 0 < tokens <= CFG.overlap_tokens
        assert tokens > CFG.overlap_tokens - 12  # the carry is as large as whole sentences allow
        assert shared[-1].endswith(".")  # it ends at a sentence boundary


def test_a_sentence_longer_than_the_overlap_is_overlapped_by_whole_words() -> None:
    doc = flow_doc([("para", " ".join(f"v{i}" for i in range(3000)))])  # one endless sentence
    chunks = chunk_document(doc, COUNT, CFG)
    assert len(chunks) > 5
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        shared = _shared_prefix_words(prev.content, nxt.content)
        assert len(shared) == CFG.overlap_tokens


def test_there_is_no_overlap_across_a_heading() -> None:
    doc = flow_doc([("heading", "A", 1), ("para", sentence_text(500)), ("heading", "B", 1), ("para", sentence_text(500, start=10_000))])
    chunks = chunk_document(doc, COUNT, CFG)
    last_a = [c for c in chunks if c.heading_path == "A"][-1]
    first_b = [c for c in chunks if c.heading_path == "B"][0]
    assert not _shared_prefix_words(last_a.content, first_b.content)
    assert not set(last_a.content.split()) & set(first_b.content.split())


# ── Empty parts, ordinals, determinism ───────────────────────────────────────


def test_headings_without_text_make_no_chunk_and_ordinals_are_dense() -> None:
    doc = flow_doc(
        [("heading", "Empty", 1), ("heading", "Also empty", 2), ("heading", "Real", 2), ("para", "One sentence only."), ("heading", "Tail", 1)]
    )
    chunks = chunk_document(doc, COUNT, CFG)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert [(c.heading_path, c.content) for c in chunks] == [("Empty > Real", "One sentence only.")]
    assert chunk_document(flow_doc([("heading", "Only a title", 1)]), COUNT, CFG) == []


def test_chunking_is_deterministic_and_ordinals_stable() -> None:
    doc = flow_doc([("heading", "H", 1), ("para", sentence_text(1500)), ("table_row", "a b c"), ("notes", "remember this.")])
    first = chunk_document(doc, COUNT, CFG)
    second = chunk_document(doc, COUNT, CFG)
    assert first == second
    assert [c.ordinal for c in first] == list(range(len(first)))
    assert [c.content_sha256 for c in first] == [hashlib.sha256(c.content.encode()).hexdigest() for c in first]


# ── Pages ────────────────────────────────────────────────────────────────────


def _word_page(index: int, per_page: int, first_page: int) -> int:
    return first_page + index // per_page


def test_page_ranges_follow_the_words_across_page_breaks_for_pdf() -> None:
    per_page = 250
    pages = [(p, [("para", sentence_text(per_page, start=(p - 3) * per_page))]) for p in range(3, 9)]  # pages 3..8
    doc = build_doc("pdf", pages, page_count=20)
    chunks = chunk_document(doc, COUNT, CFG)
    assert len(chunks) >= 4
    spans_pages = 0
    for c in chunks:
        numbers = [int(m) for m in re.findall(r"w(\d+)", c.content)]
        first, last = _word_page(numbers[0], per_page, 3), _word_page(numbers[-1], per_page, 3)
        assert (c.page_start, c.page_end) == (first, last)
        spans_pages += first != last
    assert spans_pages >= 2
    assert chunks[0].page_start == 3 and chunks[-1].page_end == 8


@pytest.mark.parametrize("kind", ["docx", "txt", "md"])
def test_flow_formats_have_no_page_numbers(kind: str) -> None:
    chunks = chunk_document(flow_doc([("para", sentence_text(900))], kind), COUNT, CFG)
    assert len(chunks) >= 2
    assert all(c.page_start is None and c.page_end is None for c in chunks)


# ── Headings, tables, notes ──────────────────────────────────────────────────


def test_headings_propagate_nest_and_reset_by_level() -> None:
    doc = flow_doc(
        [
            ("heading", "Physics", 1),
            ("para", "Intro text."),
            ("heading", "Motion", 2),
            ("para", "Speed is distance over time."),
            ("heading", "Newton", 3),
            ("para", "Force equals mass times acceleration."),
            ("heading", "Energy", 2),
            ("para", "Work is force times distance."),
            ("heading", "Chemistry", 1),
            ("para", "Atoms combine."),
        ],
        "md",
    )
    paths = [c.heading_path for c in chunk_document(doc, COUNT, CFG)]
    assert paths == ["Physics", "Physics > Motion", "Physics > Motion > Newton", "Physics > Energy", "Chemistry"]


def test_a_very_long_heading_path_is_capped_at_300_characters_on_a_word_boundary() -> None:
    parts = [("heading", "Chapter " + "x" * 60, level) for level in range(1, 8)]
    doc = flow_doc([*parts, ("para", "Body text.")])
    (chunk,) = chunk_document(doc, COUNT, CFG)
    assert 0 < len(chunk.heading_path) <= 300
    assert chunk.heading_path.startswith("Chapter xxxx")
    assert chunk.content == "Body text."


def test_table_rows_stay_whole_lines_and_notes_stay_with_their_slide() -> None:
    rows = [("table_row", f"Row {i} | Topic {i} | Hours {i}") for i in range(30)]
    doc = flow_doc([("heading", "Schedule", 1), *rows])
    (chunk,) = chunk_document(doc, COUNT, CFG)
    assert chunk.content.split("\n") == [r[1] for r in rows]

    slides = build_doc(
        "pptx",
        [
            (1, [("slide_title", "Waves"), ("para", "A wave carries energy."), ("notes", "Mention the rope demo.")]),
            (2, [("slide_title", "Light"), ("para", "Light is a wave."), ("table_row", "colour | wavelength")]),
        ],
    )
    chunks = chunk_document(slides, COUNT, CFG)
    assert [(c.heading_path, c.page_start, c.page_end) for c in chunks] == [("Waves", 1, 1), ("Light", 2, 2)]
    assert "Mention the rope demo." in chunks[0].content
    assert chunks[1].content.endswith("colour | wavelength")


def test_a_slide_without_a_title_does_not_inherit_the_previous_title() -> None:
    slides = build_doc("pptx", [(1, [("slide_title", "Waves"), ("para", "One.")]), (2, [("para", "Two.")])])
    assert [c.heading_path for c in chunk_document(slides, COUNT, CFG)] == ["Waves", ""]


# ── Hindi, Marathi, mixed ────────────────────────────────────────────────────


def test_the_devanagari_danda_is_a_sentence_boundary() -> None:
    sentence = "भारत की राजधानी नई दिल्ली है।"
    doc = flow_doc([("para", " ".join([sentence] * 300))])
    chunks = chunk_document(doc, COUNT, CFG)
    assert len(chunks) >= 3
    for c in chunks:
        assert c.script == "deva"
        assert c.content.endswith("है।")  # cut at a danda, never mid-sentence
        assert c.content.split()[0] == "भारत"  # and starts at a sentence start (the carry is whole sentences)
    check_bounds(chunks, COUNT)


def test_marathi_and_double_danda_and_mixed_script_are_classified() -> None:
    marathi = " ".join(["महाराष्ट्राची राजधानी मुंबई आहे॥"] * 250)
    mixed = " ".join(["Force म्हणजे बल."] * 250)
    english = " ".join(["The force is a push."] * 250)
    assert {c.script for c in chunk_document(flow_doc([("para", marathi)]), COUNT, CFG)} == {"deva"}
    assert {c.script for c in chunk_document(flow_doc([("para", mixed)]), COUNT, CFG)} == {"mixed"}
    assert {c.script for c in chunk_document(flow_doc([("para", english)]), COUNT, CFG)} == {"latn"}
    chunks = chunk_document(flow_doc([("para", marathi)]), COUNT, CFG)
    assert all(c.content.endswith("आहे॥") for c in chunks)


def test_the_embedded_text_is_the_heading_path_a_newline_and_the_content() -> None:
    (c,) = chunk_document(flow_doc([("heading", "Unit 1", 1), ("para", "Text.")]), COUNT, CFG)
    assert c.embedding_text == "Unit 1\nText."
    (bare,) = chunk_document(flow_doc([("para", "Text.")]), COUNT, CFG)
    assert bare.embedding_text == "Text."


# ── Pathological and random documents ────────────────────────────────────────


def test_one_1_9_million_character_paragraph_chunks_in_linear_time() -> None:
    sentence = "Alpha beta gamma delta epsilon. "
    pieces: list[str] = []
    total = 0
    while total < 1_900_000:
        block = (sentence * 312)[:9_990].rstrip()
        pieces.append(block)
        total += len(block)
    doc = flow_doc([("para", p) for p in pieces])
    started = time.perf_counter()
    chunks = chunk_document(doc, COUNT, CFG)
    elapsed = time.perf_counter() - started
    assert elapsed < 30
    assert len(chunks) > 500
    check_bounds(chunks, COUNT)
    # twice the input takes about twice the time, not four times (generous: factor 3.5)
    started = time.perf_counter()
    chunk_document(flow_doc([("para", p) for p in pieces[: len(pieces) // 2]]), COUNT, CFG)
    half = time.perf_counter() - started
    assert elapsed < max(half, 0.05) * 3.5


def test_one_huge_paragraph_without_any_sentence_end_is_packed_by_words_in_linear_time() -> None:
    pieces = [" ".join(f"t{i}" for i in range(j, j + 1500)) for j in range(0, 150 * 1500, 1500)]
    doc = flow_doc([("para", p) for p in pieces])
    started = time.perf_counter()
    chunks = chunk_document(doc, COUNT, CFG)
    assert time.perf_counter() - started < 30
    check_bounds(chunks, COUNT)


def test_ten_thousand_tiny_paragraphs_chunk_in_linear_time() -> None:
    doc = flow_doc([("para", f"Line {i}.") for i in range(10_000)])
    started = time.perf_counter()
    chunks = chunk_document(doc, COUNT, CFG)
    assert time.perf_counter() - started < 20
    assert len(chunks) >= 10_000 * 2 // CFG.target_tokens
    check_bounds(chunks, COUNT)


@pytest.mark.parametrize("seed", range(40))
def test_random_documents_always_obey_every_rule(seed: int) -> None:
    rng = random.Random(seed)
    blocks: list[tuple[str, str] | tuple[str, str, int]] = []
    for _ in range(rng.randint(1, 40)):
        roll = rng.random()
        if roll < 0.15:
            blocks.append(("heading", random_text(rng, 8), rng.randint(1, 4)))
        elif roll < 0.25:
            blocks.append(("table_row", random_text(rng, 10)))
        else:
            blocks.append(("para", random_text(rng, rng.choice([5, 80, 600]))))
    kind = rng.choice(["txt", "docx", "md"])
    doc = flow_doc(blocks, kind)
    count = rng.choice([COUNT, subword])
    chunks = chunk_document(doc, count, CFG)
    check_bounds(chunks, count)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert chunks == chunk_document(doc, count, CFG)
    source_words = {w for b in blocks if b[0] != "heading" for w in b[1].split()}
    chunk_words = {w for c in chunks for w in c.content.split()}
    assert chunk_words <= source_words  # no word was cut or invented
    assert source_words <= chunk_words  # and none was lost
    for c in chunks:
        assert c.content == c.content.strip() and "  " not in c.content
        assert c.script in ("latn", "deva", "mixed")


# ── Configuration ────────────────────────────────────────────────────────────


def test_defaults_are_380_460_and_60() -> None:
    assert (CFG.target_tokens, CFG.max_tokens, CFG.overlap_tokens, CFG.model_max_tokens) == (380, 460, 60, 512)
    assert chunker.PASSAGE_RESERVED_TOKENS >= 5  # the 'passage: ' prefix and the two special tokens


@pytest.mark.parametrize(
    "bad",
    [
        {"target_tokens": 0},
        {"overlap_tokens": 380},
        {"overlap_tokens": -1},
        {"target_tokens": 470, "max_tokens": 460},
        {"max_tokens": 600},  # does not leave room for the prefix and the heading inside 512
    ],
)
def test_a_configuration_that_cannot_keep_the_margin_is_refused(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(**bad)
