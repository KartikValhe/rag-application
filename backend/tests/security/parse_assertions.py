"""
Security assertions for the document parser (step 5b-3), written as helpers.

Each helper is used by the REAL test of its protection (tests/test_parse_stage.py) and by the
prove-it-fails meta-test (tests/security/test_prove_it_fails_parse.py), which switches the protection
off and expects the helper to raise. A helper must therefore fail with AssertionError when the
protection is missing.
"""

import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from app.core import sandbox
from app.core.upload_security import UploadSecurityError
from app.services.parsed_document import ParsedDocument
from app.workers import errors


async def _outcome(run: Callable[[], Awaitable[Any]]) -> tuple[Any, errors.IngestError | None]:
    try:
        return await run(), None
    except errors.IngestError as exc:
        return None, exc


async def assert_corrupt_child_output_is_rejected(run: Callable[[], Awaitable[Any]]) -> None:
    """A child that answers success with a document breaking the contract must fail permanently."""
    value, exc = await _outcome(run)
    assert exc is not None, "the parent accepted a document that breaks the contract"
    assert exc.permanent, f"a corrupt child must fail permanently, got {exc.code}"
    assert exc.code in ("parse_output_invalid", "parse_output_too_large", "too_many_pages"), exc.code
    assert value is None


async def assert_page_bomb_is_refused(run: Callable[[], Awaitable[Any]]) -> None:
    """A PDF whose pages the upload scan cannot count must still fail with too_many_pages."""
    value, exc = await _outcome(run)
    assert exc is not None, "a PDF over the page cap was parsed"
    assert exc.code == "too_many_pages", exc.code
    assert value is None


def assert_hidden_text_not_extracted(doc: ParsedDocument, hidden: str) -> None:
    """Hidden runs are counted in stats.hidden_text_chars, never extracted."""
    text = " ".join(b.text for p in doc.pages for b in p.blocks)
    assert hidden not in text, "hidden text was extracted"
    assert doc.stats.hidden_text_chars >= len(hidden), "hidden text was not counted"


# ── Review of 5b-3 ───────────────────────────────────────────────────────────


async def assert_parent_refuses_inflated_page_count(run: Callable[[str, str], Awaitable[Any]]) -> None:
    """A compromised child that claims 500 PDF pages or 101 slides (one short page of text) for a short file
    must be refused by the PARENT against the tenant's caps, whatever the child's own checks did.
    `run(variant, kind)` runs the real parse stage with that corrupt child."""
    for variant, kind, code in (
        ("pdf_page_count_500", "pdf", "too_many_pages"),
        ("pptx_page_count_101", "pptx", "too_many_slides"),
    ):
        value, exc = await _outcome(lambda v=variant, k=kind: run(v, k))  # type: ignore[misc]
        assert exc is not None, "the parent accepted a page count over the tenant's cap"
        assert exc.permanent and exc.code == code, (variant, exc.code)
        assert value is None


def assert_child_refuses_page_bomb(result: dict[str, Any]) -> None:
    """The CHILD's own re-check (backlog B33): a PDF whose page tree the upload scan cannot count, over the cap."""
    assert result.get("success") is not True, "the child parsed a PDF over the page cap"
    assert result == {"success": False, "error_code": "too_many_pages"}, result


async def assert_unexpected_child_exception_is_permanent(run: Callable[[], Awaitable[Any]]) -> None:
    """An exception the child does not expect is the FILE's fault: parse_failed, permanent, one attempt."""
    value, exc = await _outcome(run)
    assert exc is not None, "the parse succeeded although the extractor raised"
    assert exc.permanent, f"an unexpected exception in the child was a retryable {exc.code}, not a permanent error"
    assert exc.code == "parse_failed", exc.code
    assert value is None


def assert_nonstandard_xml_part_is_scanned(validate: Callable[[], Any]) -> None:
    """`validate()` runs the upload validation on a package whose XML part has an entity declaration and a
    name that does not end in .xml: it must be refused."""
    try:
        validate()
    except UploadSecurityError as exc:
        assert isinstance(exc.detail, dict) and exc.detail["code"] == "xml_entity_expansion_forbidden", exc.detail
        return
    raise AssertionError("a hostile XML part with a non-.xml name was accepted")


def assert_hostile_child_json_is_rejected(parse: Callable[[], Any], codes: tuple[str, ...]) -> None:
    """`parse()` parses one hostile raw answer with the parent's strict parser: a SandboxError with a stable code."""
    try:
        parse()
    except sandbox.SandboxError as exc:
        assert exc.code in codes, exc.code
        return
    raise AssertionError("the parent accepted a hostile child answer")


def boundary_problems(pieces: list[str]) -> list[str]:
    """What is wrong at the seams between pieces of one split text: a piece that starts with a joiner or a
    combining mark, or ends with a joiner or a virama, splits a conjunct and breaks the parent's text rules."""
    problems = []
    for a, b in zip(pieces, pieces[1:], strict=False):
        if b[0] in ("\u200d", "\u200c"):
            problems.append("a piece starts with a joiner")
        if unicodedata.category(b[0]).startswith("M"):
            problems.append("a piece starts with a combining mark")
        if a[-1] in ("\u200d", "\u200c", "\u094d"):
            problems.append("a piece ends with a joiner or virama")
    return problems


def assert_hard_cuts_never_split_a_cluster(split: Callable[[str], list[str]], text: str) -> None:
    pieces = split(text)
    assert len(pieces) >= 2
    problems = boundary_problems(pieces)
    assert not problems, "a hard cut split a conjunct: " + ", ".join(sorted(set(problems)))
