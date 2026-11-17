"""
Prove-it-fails for the fixes of the read-only review of step 5b-3.

Each test runs the scenario with the protection ON (the shared helper in parse_assertions.py passes), then
switches that ONE protection off and requires the same helper to RAISE, so the helper really checks it.

  1. permanent-error mapping   an unexpected exception in the child is a permanent parse_failed
  2. all-parts XML scan        a hostile XML part with a non-.xml name is refused
  3. child-output limits       one test per rule of the parent's strict JSON parser
  4. the cut-point rule        a hard cut never splits a conjunct

TEST database only: the autouse fixture asserts current_database() ends in _test before anything runs.
"""

import json
import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import sandbox, upload_security
from app.core.upload_security import UploadProfile, inspect_upload
from app.services import document_extractors as ex
from app.services.document_limits import default_limits
from app.services.parsed_document import MAX_BLOCK_CHARS
from app.workers import stages
from tests._parse_children import _with_junk
from tests.security.parse_assertions import (
    assert_hard_cuts_never_split_a_cluster,
    assert_hostile_child_json_is_rejected,
    assert_nonstandard_xml_part_is_scanned,
    assert_unexpected_child_exception_is_permanent,
)
from tests.test_parse_stage import PARSE
from tests.test_review_5b3_upload_scan import DOCX_MAIN, ENTITY, _docx, _with_main_part_renamed


@pytest.fixture(autouse=True)
async def _test_database_only() -> None:
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"], connect_args={"statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    finally:
        await engine.dispose()
    assert name.endswith("_test"), f"prove-it tests must run against the test database, not {name!r}"


# ── 1. Permanent-error mapping ───────────────────────────────────────────────


async def test_switching_off_the_childs_catch_all_is_caught() -> None:
    def scenario(mode: str) -> Any:
        async def run() -> Any:
            return await stages.parse_in_sandbox(
                b"Unit 1", file_kind="txt", limits=default_limits(), parse=PARSE,
                child_module="tests._parse_children", child_args=(mode,),
            )

        return run

    await assert_unexpected_child_exception_is_permanent(scenario("raise_value_error"))  # ON: passes
    with pytest.raises(AssertionError, match="a retryable worker_crash, not a permanent error"):
        await assert_unexpected_child_exception_is_permanent(scenario("raise_no_guard"))  # catch-all off in the child


# ── 2. The all-parts XML scan ────────────────────────────────────────────────


