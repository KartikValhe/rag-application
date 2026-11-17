"""
The parsed-document contract (app/services/parsed_document.py, step 5b-3, design A3 step 3).

The sandbox child is UNTRUSTED too: a compromised child can send anything. The parent therefore
validates the child's JSON against the strict model and the hard caps before it uses a single value.
Every rejection has a stable error code and a fixed message that never echoes the payload.
"""

import copy
import json
from typing import Any

import pytest

from app.services import parsed_document as pd
from app.services.parsed_document import ParsedDocumentError, validate_parsed_document

SECRET = "SECRET-MARKER-9f3a"


def good(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "file_kind": "pdf",
        "page_count": 3,
        "pages": [
            {"n": 1, "blocks": [{"t": "para", "level": None, "text": "Linear equations in one variable."}]},
            {
                "n": 3,
                "blocks": [
                    {"t": "heading", "level": 2, "text": "Unit 2"},
                    {"t": "para", "level": None, "text": "लघुगणक और घातांक। दूसरा वाक्य॥"},
                ],
            },
        ],
        "stats": {"chars": 0, "blocks": 3, "hidden_text_chars": 0, "script_guess": "mixed"},
    }
    doc["stats"]["chars"] = sum(len(b["text"]) for p in doc["pages"] for b in p["blocks"])
    doc.update(overrides)
    return doc


def code_of(payload: Any, **kwargs: Any) -> str:
    with pytest.raises(ParsedDocumentError) as exc:
        validate_parsed_document(payload, **kwargs)
    return exc.value.code


# ── The happy path ───────────────────────────────────────────────────────────


def test_a_good_payload_is_accepted_and_keeps_devanagari_danda() -> None:
    doc = validate_parsed_document(good())
    assert doc.file_kind == "pdf"
    assert doc.page_count == 3
    assert [p.n for p in doc.pages] == [1, 3]  # pages without text are simply absent
    assert doc.pages[1].blocks[1].text.endswith("दूसरा वाक्य॥")
    assert doc.stats.script_guess == "mixed"


def test_every_block_type_is_accepted() -> None:
    blocks = [
        {"t": "slide_title", "level": None, "text": "Course outline"},
        {"t": "heading", "level": 1, "text": "Unit 1"},
        {"t": "para", "level": None, "text": "text"},
        {"t": "table_row", "level": None, "text": "a | b | c"},
        {"t": "notes", "level": None, "text": "speaker notes"},
    ]
    payload = good(file_kind="pptx", page_count=1, pages=[{"n": 1, "blocks": blocks}])
    payload["stats"].update(chars=sum(len(b["text"]) for b in blocks), blocks=5)
    assert len(validate_parsed_document(payload).pages[0].blocks) == 5


def test_a_json_round_trip_is_accepted() -> None:
    assert validate_parsed_document(json.loads(json.dumps(good()))).page_count == 3


# ── Malformed payloads: stable code parse_output_invalid ─────────────────────


