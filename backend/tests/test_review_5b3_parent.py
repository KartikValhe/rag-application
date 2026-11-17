"""
Review of step 5b-3, parent side: the child's raw answer (finding 5), its encoding and size (finding 10),
and the worker's handling of an unexpected error in the child (finding 1). Written before the fixes.

The sandbox is the real one; hostile children are tests/_parse_children.py modes. Memory is measured with
tracemalloc in the PARENT process only (the child's own memory is its limit's business).
"""

import tracemalloc
from typing import Any

import pytest

from app.core import sandbox
from app.services.document_limits import default_limits
from app.services.parsed_document import validate_parsed_document
from app.workers import errors, stages
from tests._parse_children import raw_answer
from tests.test_parse_stage import PARSE, no_leftovers, store_root  # noqa: F401  (autouse fixtures)

HOSTILE_RAW = [
    ("duplicate_key", "parse_output_invalid"),
    ("nan", "parse_output_invalid"),
    ("infinity", "parse_output_invalid"),
    ("float", "parse_output_invalid"),
    ("deep_100", "parse_output_invalid"),
    ("deep_10000", "parse_output_invalid"),
    ("many_objects", "parse_output_too_large"),
    ("many_numbers", "parse_output_too_large"),
    ("huge_int_5000", "parse_output_invalid"),
    ("big_int_100", "parse_output_invalid"),
    ("lone_surrogate", "parse_output_invalid"),
    ("nul", "parse_output_invalid"),
    ("surrogate_key", "parse_output_invalid"),
    ("bom", "parse_output_invalid"),
    ("invalid_utf8", "parse_output_invalid"),
    ("utf16", "parse_output_invalid"),
    ("trailing_garbage", "parse_output_invalid"),
    ("not_an_object", "parse_output_invalid"),
]


@pytest.mark.parametrize(("variant", "code"), HOSTILE_RAW)
async def test_hostile_raw_child_output_is_a_permanent_error_with_a_stable_code(variant: str, code: str) -> None:
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.parse_in_sandbox(
            b"Unit 1", file_kind="txt", limits=default_limits(), parse=PARSE,
            child_module="tests._parse_children", child_args=(f"raw-{variant}",),
        )
    assert exc.value.code == code


@pytest.mark.parametrize("variant", ["many_objects", "many_numbers", "deep_10000", "huge_int_5000", "deep_100", "duplicate_key"])
def test_the_parent_parser_rejects_hostile_json_with_bounded_memory(variant: str) -> None:
    raw = raw_answer(variant)  # built before the measurement starts
    tracemalloc.start()
    try:
        with pytest.raises(sandbox.SandboxError) as exc:
            sandbox.parse_child_json_strict(raw, limits=stages.JSON_LIMITS)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert exc.value.code in ("parse_output_invalid", "parse_output_too_large")
    # A 12 MB hostile answer may cost a copy or two of itself, never the hundreds of MB its objects would
    assert peak < 4 * len(raw) + 16 * 1024 * 1024, f"peak {peak / 1e6:.0f} MB for {len(raw) / 1e6:.0f} MB of input"


def test_a_good_answer_passes_the_strict_parser() -> None:
    answer = sandbox.parse_child_json_strict(raw_answer("bom")[3:], limits=stages.JSON_LIMITS)
    assert answer["success"] is True and answer["peak_memory_kb"] == 1000


def test_an_empty_answer_is_still_a_crash_not_an_invalid_answer() -> None:
    with pytest.raises(sandbox.SandboxCrash):
        sandbox.parse_child_json_strict(b"", limits=stages.JSON_LIMITS)


def test_the_timetable_parser_keeps_its_old_behaviour_but_no_longer_dies_on_recursion() -> None:
    assert sandbox.parse_child_json(b'{"a": 1}') == {"a": 1}
    with pytest.raises(sandbox.SandboxCrash):
        sandbox.parse_child_json(b"[" * 10_000 + b"]" * 10_000)
    with pytest.raises(sandbox.SandboxCrash):
        sandbox.parse_child_json(b"x")


# ── The largest legitimate answer passes every limit; the default cap covers it ──────────────────────


