"""
Security assertions for the document parser (step 5b-3), written as helpers.

Each helper is used by the REAL test of its protection (tests/test_parse_stage.py) and by the
prove-it-fails meta-test (tests/security/test_prove_it_fails_parse.py), which switches the protection
off and expects the helper to raise. A helper must therefore fail with AssertionError when the
protection is missing.
"""

from collections.abc import Awaitable, Callable
from typing import Any

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