def _with(path: list[Any], value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(good())
    target: Any = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return doc


WRONG_TYPES: list[tuple[str, dict[str, Any]]] = [
    ("page n is a string", _with(["pages", 0, "n"], "1")),
    ("page n is a bool", _with(["pages", 0, "n"], True)),
    ("page n is a float", _with(["pages", 0, "n"], 1.0)),
    ("text is an int", _with(["pages", 0, "blocks", 0, "text"], 5)),
    ("text is None", _with(["pages", 0, "blocks", 0, "text"], None)),
    ("level is a string", _with(["pages", 1, "blocks", 0, "level"], "2")),
    ("level is a bool", _with(["pages", 1, "blocks", 0, "level"], True)),
    ("block type is unknown", _with(["pages", 0, "blocks", 0, "t"], "script")),
    ("page_count is a string", _with(["page_count"], "3")),
    ("stats.chars is a float", _with(["stats", "chars"], 3.5)),
    ("stats.script_guess is unknown", _with(["stats", "script_guess"], "arab")),
    ("file_kind is unknown", _with(["file_kind"], "exe")),
    ("pages is a dict", _with(["pages"], {"n": 1})),
    ("blocks is a string", _with(["pages", 0, "blocks"], "text")),
    ("a page is a list", _with(["pages", 0], [])),
    ("stats is a list", _with(["stats"], [])),
]


@pytest.mark.parametrize("payload", [p for _, p in WRONG_TYPES], ids=[n for n, _ in WRONG_TYPES])
def test_wrong_types_are_rejected(payload: dict[str, Any]) -> None:
    assert code_of(payload) == "parse_output_invalid"


@pytest.mark.parametrize("payload", [None, [], "text", 5, b"{}", [good()]])
def test_a_payload_that_is_not_an_object_is_rejected(payload: Any) -> None:
    assert code_of(payload) == "parse_output_invalid"


EXTRA_KEYS = [
    ("top level", _with(["debug"], "x")),
    ("page", _with(["pages", 0, "title"], "x")),
    ("block", _with(["pages", 0, "blocks", 0, "html"], "<b>")),
    ("stats", _with(["stats", "lang"], "hi")),
]


@pytest.mark.parametrize("payload", [p for _, p in EXTRA_KEYS], ids=[n for n, _ in EXTRA_KEYS])
def test_extra_keys_are_rejected(payload: dict[str, Any]) -> None:
    assert code_of(payload) == "parse_output_invalid"


MISSING = [
    ("file_kind", lambda d: d.pop("file_kind")),
    ("pages", lambda d: d.pop("pages")),
    ("stats", lambda d: d.pop("stats")),
    ("page n", lambda d: d["pages"][0].pop("n")),
    ("block text", lambda d: d["pages"][0]["blocks"][0].pop("text")),
    ("stats.hidden_text_chars", lambda d: d["stats"].pop("hidden_text_chars")),
]


@pytest.mark.parametrize(("name", "mutate"), MISSING, ids=[n for n, _ in MISSING])
def test_missing_keys_are_rejected(name: str, mutate: Any) -> None:
    doc = good()
    mutate(doc)
    assert code_of(doc) == "parse_output_invalid"


@pytest.mark.parametrize("n", [0, -1, -5])
def test_non_positive_page_numbers_are_rejected(n: int) -> None:
    assert code_of(_with(["pages", 0, "n"], n)) == "parse_output_invalid"


def test_pages_out_of_order_are_rejected() -> None:
    doc = good()
    doc["pages"].reverse()
    assert code_of(doc) == "parse_output_invalid"


def test_a_repeated_page_number_is_rejected() -> None:
    doc = good()
    doc["pages"][1]["n"] = 1
    assert code_of(doc) == "parse_output_invalid"


def test_a_page_number_beyond_the_page_count_is_rejected() -> None:
    assert code_of(_with(["page_count"], 2)) == "parse_output_invalid"  # page 3 does not exist


def test_an_empty_document_is_rejected() -> None:
    """The child reports no_text_found itself; a success without any block is a broken child."""
    doc = good(pages=[])
    doc["stats"].update(chars=0, blocks=0)
    assert code_of(doc) == "parse_output_invalid"


def test_a_page_without_blocks_is_rejected() -> None:
    """Pages without text must be left out, not sent empty."""
    doc = good()
    doc["pages"].append({"n": 3, "blocks": []})
    doc["pages"][1]["n"] = 2
    assert code_of(doc) == "parse_output_invalid"


@pytest.mark.parametrize(
    "bad_text",
    [
        "a\x00b",  # NUL
        "a\x1bb",  # ESC
        "a\x7fb",  # DEL
        "line one\nline two",  # the child collapses whitespace: no newline survives
        "tab\there",
        "a\x85b",  # C1 control (NEL)
        "a‮b",  # bidi override
        "a⁦b",  # bidi isolate
        "a​b",  # zero width space
        "a﻿b",  # BOM
        "a\ud800b",  # lone surrogate
        "é",  # not NFC (decomposed)
    ],
)
def test_control_zero_width_bidi_and_non_nfc_text_is_rejected(bad_text: str) -> None:
    doc = _with(["pages", 0, "blocks", 0, "text"], bad_text)
    doc["stats"]["chars"] = sum(len(b["text"]) for p in doc["pages"] for b in p["blocks"])
    assert code_of(doc) == "parse_output_invalid"


def test_zero_width_joiner_is_allowed_between_devanagari_letters() -> None:
    """ZWJ and ZWNJ change how Devanagari conjuncts are drawn, so a cleaned text may keep them."""
    text = "क्‍ष और क्‌ष"
    doc = _with(["pages", 0, "blocks", 0, "text"], text)
    doc["stats"]["chars"] = sum(len(b["text"]) for p in doc["pages"] for b in p["blocks"])
    assert validate_parsed_document(doc).pages[0].blocks[0].text == text


def test_whitespace_only_or_untrimmed_text_is_rejected() -> None:
    for bad in ["", "   ", " leading", "trailing ", "double  space"]:
        doc = _with(["pages", 0, "blocks", 0, "text"], bad)
        doc["stats"]["chars"] = sum(len(b["text"]) for p in doc["pages"] for b in p["blocks"])
        assert code_of(doc) == "parse_output_invalid", bad


@pytest.mark.parametrize("level", [None, 0, 10, -1])
def test_a_heading_needs_a_level_between_1_and_9(level: Any) -> None:
    assert code_of(_with(["pages", 1, "blocks", 0, "level"], level)) == "parse_output_invalid"


def test_only_headings_have_a_level() -> None:
    assert code_of(_with(["pages", 0, "blocks", 0, "level"], 1)) == "parse_output_invalid"


def test_stats_must_match_the_content() -> None:
    assert code_of(_with(["stats", "chars"], 1)) == "parse_output_invalid"
    assert code_of(_with(["stats", "blocks"], 99)) == "parse_output_invalid"
    assert code_of(_with(["stats", "hidden_text_chars"], -1)) == "parse_output_invalid"


def test_flow_formats_have_one_page_with_number_one() -> None:
    """DOCX, TXT and MD have no real pages: one page, n = 1 (the chunker leaves page_start empty)."""
    payload = good(file_kind="docx", page_count=2)
    assert code_of(payload) == "parse_output_invalid"
    payload = good(file_kind="txt", page_count=1, pages=[{"n": 1, "blocks": [{"t": "para", "level": None, "text": "x"}]}])
    payload["stats"].update(chars=1, blocks=1)
    assert validate_parsed_document(payload).page_count == 1
    payload["pages"][0]["n"] = 2
    assert code_of(payload) == "parse_output_invalid"


def test_the_expected_kind_must_match() -> None:
    assert code_of(good(), expected_kind="docx") == "parse_output_invalid"
    assert validate_parsed_document(good(), expected_kind="pdf").file_kind == "pdf"


# ── Over-cap payloads ────────────────────────────────────────────────────────


def _pages(count: int, blocks_per_page: int = 1, text: str = "x") -> dict[str, Any]:
    pages = [
        {"n": i + 1, "blocks": [{"t": "para", "level": None, "text": text} for _ in range(blocks_per_page)]}
        for i in range(count)
    ]
    return good(
        page_count=max(count, 1),
        pages=pages,
        stats={
            "chars": count * blocks_per_page * len(text),
            "blocks": count * blocks_per_page,
            "hidden_text_chars": 0,
            "script_guess": "latn",
        },
    )


def test_too_many_pages_is_rejected_against_the_caller_cap() -> None:
    assert validate_parsed_document(_pages(5), max_pages=5).page_count == 5
    assert code_of(_pages(6), max_pages=5) == "parse_output_too_large"


def test_the_hard_page_cap_applies_whatever_the_caller_asks() -> None:
    assert code_of(_pages(pd.MAX_PAGES + 1), max_pages=10**9) == "parse_output_too_large"


def test_too_many_blocks_on_one_page_is_rejected() -> None:
    assert validate_parsed_document(_pages(1, pd.MAX_BLOCKS_PER_PAGE)).stats.blocks == pd.MAX_BLOCKS_PER_PAGE
    assert code_of(_pages(1, pd.MAX_BLOCKS_PER_PAGE + 1)) == "parse_output_too_large"


def test_too_many_blocks_in_total_is_rejected() -> None:
    per_page = pd.MAX_BLOCKS_PER_PAGE
    pages = pd.MAX_BLOCKS_TOTAL // per_page + 1
    assert code_of(_pages(pages, per_page), max_pages=pages) == "parse_output_too_large"


def test_a_block_over_the_character_cap_is_rejected() -> None:
    assert validate_parsed_document(_pages(1, 1, "x" * pd.MAX_BLOCK_CHARS)).stats.chars == pd.MAX_BLOCK_CHARS
    assert code_of(_pages(1, 1, "x" * (pd.MAX_BLOCK_CHARS + 1))) == "parse_output_too_large"


def test_total_text_over_the_cap_is_text_too_large() -> None:
    block = "x" * 1000
    payload = _pages(1, 11, block)
    assert validate_parsed_document(payload, max_text_chars=11_000).stats.chars == 11_000
    assert code_of(payload, max_text_chars=10_999) == "text_too_large"


def test_the_default_text_cap_is_two_million_characters() -> None:
    assert pd.DEFAULT_MAX_TEXT_CHARS == 2_000_000


def test_caps_are_checked_before_the_slow_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-cap payload must be refused cheaply, without building models for every block."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the models must not be built for an over-cap payload")

    monkeypatch.setattr(pd.ParsedDocument, "model_validate", boom)
    assert code_of(_pages(1, pd.MAX_BLOCKS_PER_PAGE + 1)) == "parse_output_too_large"