def _worst_case_answer() -> dict[str, Any]:
    """2,000,000 characters of 4-byte text in 100,000 blocks of the longest block type, on two pages."""
    text = "𝐀" * 20
    block = {"t": "slide_title", "level": None, "text": text}
    pages = [{"n": n, "blocks": [dict(block) for _ in range(50_000)]} for n in (1, 2)]
    doc = {
        "file_kind": "pptx", "page_count": 2, "pages": pages,
        "stats": {"chars": 2_000_000, "blocks": 100_000, "hidden_text_chars": 0, "script_guess": "latn"},
    }
    return {"success": True, "document": doc, "peak_memory_kb": 123456}


def test_the_largest_legitimate_answer_fits_the_default_cap_and_every_parent_limit() -> None:
    import json

    from app.core.config import Settings
    from app.services.parsed_document import MAX_ANSWER_BYTES

    raw = json.dumps(_worst_case_answer(), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) <= MAX_ANSWER_BYTES
    assert Settings.model_fields["DOC_PARSE_MAX_OUTPUT_BYTES"].default >= MAX_ANSWER_BYTES  # the setting covers it
    assert MAX_ANSWER_BYTES < 16 * 1024 * 1024  # and it is far below the old 20 MB
    parsed = sandbox.parse_child_json_strict(raw, limits=stages.JSON_LIMITS)
    validate_parsed_document(parsed["document"], max_pages=100)


# ── 10. UTF-8 on the wire, and big non-ASCII documents round-trip within the cap ─────────────────────


async def test_the_child_writes_utf8_not_unicode_escapes() -> None:
    text = "नमस्ते 𝐀"
    raw = await sandbox.run_child(
        "tests._sandbox_children", b"x", args=("echo",), params={"text": text},
        limits=sandbox.SandboxLimits(timeout_seconds=30, max_output_bytes=100_000),
    )
    assert text.encode() in raw
    assert b"\\u" not in raw


def _big_text(alphabet_words: list[str], chars: int) -> str:
    import random

    rng = random.Random(11)
    out: list[str] = []
    size = 0
    while size < chars:
        word = rng.choice(alphabet_words)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)[:chars].strip()


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("devanagari", ["भारत", "संविधान", "लोकतंत्र", "नागरिकों", "मौलिक", "अधिकार", "विद्यार्थ्यांसाठी", "अभ्यासक्रम"]),
        ("non-bmp", ["𝐀𝐁𝐂", "𝒳𝒴", "𝔸𝔹ℂ", "𝕩𝕪𝕫𝕨", "𝟘𝟙𝟚"]),
    ],
)
async def test_a_1_9_million_character_non_ascii_document_round_trips_within_the_cap(name: str, words: list[str]) -> None:
    text = _big_text(words, 1_900_000)
    assert len(text) >= 1_899_000
    # 9 MiB: above the real UTF-8 size (3 or 4 bytes a character) and well below the 6 or 12 bytes of \uXXXX escapes
    settings = stages.ParseSettings(timeout_seconds=120.0, max_output_bytes=9 * 1024 * 1024, memory_limit_mb=1024, cpu_seconds=120)
    outcome = await stages.run_parse_child(text.encode(), file_kind="txt", limits=default_limits(), parse=settings)
    parts = [b.text for p in outcome.document.pages for b in p.blocks]
    assert " ".join(parts) == text, name
    # stats.chars counts the characters of the blocks, not the single spaces between them
    assert outcome.document.stats.chars == len(text) - (len(parts) - 1)


# ── 1. Worker side: a hostile file ends in ONE attempt, never a retry loop ───────────────────────────


async def test_a_lone_surrogate_in_the_child_answer_cannot_crash_the_child_into_a_retry_loop() -> None:
    """The bootstrap writes UTF-8; a lone surrogate cannot be encoded as such. The answer must still be
    written (escaped) so the PARENT rejects it permanently, instead of the child dying with exit 1."""
    from app.core import sandbox_child

    class Out:
        def __init__(self) -> None:
            self.data = b""
            self.buffer = self

        def write(self, b: bytes) -> None:
            self.data += b

        def flush(self) -> None:
            pass

    out = Out()
    sandbox_child.write_answer({"success": True, "junk": "\ud800"}, out)  # type: ignore[arg-type]
    assert b"ud800" in out.data


def test_the_attempt_budget_of_a_crash_is_documented_in_the_settings() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["INGEST_MAX_CRASH_ATTEMPTS"].default == 2
    assert Settings.model_fields["INGEST_MAX_ATTEMPTS"].default == 3