@pytest.mark.parametrize("profile", [UploadProfile.DOCUMENT, UploadProfile.TIMETABLE], ids=["document", "timetable"])
def test_switching_off_the_all_parts_xml_scan_is_caught(profile: UploadProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = _with_main_part_renamed(_docx(), "word/document.xml", "word/x.dat", DOCX_MAIN)

    def validate() -> Any:
        return inspect_upload(hostile, "syllabus.docx", profile)

    assert_nonstandard_xml_part_is_scanned(validate)  # ON: passes

    # OFF: the 5b-3 rule, parts selected by their .xml extension (and the word/ and xl/ folders)
    monkeypatch.setattr(upload_security, "_part_needs_xml_scan", lambda key, _declared: key.endswith(".xml"))
    with pytest.raises(AssertionError, match="a hostile XML part with a non-.xml name was accepted"):
        assert_nonstandard_xml_part_is_scanned(validate)


def test_switching_off_the_declared_type_rule_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name rule alone is not enough: a part with a binary-looking name and an XML content type."""
    import re

    from tests.test_review_5b3_upload_scan import _read, _rewrite

    data = _docx()
    ct = _read(data, "[Content_Types].xml").replace(
        "</Types>", '<Override PartName="/word/looks-like.png" ContentType="application/vnd.example+xml"/></Types>'
    )
    hostile = _rewrite(data, {"[Content_Types].xml": ct.encode()}, {"word/looks-like.png": ENTITY})

    def validate() -> Any:
        return inspect_upload(hostile, "syllabus.docx", UploadProfile.DOCUMENT)

    assert_nonstandard_xml_part_is_scanned(validate)  # ON: passes
    binary = re.compile(r"\.(png|jpe?g|gif|bmp|tiff?|wmf|emf|bin)$")
    monkeypatch.setattr(upload_security, "_part_needs_xml_scan", lambda key, _declared: not binary.search(key))
    with pytest.raises(AssertionError, match="a hostile XML part with a non-.xml name was accepted"):
        assert_nonstandard_xml_part_is_scanned(validate)


# ── 3. The child-output limits, one rule at a time ───────────────────────────

SMALL = sandbox.JsonLimits(max_structure=1000, max_depth=8)
INVALID = ("parse_output_invalid",)
TOO_LARGE = ("parse_output_too_large",)


def _raw(kind: str) -> bytes:
    return {
        "structure": _with_junk(b"[" + b"{}," * 5000 + b"{}]"),
        "depth": _with_junk(b"[" * 100 + b"]" * 100),
        "duplicate_key": b'{"success":true,"success":false}',
        "nan": b'{"success":true,"peak_memory_kb":NaN}',
        "float": b'{"success":true,"peak_memory_kb":1.5}',
        "big_int": b'{"success":true,"peak_memory_kb":' + b"9" * 100 + b"}",
        "lone_surrogate": b'{"success":true,"junk":"\\ud800"}',
        "nul": b'{"success":true,"junk":"a\\u0000b"}',
        "invalid_utf8": b'{"success":true,"junk":"\xff\xfe"}',
    }[kind]


RULES = [
    ("structure-count", "_check_structure", "structure", TOO_LARGE),
    ("depth", "_depth_ok", "depth", INVALID),
    ("duplicate-keys", "_pairs_hook", "duplicate_key", INVALID),
    ("nan-and-infinity", "_reject_constant", "nan", INVALID),
    ("floats", "_reject_float", "float", INVALID),
    ("big-ints", "_parse_int", "big_int", INVALID),
    ("lone-surrogates", "_string_ok", "lone_surrogate", INVALID),
    ("nul", "_string_ok", "nul", INVALID),
    ("strict-utf8", "_decode", "invalid_utf8", INVALID),
]


def _off(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    replacements: dict[str, Any] = {
        "_check_structure": lambda *_a, **_k: None,
        "_depth_ok": lambda *_a, **_k: True,
        "_pairs_hook": dict,
        "_reject_constant": lambda _name: 0,
        "_reject_float": float,
        "_parse_int": int,
        "_string_ok": lambda _s: True,
        "_decode": lambda raw: raw.decode("utf-8", errors="replace"),
    }
    monkeypatch.setattr(sandbox, target, replacements[target])


@pytest.mark.parametrize(("rule", "target", "kind", "codes"), RULES, ids=[r[0] for r in RULES])
def test_switching_off_one_child_output_limit_is_caught(
    rule: str, target: str, kind: str, codes: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(kind)

    def parse() -> Any:
        return sandbox.parse_child_json_strict(raw, limits=SMALL)

    assert_hostile_child_json_is_rejected(parse, codes)  # ON: passes
    _off(monkeypatch, target)
    with pytest.raises(AssertionError, match="the parent accepted a hostile child answer"):
        assert_hostile_child_json_is_rejected(parse, codes)


def test_the_hostile_answers_are_valid_json_apart_from_their_one_rule() -> None:
    """Sanity of the fixtures: without the strict parser each answer would have been accepted by json.loads
    (except the invalid UTF-8 one, which json.loads rejects only because bytes must be valid UTF-8)."""
    for kind in ("structure", "depth", "duplicate_key", "nan", "float", "big_int", "lone_surrogate", "nul"):
        json.loads(_raw(kind))


# ── 4. The cut-point rule ────────────────────────────────────────────────────


def test_switching_off_the_cut_point_rule_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    text_with_joiner_at_the_cut = "क" * (MAX_BLOCK_CHARS - 1) + "्" + "\u200d" + "ष" * 40
    assert_hard_cuts_never_split_a_cluster(ex.split_text, text_with_joiner_at_the_cut)  # ON: passes
    monkeypatch.setattr(ex, "_unsafe_cut", lambda _text, _cut: False)
    with pytest.raises(AssertionError, match="a hard cut split a conjunct"):
        assert_hard_cuts_never_split_a_cluster(ex.split_text, text_with_joiner_at_the_cut)