# ── No echo, fixed messages ──────────────────────────────────────────────────


def _hostile_payloads() -> list[dict[str, Any]]:
    marked = SECRET
    return [
        _with(["pages", 0, "blocks", 0, "text"], 5),
        _with(["pages", 0, "blocks", 0, "html" + marked], marked),
        _with(["file_kind"], marked),
        _with(["pages", 0, "blocks", 0, "text"], f"a\x00{marked}"),
        _with(["pages", 0, "blocks", 0, "t"], marked),
        _with(["stats", "script_guess"], marked),
        _with(["pages", 0, "n"], marked),
        {marked: marked},
    ]


@pytest.mark.parametrize("payload", _hostile_payloads())
def test_a_rejection_never_echoes_the_payload(payload: dict[str, Any]) -> None:
    with pytest.raises(ParsedDocumentError) as exc:
        validate_parsed_document(payload)
    err = exc.value
    for shown in (str(err), repr(err), err.message, err.code, repr(err.args)):
        assert SECRET not in shown
    assert err.__cause__ is None and err.__suppress_context__  # no chained pydantic error with the input


def test_every_code_has_one_fixed_message() -> None:
    assert set(pd.ERROR_MESSAGES) == {"parse_output_invalid", "parse_output_too_large", "text_too_large"}
    seen = set()
    for bad, expected in [
        (_with(["pages", 0, "n"], "1"), "parse_output_invalid"),
        (_pages(1, pd.MAX_BLOCKS_PER_PAGE + 1), "parse_output_too_large"),
    ]:
        with pytest.raises(ParsedDocumentError) as exc:
            validate_parsed_document(bad)
        assert exc.value.code == expected
        assert exc.value.message == pd.ERROR_MESSAGES[expected]
        seen.add(exc.value.message)
    assert len(seen) == 2
